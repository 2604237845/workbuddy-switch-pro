#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""⚠️【结论已作废 · 2026-09-20 晚推翻】真实答案见 `probe_expert_mp.py`。

下面第 6 行原来写着「从外部无法点亮」—— **错的**。
用户手动做完该任务 + 抓 HAR 后拿到真实报文，照抄后**单个账号 3 秒内就点亮**。

⚠️ **但别把根因说成"事件名发错了"**（我一度这么写，已更正）：本文件里的
A1 / A2 变体**发的就是正确的事件名 `expert_actual_use`**，照样失败。
失败版与成功版最大的差异是：失败版**没有任何客户端身份/环境字段**
（无 `ideType` / `extName` / `ideName` / `machineId` / `product` / `os` / `timezone`），
成功版带着**完整的小程序身份信封**。见本文件 `make_events()` 的 `base()` —— 一眼可见。

→ **最强候选根因：缺客户端身份信封**（开学季是小程序专属活动，服务端按客户端身份归因）；
  并列候选：`type` 值（`"agent"` vs `"send_message"`）、多余伪造字段污染、
  同批多发的 `expert_summoned`。**四条都没做单变量隔离**，别当已证结论。

保留本文件只为留存排查过程，**不要再照它下结论**。

--------------------------------------------------
（以下为原始记录，结论已作废）

开学季 `expert_use`（召唤1次开学季专家）攻关记录 + 可复跑的实验集合。

════════════════════════════════════════════════════════════════════════
原结论（2026-09-20）：**从外部无法点亮**。  ← 已推翻
════════════════════════════════════════════════════════════════════════
任务本身：title「召唤1 次开学季专家」，desc「召唤 1 位开学季专家并进行 1 次对话」，
`jump_url_mp: /subpackages/secondary/expert-center/index?category_id=BackToSchool`。
7 个账号全是 `in_progress 0/1`。开学季活动 **2026-09-24 结束**。

为什么反推不出来：
  开学季是**小程序专属活动** —— `/portal/activity/school` 不是网页（GET 返回
  `{"error_msg":"404 Route Not Found"}`），拿不到前端 JS 去读它真实发的事件码。
  能反推的只有 API 行为，而 API 对所有尝试都回 `HTTP 200 {"code":0,"msg":"OK"}` 却**不计分**
  （跟"信封格式"那种静默忽略同一个套路）。

已试过并全部失败（15 种）：
  A. 事件形状（→ codebuddy.cn `/v2/report`，裸数组）
     A1 expert_summoned + expert_actual_use（带 activityId）      ❌
     A2 同上但不带 activityId                                     ❌
     A3 驼峰 `ExpertActualUse`（照 t_team_3 的 meta 命名）        ❌
     A4 驼峰 + 蛇形混合两条                                       ❌
     A5 expert_summoned + expert_actual_use + chat_request_send  ❌
     A6 chat_request_send 带 expertId/expertName/agentType        ❌
     A7 驼峰 + chat_request_send                                  ❌
     A8 事件带 source="growth-center"                             ❌
  B. 换目标域 / 身份
     B1 往 BASE（www.workbuddy.cn）打 t_expert_5 的成功形状        ❌
     B2 走 copilot.tencent.com + `X-Client-Platform: miniprogram` ❌
  C. 请求头组合（→ codebuddy.cn）
     C1 `X-Client-Platform: miniprogram`                          ❌
     C2 `X-Product: SaaS`                                         ❌
     C3 Referer 指到专家中心                                       ❌
     C4 C1 + C2                                                    ❌
  D. 专用接口（全部 404）
     `/tasks/expert-complete`、`/tasks/expert_use/complete`、
     `/tasks/expert/complete`、`/tasks/complete`、`/expert/summon`,
     `/tasks/expert_use`(GET)、`/tasks/expert_use/config` 等        ❌
     （对照：`/tasks/expert_use/viewed` 存在 → HTTP 200，所以探测方式本身有效）
  E. 真实对话搭车（skill_1 的突破口）
     E1 开学季域有完整对话 API（`/console/webchat/conversations` 200；
        `/console/chat/completions` 要求 `stream=True`），
        真发流式对话 + `_meta["codebuddy.ai"]["growthEvent"]` 搭车   ❌
        （meta 两种命名：蛇形 expert_summoned/expert_actual_use、驼峰 ExpertActualUse）
     E2 BASE 域真实对话 + 带 activityId 的 meta                     ❌

另外确认过的两件事（有价值，别重试）：
  · 专家列表接口返回的字段是 **`expert_id`**，不是 `id` ——
    上游 `_school_fetch_expert()` 读 `e["id"]` 拿到空串，导致 `if eid:` 不成立、
    **上报代码从未执行过**。这是个真 bug（虽然修了也点不亮）。
  · 分类过滤是有效的：`categories:["16-BackToSchool"]` 返回的 10 个专家全属该分类。

════════════════════════════════════════════════════════════════════════
用法：
    python probe_expert_use.py status      # 只读：看 7 个账号的 expert_use 状态
    python probe_expert_use.py try <变体>  # 复跑某个变体（默认 A1）
      变体名：A1 A3 A5 A6 B1 C1 E1
"""
import json
import sys
import time
import uuid
from pathlib import Path

HUB = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB))

import engine as E  # noqa: E402

CODE = "expert_use"


def load():
    cfg = E.load_config()
    accounts, _ = E.read_switch_accounts(cfg)
    E.write_token_file(accounts)
    argv_bak = list(sys.argv)
    sys.argv = ["workbuddy_daily"]
    try:
        mod = E.load_upstream()
    finally:
        sys.argv = argv_bak
    return mod, accounts


def make_events(mod, uid, eid, ename, activity_id=True, camel=False, with_chat=False):
    now = int(time.time() * 1000)
    conv = "conv-" + str(uuid.uuid4())
    rid = str(uuid.uuid4())
    mid = "msg-" + str(uuid.uuid4())

    def base(code, extra=None):
        ev = {"eventCode": code, "id": eid, "name": ename, "expertTitle": ename,
              "type": "agent", "expertType": "agent", "source": "builtin", "version": "",
              "cost": 0, "characterCount": 12, "reportDelay": 0, "requestId": rid,
              "messageId": mid, "conversationId": conv,
              "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2",
              "timestamp": now, "userId": uid}
        if activity_id:
            ev["activityId"] = getattr(mod, "SCHOOL_ACTIVITY_ID", "school_open_day_2026")
        if extra:
            ev.update(extra)
        return ev

    if camel:
        evs = [base("ExpertActualUse")]
    else:
        evs = [base("expert_summoned"), base("expert_actual_use")]
    if with_chat:
        evs.append(base("chat_request_send", {
            "source": "mini_program", "ideName": "wx_app_cloud", "ideType": "WorkBuddy_MP",
            "mode": "chat", "inputLength": 12, "agentType": "expert"}))
    return evs


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    variant = (sys.argv[2] if len(sys.argv) > 2 else "A1").upper()
    mod, accounts = load()

    if which == "status":
        print("=" * 78)
        print("expert_use 全账号状态（只读）")
        print("=" * 78)
        for i, a in enumerate(accounts, 1):
            try:
                sc = mod._school_session(a["at"])
                tasks, _ = mod._school_fetch_tasks(sc)
                t = next((x for x in tasks if x.get("task_code") == CODE), None)
                print("账号%d %-16s %s %s/%s"
                      % (i, a.get("nick", "?")[:14], (t or {}).get("status"),
                         (t or {}).get("progress"), (t or {}).get("target_count")))
            except Exception as exc:  # noqa: BLE001
                print("账号%d 查询失败 %s" % (i, type(exc).__name__))
        return 0

    if which != "try":
        print("未知命令：%s（可用：status / try <变体>）" % which)
        return 2

    acc = accounts[0]
    at = (acc.get("at") or "").strip()
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    sc = mod._school_session(at)
    D = mod.SCHOOL_DOMAIN
    base_s = mod.new_api(at)

    print("=" * 78)
    print("账号1 %s —— 复跑变体 %s" % (nick, variant))
    print("=" * 78)

    def prog():
        try:
            tasks, _ = mod._school_fetch_tasks(sc)
            t = next((x for x in tasks if x.get("task_code") == CODE), None)
            return (t or {}).get("status"), (t or {}).get("progress")
        except Exception:  # noqa: BLE001
            return None, None

    eid = ename = ""
    try:
        r = mod._school_post(sc, D + "/v2/operation-platform/market/expert/list",
                             {"page": 1, "page_size": 10,
                              "categories": [mod.SCHOOL_EXPERT_CATEGORY], "expert_type": "agent"})
        ex = (r.json().get("data") or {}).get("experts") or []
        e0 = ex[0] if ex else {}
        eid = e0.get("expert_id") or ""
        ename = e0.get("display_name_zh") or ""
    except Exception:  # noqa: BLE001
        pass
    print("起始: %s   专家: %r (%r)" % (prog(), ename, eid))
    if not eid:
        return 1
    try:
        mod._school_viewed(sc, CODE)
    except Exception:  # noqa: BLE001
        pass

    if variant == "B1":
        evs = make_events(mod, uid, eid, ename, activity_id=False)
        http = mod.report(base_s, uid, nick, [
            {"eventCode": "expert_summoned", "id": eid, "name": ename, "type": "agent",
             "expertTitle": ename, "expertType": "agent"},
            {"eventCode": "expert_actual_use", "id": eid, "name": ename, "type": "",
             "expertType": "agent", "source": "builtin", "version": "", "cost": 0,
             "characterCount": 12, "conversationId": "conv-" + str(uuid.uuid4()),
             "requestId": str(uuid.uuid4()), "messageId": "msg-" + str(uuid.uuid4()),
             "requestModelId": "deepseek-v4-flash", "requestModelName": "DeepSeek V4 Flash"}])
        print("往 BASE 发 %d 条 → %s" % (len(evs), getattr(http, "status_code", "?")))
    elif variant == "E1":
        conv = sc.post(D + "/console/webchat/conversations",
                       json={"name": "expert-" + str(uuid.uuid4())[:8]},
                       timeout=20, verify=False).json()
        cid = (conv.get("data") or {}).get("conversationId", "")
        prompt = "你好，请简单介绍一下你能帮我做什么，回答OK即可"
        req = str(uuid.uuid4())
        ge = make_events(mod, uid, eid, ename)
        meta = {"codebuddy.ai": {"growthEvent": json.dumps(ge, ensure_ascii=False),
                                 "promptRequestId": req,
                                 "clientSendTime": int(time.time() * 1000),
                                 "userId": uid, "mode": "craft", "model": "glm-5.2",
                                 "expertId": eid, "tags": ["expert:" + eid]}}
        hd = dict(sc.headers)
        hd["Accept"] = "text/event-stream"
        r = sc.post(D + "/console/chat/completions",
                    json={"messages": [{"role": "user", "content": prompt}],
                          "model": "glm-5.2", "stream": True,
                          "conversationId": cid, "_meta": meta},
                    timeout=90, verify=False, stream=True, headers=hd)
        n = 0
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data: ") and line[6:].strip() not in ("[DONE]", ""):
                n += 1
        print("开学季域真实对话完成 conv=%s 收 %d 个 SSE 分片" % (cid, n))
    else:
        hd = {}
        if variant == "C1":
            hd = {"X-Client-Platform": "miniprogram"}
        camel = variant in ("A3",)
        with_chat = variant in ("A5", "A6")
        evs = make_events(mod, uid, eid, ename, camel=camel, with_chat=with_chat)
        if variant == "A6":
            for e in evs:
                e["expertId"] = eid
                e["expertName"] = ename
        r = sc.post(D + "/v2/report", json=evs, timeout=25, verify=False, headers=hd)
        print("发 %d 条到 codebuddy.cn → HTTP %s %s"
              % (len(evs), r.status_code, r.text[:90].replace("\n", " ")))

    for w in (4, 8, 12):
        time.sleep(w)
        st, cur = prog()
        print("   +%2ds 复查: %s %s/1" % (w, st, cur))
        if st in ("completed", "claimed"):
            print("   ✅ 成功！")
            if st == "completed":
                print("   🎁 领奖 → %s" % mod._school_claim(sc, CODE))
            return 0
    print("   ❌ 未点亮（预期如此，见文件头结论）")
    return 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
