"""开学季「召唤1次开学季专家」(expert_use) 攻关 —— 小程序真实遥测事件版。

🔴 结论来源：用户 2026-09-20 21:08–21:09 手动做完该任务的抓包（`probe_har.py` 分析）。

真正的点亮链路是**三条小程序遥测事件**（POST /v2/report，**裸数组**，不带信封）：

    21:08:48  expert_summon_click  type=expert_list_card  position=2
    21:08:50  expert_summon_click  type=quick_prompt      position=0
    21:09:04  expert_actual_use    type=send_message      characterCount=2
    21:09:07  chat_request_send    （AS 会话建好后）

**最小充分动作 = 只发 `expert_actual_use` 一条**（账号4 实测：+3 秒就 completed）。
上面两条 `expert_summon_click` 单独发**不点亮**（账号2 实测），发了也无害。

⚠️ **根因尚未用单变量实验钉死，别写成"事件名错了"**：
白天失败的 A1/A2 变体（`probe_expert_use.py`）**发的也是 `expert_actual_use`**，名字没错。
失败版与成功版最大的差异是 —— 失败版**没有任何客户端身份/环境字段**
（无 `ideType` / `extName` / `ideName` / `machineId` / `product` / `os` / `timezone`），
成功版带着**完整的小程序身份信封**；同活动里能点亮的 `chat_3_times` 也带这一套。
→ 最强候选 = **缺客户端身份信封**；并列候选 = `type` 值（`agent`→`send_message`）、
多余伪造字段污染、同批多发的 `expert_summoned`。四条均未隔离验证。

真实报文字段（照抄）：
    ideType=WorkBuddy_MP  ideVersion=2.4.2  extName=workbuddy-mp  extVersion=2.4.2
    product=SaaS  os=ios  osVersion=27.0  arch=""  machineId=<设备id>
    timezone=Asia/Shanghai  userId=<uid>  userNickname=<昵称>  ideName=wx_app_cloud
    expert_summon_click: id/name=<expert_id>  expertTitle=<职业>  type=...  position=N
    expert_actual_use:   id/name=<expert_id>  expertTitle=<职业>  type=send_message
                         characterCount=2  expertType=agent

用法：
    python probe_expert_mp.py status <账号号>          # 只读
    python probe_expert_mp.py summon <账号号>          # 只发 2 条 summon_click
    python probe_expert_mp.py use    <账号号>          # summon + actual_use
    python probe_expert_mp.py full   <账号号>          # summon + use + 会话/ACP/chat/完成
    python probe_expert_mp.py sweep  [--dry]           # 对全部未完成账号跑 full
"""
import json
import os
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

# 抓包里的小程序 UA / Referer（report 域用）
MP_UA_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 27_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
             "MicroMessenger/8.0.78(0x18004e2b) NetType/WIFI Language/zh_CN")
# 小程序 referer 里的 AppID。
# 🔴 开源版刻意**不写真实 AppID**：它是「WorkBuddy 官方小程序」的标识 —— 严格说不是密钥
#    （任何人在微信里打开该小程序都能看到），但会命中 GitHub 的 secret scanning 规则，
#    并被标记成 "public leak"，所以仓库里只留占位符。
# 需要跑这个探针时，自己填（可从客户端本地日志 ~/.workbuddy/logs/ 里的真实报文抄，
# 或直接看小程序页面的 URL）：
#     set WB_PROBE_MP_APPID=wx****************
_MP_APPID = os.environ.get("WB_PROBE_MP_APPID", "")
MP_REFERER = ("https://servicewechat.com/%s/140/page-frame.html" % _MP_APPID
              if _MP_APPID else "")


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
        out("   %-10s expert_use 未下发" % tag)
        return None
    out("   %-10s expert_use = %-12s %s/%s" % (tag, t.get("status"), t.get("progress"),
                                              t.get("target_count")))
    return t.get("status"), t.get("progress"), t.get("target_count"), in_period


def pick_expert(mod, s):
    """照抄抓包：expert/list(categories=[16-BackToSchool]) → get-by-ids 拿职业名。"""
    r = mod._school_post(s, mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/expert/list",
                         {"edition_mode": "all,domestic", "page": 1, "page_size": 100,
                          "sort_by": "use_count", "sort_order": "desc",
                          "categories": [CATEGORY], "expert_type": "agent"})
    experts = (r.json().get("data") or {}).get("experts") or []
    if not experts:
        return "", "", ""
    eid = experts[0].get("expert_id") or experts[0].get("source_id") or experts[0].get("id") or ""
    if not eid:
        return "", "", ""
    title = experts[0].get("profession_zh") or experts[0].get("profession") or ""
    try:
        r2 = mod._school_post(s, mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/expert/get-by-ids",
                              {"expert_ids": [eid], "include_invisible": True})
        ex = ((r2.json().get("data") or {}).get("experts") or [])
        if ex:
            title = ex[0].get("profession_zh") or ex[0].get("profession") or title
    except Exception:  # noqa: BLE001
        pass
    return eid, (experts[0].get("display_name_zh") or experts[0].get("displayName")
                 or experts[0].get("name") or "?"), title


# ---------------- 遥测事件（照抄抓包） ----------------

def _mp_base(mod, uid, nick):
    return {"ideType": "WorkBuddy_MP", "ideVersion": "2.4.2", "extName": "workbuddy-mp",
            "extVersion": "2.4.2", "product": "SaaS", "os": "ios", "osVersion": "27.0",
            "arch": "", "machineId": mod.derive_id(uid, "mp-machine"),
            "timezone": "Asia/Shanghai", "userId": uid, "userNickname": nick,
            "ideName": "wx_app_cloud"}


def ev_summon_click(mod, uid, nick, eid, title, typ, pos):
    ev = _mp_base(mod, uid, nick)
    ev.update({"eventCode": "expert_summon_click", "timestamp": int(time.time() * 1000),
               "id": eid, "name": eid, "expertTitle": title, "type": typ, "position": pos})
    return ev


def ev_actual_use(mod, uid, nick, eid, title, chars=2):
    ev = _mp_base(mod, uid, nick)
    ev.update({"eventCode": "expert_actual_use", "timestamp": int(time.time() * 1000),
               "id": eid, "name": eid, "expertTitle": title,
               "type": "send_message", "characterCount": chars, "expertType": "agent"})
    return ev


def send_events(mod, s, events):
    """裸数组 POST /v2/report（🔴 绝不套 common/events 信封）。"""
    s.headers["User-Agent"] = MP_UA_IOS
    if MP_REFERER:                      # 未配置 WB_PROBE_MP_APPID 时不带 Referer
        s.headers["Referer"] = MP_REFERER
    try:
        # ⚠️ api_retry 内部已固定 timeout=25，多传会 duplicate keyword 报错
        r = mod.api_retry(s, "POST", mod.SCHOOL_DOMAIN + "/v2/report", body=events)
        code = getattr(r, "status_code", 0)
        try:
            j = r.json()
        except Exception:  # noqa: BLE001
            j = {}
        return code, j
    except Exception as exc:  # noqa: BLE001
        return 0, {"err": "%s: %s" % (type(exc).__name__, str(exc)[:120])}


# ---------------- AS 会话 + ACP（保留，作为「完整流程」的可选补充） ----------------

def create_conv(mod, s, eid):
    body = {"prompt": "你好", "tags": ["expert:%s" % eid, "locale:zh"],
            "conversationOrigin": "workbuddy-mp"}
    r = s.post(mod.SCHOOL_DOMAIN + AS_CONV, json=body, timeout=40, verify=False)
    try:
        return r.status_code, r.json()
    except Exception:  # noqa: BLE001
        return r.status_code, {"raw": r.text[:300]}


def acp_turn(conv, uid, nick, text="你好", wait=90):
    import requests

    data = conv.get("data") or {}
    sess = data.get("session") or {}
    sandbox_id = sess.get("sandboxId") or data.get("runtimeId") or ""
    link = data.get("link") or sess.get("link") or ""
    token = sess.get("token") or ""
    cluster = data.get("clusterDomainSuffix") or ""
    port = ""
    if link:
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
        return 0, lines, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def finish_conv(mod, s, conv_id):
    r = s.post(mod.SCHOOL_DOMAIN + AS_CONV + str(conv_id),
               json={"status": "completed"}, timeout=30, verify=False)
    return r.status_code, r.text[:140]


def report_chat(mod, s, uid, nick, conv_id):
    rid = uuid.uuid4().hex
    ev = _mp_base(mod, uid, nick)
    ev.update({"eventCode": "chat_request_send", "timestamp": int(time.time() * 1000),
               "source": "mini_program", "mode": "craft", "conversationId": str(conv_id),
               "requestId": rid, "inputLength": 2, "requestModelId": "hy3",
               "requestModelName": "Hy3", "mentionContexts": [], "mentionContextCount": 0,
               "command": "", "traceId": rid})
    return send_events(mod, s, [ev])


def poll(mod, s, want=("completed", "claimed"), rounds=8, gap=3):
    st = None
    for i in range(rounds):
        time.sleep(gap)
        st = show(mod, s, "   +%2ds" % ((i + 1) * gap))
        if st and st[0] in want:
            break
    return st


def draw_wheel_if_any(mod, s, uid, nick):
    """🔴 领奖会发**抽奖机会**（`expert_use` 每天 50分+1抽）。

    独立脚本走的是 `school_run_tasks` 之外的路径 → 引擎的 `school_final_sweep()`
    收尾（补领 + 抽转盘）**不会执行** → 机会就躺在那儿没人抽。
    2026-09-20 实测漏掉 6 次（账号2-7），用户发现后手工补抽（得 216 积分）。
    所以探针自己补这一步：只查余额、>0 才抽（`school_lottery` 幂等）。
    """
    try:
        d = mod._school_get(s, mod.SCHOOL_BASE + "/config").json()
        bal = ((d.get("data") or {}).get("chance") or {}).get("balance", 0) or 0
    except Exception as exc:  # noqa: BLE001
        out("   🎡 抽奖前查余额失败 %s: %s" % (type(exc).__name__, str(exc)[:60]))
        return
    if bal <= 0:
        out("   🎡 转盘余额 0，无需抽")
        return
    out("   🎡 转盘余额 %s，补抽…" % bal)
    try:
        mod.school_lottery(s, uid, nick, lambda m: out("   " + m.strip()))
    except Exception as exc:  # noqa: BLE001
        out("   🎡 抽奖失败 %s: %s" % (type(exc).__name__, str(exc)[:60]))


def claim_if_done(mod, s, st, uid, nick):
    if st and st[0] == "completed":
        out("   🎁 领奖 → %s" % mod._school_claim(s, "expert_use"))
        show(mod, s, "领奖后")
    elif st and st[0] == "claimed":
        out("   ✅ 已到手")
    else:
        return
    # 领奖发的抽奖机会顺手用掉（独立脚本不走引擎收尾，必漏）
    draw_wheel_if_any(mod, s, uid, nick)


# ---------------- 命令 ----------------

def run_one(idx, mode, accs=None):
    mod = E.load_upstream()
    if accs is None:
        _, accs = load()
    acc = accs[idx - 1]
    at = acc["at"]
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = sset(at, mod)

    out("=" * 78)
    out("账号%d %s —— 模式 %s" % (idx, acc.get("nick"), mode))
    out("=" * 78)
    r0 = show(mod, s, "起始")
    if r0 and r0[0] in ("claimed", "completed"):
        out("   已完成/已领，跳过")
        return True

    eid, ename, etitle = pick_expert(mod, s)
    out("   选中专家: %s / %s (%s)" % (ename, etitle, eid))
    if not eid:
        out("   ✘ 拿不到 expert_id")
        return False

    if mode in ("summon", "use", "full"):
        evs = [ev_summon_click(mod, uid, nick, eid, etitle, "expert_list_card", 2)]
        time.sleep(1)
        evs.append(ev_summon_click(mod, uid, nick, eid, etitle, "quick_prompt", 0))
        for e in evs:
            code, j = send_events(mod, s, [e])
            out("   ① %s type=%s pos=%s → HTTP %s %s"
                % (e["eventCode"], e["type"], e["position"], code,
                   json.dumps(j, ensure_ascii=False)[:80]))
            time.sleep(1)
        st = poll(mod, s, rounds=3, gap=3)
        if st and st[0] in ("completed", "claimed"):
            claim_if_done(mod, s, st, uid, nick)
            return True
        if mode == "summon":
            out("   ⏸ summon_click 未点亮（继续用 use 验证）")
            return False

    if mode in ("use", "full"):
        ev = ev_actual_use(mod, uid, nick, eid, etitle)
        code, j = send_events(mod, s, [ev])
        out("   ② expert_actual_use type=send_message → HTTP %s %s"
            % (code, json.dumps(j, ensure_ascii=False)[:80]))
        st = poll(mod, s, rounds=4, gap=3)
        if st and st[0] in ("completed", "claimed"):
            claim_if_done(mod, s, st, uid, nick)
            return True
        if mode == "use":
            out("   ⏸ actual_use 未点亮（继续用 full 验证）")
            return False

    if mode == "full":
        code, d = create_conv(mod, s, eid)
        data = d.get("data") or {}
        conv_id = data.get("id") or ""
        out("   ③ 创建 AS 会话 → HTTP %s code=%s id=%s status=%s"
            % (code, d.get("code"), conv_id, data.get("status")))
        if conv_id:
            man = (data.get("manifest") or {})
            envs = {e.get("key"): e.get("value") for e in (man.get("envs") or [])}
            out("      manifest.X_EXPERT_ID = %s" % envs.get("X_EXPERT_ID"))
            t0 = time.time()
            ac, lines, tail = acp_turn(d, uid, nick)
            out("   ④ ACP 对话回合 → HTTP %s %d 行 %.1fs %s"
                % (ac, lines, time.time() - t0, (tail or "").replace("\n", " ")[:110]))
            time.sleep(2)
            cc, cj = report_chat(mod, s, uid, nick, conv_id)
            out("   ⑤ chat_request_send → HTTP %s %s"
                % (cc, json.dumps(cj, ensure_ascii=False)[:70]))
            time.sleep(2)
            fc, ft = finish_conv(mod, s, conv_id)
            out("   ⑥ 标记 completed → HTTP %s %s" % (fc, ft[:70]))
        st = poll(mod, s, rounds=8, gap=3)
        claim_if_done(mod, s, st, uid, nick)
        if not (st and st[0] in ("completed", "claimed")):
            out("   ❌ 仍未点亮")
            return False
    return True


def cmd_status(idx):
    mod = E.load_upstream()
    _, accs = load()
    s = sset(accs[idx - 1]["at"], mod)
    out("账号%d（%s）" % (idx, accs[idx - 1].get("nick")))
    show(mod, s, "当前")
    return 0


def cmd_sweep(dry=False):
    mod = E.load_upstream()
    _, accs = load()
    todo = []
    for i, a in enumerate(accs, 1):
        s = sset(a["at"], mod)
        st = show(mod, s, "账号%d" % i)
        if not (st and st[0] in ("completed", "claimed")):
            todo.append(i)
    out("需要处理: %s" % (todo or "无"))
    if dry or not todo:
        return 0
    for i in todo:
        try:
            run_one(i, "full", accs=accs)
        except Exception as exc:  # noqa: BLE001
            out("   账号%d 异常: %s: %s" % (i, type(exc).__name__, str(exc)[:120]))
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    flags = {a for a in args if a.startswith("--")}
    args = [a for a in args if not a.startswith("--")]
    if not args:
        out(__doc__)
        return 2
    cmd = args[0].lower()
    if cmd == "status":
        return cmd_status(int(args[1]) if len(args) > 1 else 1)
    if cmd in ("summon", "use", "full"):
        ok = run_one(int(args[1]) if len(args) > 1 else 1, cmd)
        return 0 if ok else 1
    if cmd == "sweep":
        return cmd_sweep(dry="--dry" in flags)
    out("未知命令 %s（可用：status / summon / use / full / sweep）" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
