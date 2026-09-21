"""⚠️【结论已作废 · 2026-09-20 晚更正】请改用 `probe_expert_mp.py`。

本文件当初（同日晚）根据抓包推出的结论——「必须创建带 tags=['expert:<id>'] 的 AS 会话
+ 真的跑一次 ACP 对话回合才能点亮 expert_use」——**是错的**。
实测（账号2 / 账号4）：AS 会话和 ACP 都不是必要条件。

真正点亮的唯一必要动作：往 `www.codebuddy.cn/v2/report` 发一条**裸数组**事件
`{"eventCode": "expert_actual_use", ...}`（MP 遥测形状）。详见 `probe_expert_mp.py`
与 `engine.apply_school_activity_fix` 的补丁 ②b。

保留本文件只为留存排查过程；下面代码里的 AS 会话 / ACP 部分不要当作判据使用。

--------------------------------------------------
（以下为原始记录，结论已作废）

开学季「召唤 1 次专家」攻关 —— 用真实抓包反推出的完整流程。

🔴 结论来源：用户 2026-09-20 21:08–21:09 手动做完【召唤1次开学季专家】的抓包
（用 `probe_har.py` 分析）。真实流程（按时间）：

    21:08:35  GET  /portal/activity/school/tasks          → expert_use: in_progress 0/1
    21:08:39  POST /v2/operation-platform/market/expert/list
    21:08:48  POST /v2/operation-platform/market/expert/get-by-ids
    21:09:05  POST /console/as/conversations/            ★ body 带 tags=["expert:<ID>"]
    21:09:06  POST https://miniprogram.e2b.*.sandbox.../acp/bootstrap
                                                          ★★ 真正的「对话」在这里：
                                                             JSON-RPC 一次性发 initialize +
                                                             session/new + set_model +
                                                             session/prompt
    21:09:07  POST /v2/report                             chat_request_send（mode=craft）
    21:09:14  POST /console/as/conversations/<id>         body {"status":"completed"}
    21:09:24  DELETE .../acp                              关闭 ACP 连接
    21:09:34  POST /portal/activity/school/tasks/expert_use/claim  → ✅ chance_granted 1
    21:09:35  GET  /portal/activity/school/tasks          → expert_use: claimed 1/1

📌 之前 15 种方案全失败的原因：漏了两件事 ——
   ① 会话创建体里的 `tags:["expert:<expert_id>"]`（服务端靠这个 tag 认「这是专家对话」）；
   ② **真的跑一次 ACP 对话回合**。
   实测：只做①（会话创建成功、manifest 里确实带上了 `X_EXPERT_ID`、status 也标成 completed）
   → 任务**仍然不动**；必须补上②的 `session/prompt`。

用法：
    python probe_expert_summon.py status <账号号>              # 只读
    python probe_expert_summon.py try    <账号号>              # 完整跑一遍（会真花一次模型额度）
    python probe_expert_summon.py try    <账号号> --no-acp     # 只创建会话，不跑对话（对照用）
"""
import json
import sys
import time
import uuid

import engine as E

CATEGORY = "16-BackToSchool"
AS_CONV = "/console/as/conversations/"

# 抓包里 mp 客户端带的头（照抄，别改）
MP_HDRS = {
    "X-Client-Platform": "mp-weixin",
    "X-Client-Product": "workbuddy-mp",
    "X-Client-Version": "2.4.2",
    "X-Platform": "wechatmp",
    "X-Call-Type": "client",
    "X-Credential-Type": "access_token",
    "Accept": "application/json",
}


def out(msg=""):
    print(msg)


def load():
    cfg = E.load_config()
    accs, _ = E.read_switch_accounts(cfg)
    E.write_token_file(accs)
    sys.argv = ["workbuddy_daily"]
    return E.load_upstream(), accs


def sset(at, mod):
    s = mod._school_session(at)
    s.headers.update(MP_HDRS)
    return s


def show(mod, s, tag):
    ts, in_period = mod._school_fetch_tasks(s)
    t = next((x for x in ts if isinstance(x, dict) and x.get("task_code") == "expert_use"), None)
    if not t:
        out("   %s expert_use 未下发" % tag)
        return None
    out("   %-10s expert_use = %-12s %s/%s" % (tag, t.get("status"), t.get("progress"),
                                              t.get("target_count")))
    return t.get("status"), t.get("progress"), t.get("target_count"), in_period


def pick_expert(mod, s):
    r = mod._school_post(s, mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/expert/list",
                         {"edition_mode": "all,domestic", "page": 1, "page_size": 100,
                          "sort_by": "use_count", "sort_order": "desc",
                          "categories": [CATEGORY], "expert_type": "agent"})
    experts = (r.json().get("data") or {}).get("experts") or []
    for e in experts:
        eid = e.get("expert_id") or e.get("source_id") or e.get("id") or ""
        if eid:
            return eid, (e.get("display_name_zh") or e.get("displayName")
                         or e.get("name") or "?")
    return "", ""


def create_conv(mod, s, eid):
    """★ 关键：带 tags=['expert:<id>'] 创建 AS 会话。"""
    body = {"prompt": "你好", "tags": ["expert:%s" % eid, "locale:zh"],
            "conversationOrigin": "workbuddy-mp"}
    r = s.post(mod.SCHOOL_DOMAIN + AS_CONV, json=body, timeout=40, verify=False)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"raw": r.text[:300]}


def acp_turn(conv, uid, nick, text="你好", wait=90):
    """★★ 真正的「对话」：往沙箱的 /acp/bootstrap 发 JSON-RPC（照抄抓包报文）。

    返回 (http码, 收到的事件行数, 结尾片段)。这是任务判据所在 —— 不跑这一步点不亮。
    """
    import requests

    data = conv.get("data") or {}
    sess = data.get("session") or {}
    sandbox_id = sess.get("sandboxId") or data.get("runtimeId") or ""
    link = data.get("link") or sess.get("link") or ""
    token = sess.get("token") or ""
    cluster = data.get("clusterDomainSuffix") or ""
    port = ""
    if link:
        # link 形如 https://65225-<sandboxId>.e2b.bj7.sandbox.cloudstudio.club/acp
        host = link.split("://", 1)[-1].split("/")[0]
        port = host.split("-", 1)[0]
    if not (sandbox_id and port):
        return 0, 0, "拿不到沙箱地址（link=%r）" % link[:80]

    url = "https://%s-%s.e2b.%s/acp/bootstrap" % (port, sandbox_id, cluster)
    cid = str(data.get("id") or "")
    rid = uuid.uuid4().hex
    msg = {"type": "text", "text": text}
    payload = {
        "initialize": {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": 1,
                                  "clientCapabilities": {"_meta": {"codebuddy.ai":
                                                                   {"cwd": "/workspace"}},
                                                         "fs": {"readTextFile": False,
                                                                "writeTextFile": False}}}},
        "sessionNew": {"jsonrpc": "2.0", "id": 2, "method": "session/new",
                       "params": {"sessionId": cid, "cwd": "/workspace",
                                  "mcpServers": [{"name": "workbuddy-mp", "type": "acp"}]}},
        "setConfigOption": {"jsonrpc": "2.0", "id": 12, "method": "session/set_config_option",
                            "params": {"sessionId": cid, "configId": "language", "value": "zh"}},
        "setModel": {"jsonrpc": "2.0", "id": 3, "method": "session/set_model",
                     "params": {"sessionId": cid, "modelId": "hy3"}},
        "prompt": {"jsonrpc": "2.0", "id": 4, "method": "session/prompt",
                   "params": {"sessionId": cid, "prompt": [msg],
                              "_meta": {"codebuddy.ai": {
                                  "conversationRequestId": rid,
                                  "clientRequestId": str(uuid.uuid4()),
                                  "telemetryClientInfo": {
                                      "ideType": "WorkBuddy_MP", "ideName": "wx_app_cloud",
                                      "ideVersion": "2.4.2", "extName": "workbuddy-mp",
                                      "extVersion": "2.4.2", "machineId": "",
                                      "userId": uid, "userNickname": nick}}}}},
    }
    hdrs = dict(MP_HDRS)
    hdrs.update({"Content-Type": "application/json", "Accept": "text/event-stream",
                 "x-cs-sandbox-id": sandbox_id, "x-cs-sandbox-port": str(port),
                 "x-workbuddy-flow-type": "turn"})
    if token:
        hdrs["Authorization"] = "Bearer " + token

    lines, tail, done = 0, "", False
    try:
        r = requests.post(url, json=payload, headers=hdrs, timeout=30, verify=False,
                          stream=True)
        code = r.status_code
        if code != 200:
            return code, 0, r.text[:200]
        deadline = time.time() + wait
        for raw in r.iter_lines(decode_unicode=False):
            lines += 1
            s_line = (raw or b"").decode("utf-8", "replace")
            if s_line.startswith("data:"):
                tail = s_line[:160]
                if '"stopReason"' in s_line or "turn/complete" in s_line:
                    done = True
            if done or time.time() > deadline:
                break
        r.close()
        return code, lines, tail
    except Exception as exc:  # noqa: BLE001
        # 链接可能只允许被「端口映射」路由（抓包里走的是 miniprogram.* 主机名 + 两个 x-cs 头）
        return 0, lines, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def finish_conv(mod, s, conv_id):
    r = s.post(mod.SCHOOL_DOMAIN + AS_CONV + str(conv_id),
               json={"status": "completed"}, timeout=30, verify=False)
    return r.status_code, r.text[:140]


def report_chat(mod, s, uid, nick, conv_id, eid=""):
    """chat_request_send，形状逐字段照抄抓包（mode=craft、requestId≠conversationId、带 traceId）。"""
    rid = uuid.uuid4().hex
    ev = {"eventCode": "chat_request_send", "timestamp": int(time.time() * 1000),
          "ideType": "WorkBuddy_MP", "ideVersion": "2.4.2", "extName": "workbuddy-mp",
          "extVersion": "2.4.2", "product": "SaaS", "os": "ios", "osVersion": "27.0",
          "arch": "", "machineId": mod.derive_id(uid, "mp-machine"),
          "timezone": "Asia/Shanghai", "userId": uid, "userNickname": nick,
          "ideName": "wx_app_cloud", "source": "mini_program", "mode": "craft",
          "conversationId": str(conv_id), "requestId": rid, "inputLength": 2,
          "requestModelId": "hy3", "requestModelName": "Hy3", "mentionContexts": [],
          "mentionContextCount": 0, "command": "", "traceId": rid}
    # ⚠️ api_retry 内部已固定 timeout=25，多传会 duplicate keyword 报错
    r = mod.api_retry(s, "POST", mod.SCHOOL_DOMAIN + "/v2/report", body=[ev])
    return getattr(r, "status_code", 0)


def cmd_status(idx):
    mod, accs = load()
    s = sset(accs[idx - 1]["at"], mod)
    out("账号%d（%s）" % (idx, accs[idx - 1].get("nick")))
    show(mod, s, "当前")
    return 0


def cmd_try(idx, with_acp=True):
    mod, accs = load()
    acc = accs[idx - 1]
    at = acc["at"]
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = sset(at, mod)

    out("=" * 78)
    out("账号%d %s —— AS 会话 + ACP 对话回合%s"
        % (idx, acc.get("nick"), "" if with_acp else "（已跳过 ACP）"))
    out("=" * 78)
    r0 = show(mod, s, "起始")
    if r0 and r0[0] in ("claimed", "completed"):
        out("   已完成/已领，今天不用再试")
        return 0

    eid, ename = pick_expert(mod, s)
    out("   选中专家: %s (%s)" % (ename, eid))
    if not eid:
        out("   ✘ 拿不到 expert_id")
        return 2

    code, d = create_conv(mod, s, eid)
    data = d.get("data") or {}
    conv_id = data.get("id") or ""
    out("   ① 创建 AS 会话 → HTTP %s code=%s id=%s status=%s"
        % (code, d.get("code"), conv_id, data.get("status")))
    if not conv_id:
        out("     响应: %s" % json.dumps(d, ensure_ascii=False)[:400])
        return 2
    man = (data.get("manifest") or {})
    envs = {e.get("key"): e.get("value") for e in (man.get("envs") or [])}
    out("     会话 manifest.X_EXPERT_ID = %s（有值 = 专家 tag 生效）" % envs.get("X_EXPERT_ID"))

    # ★★ 关键步骤：真的跑一次对话回合
    if with_acp:
        t0 = time.time()
        code, lines, tail = acp_turn(d, uid, nick)
        out("   ② ACP 对话回合 → HTTP %s  收到 %d 行事件  用时 %.1fs"
            % (code, lines, time.time() - t0))
        if tail:
            out("     结尾: %s" % tail.replace("\n", " ")[:150])

    time.sleep(2)
    http = report_chat(mod, s, uid, nick, conv_id, eid)
    out("   ③ chat_request_send（mode=craft）→ HTTP %s" % http)

    time.sleep(2)
    fc, ft = finish_conv(mod, s, conv_id)
    out("   ④ 标记会话 completed → HTTP %s %s" % (fc, ft[:80]))

    out("   ⑤ 轮询任务状态…")
    st = None
    for i in range(8):
        time.sleep(3)
        st = show(mod, s, "   +%2ds" % ((i + 1) * 3))
        if st and st[0] in ("completed", "claimed"):
            break

    if st and st[0] == "completed":
        out("   🎁 领奖 → %s" % mod._school_claim(s, "expert_use"))
        show(mod, s, "领奖后")
    elif st and st[0] == "claimed":
        out("   ✅ 已到手")
    else:
        out("   ❌ 仍未点亮（会话 %s）" % conv_id)
    return 0


def cmd_finish(idx, conv_id):
    mod, accs = load()
    s = sset(accs[idx - 1]["at"], mod)
    out("标记会话 %s 完成：HTTP %s" % (conv_id, finish_conv(mod, s, conv_id)[0]))
    time.sleep(3)
    st = show(mod, s, "当前")
    if st and st[0] == "completed":
        out("领奖 → %s" % mod._school_claim(s, "expert_use"))
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:]]
    flags = {a for a in args if a.startswith("--")}
    args = [a for a in args if not a.startswith("--")]
    if not args:
        out(__doc__)
        return 2
    cmd = args[0].lower()
    if cmd == "status":
        return cmd_status(int(args[1]) if len(args) > 1 else 1)
    if cmd == "try":
        return cmd_try(int(args[1]) if len(args) > 1 else 1, with_acp="--no-acp" not in flags)
    if cmd == "finish":
        return cmd_finish(int(args[1]), args[2])
    out("未知命令 %s（可用：status / try / finish）" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
