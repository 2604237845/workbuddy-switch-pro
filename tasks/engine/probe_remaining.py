#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：把「点不亮」的任务改成「真实对话 + growthEvent 搭车」再试

依据（从客户端 app.asar 挖出来的）：
    能点亮的任务（t_team_3 / t_black_cat）都是：真实 POST /console/chat/completions，
    并把成长埋点塞进请求的 _meta["codebuddy.ai"]["growthEvent"]。
    客户端侧就是 parseGrowthEvents(meta.growthEvent)，meta 支持 codebuddy.ai 嵌套形式。
    只打 /v2/report 的任务（skill_1 / Buddy_App / Library_read / Expert_lighthouse）
    一律不给进度。

已实测结果：
    skill_1（尝鲜热门技能）：✅ accepted 0/1 → completed 1/1
    Buddy_App / Buddy_App_QQ：❌ 用瞎猜的事件码不行，改用上游那串真实事件链再试

用法：
    python probe_remaining.py skill|buddy|richmeow|library|all
"""
import json
import sys
import time
import uuid
from pathlib import Path

HUB = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB))

import engine as E  # noqa: E402

SKILL_NAME = "algorithmic-trading"


def meta_for(growth_events, uid, extra=None):
    """按 t_team_3 的成熟结构拼 meta。"""
    rid = str(uuid.uuid4())
    payload = {"codebuddy.ai": {
        "growthEvent": json.dumps(growth_events, ensure_ascii=False),
        "promptRequestId": rid,
        "clientSendTime": int(time.time() * 1000),
        "userId": uid,
        "mode": "craft",
        "model": "glm-5.2",
    }}
    if extra:
        payload["codebuddy.ai"].update(extra)
    return payload, rid


def _done(mod, s, code, before):
    st2, cur2, tgt2 = mod.prog(s, code)
    ok = st2 in ("completed", "claimed") or (cur2 or 0) > (before or 0)
    print("   %s: %s %s/%s → %s" % (code, st2, cur2, tgt2, "✅ 点亮" if ok else "❌ 没动"))
    return ok


def try_skill(mod, s, uid, nick):
    code = "skill_1"
    st, cur, tgt = mod.prog(s, code)
    print("【%s 尝鲜热门技能】当前: %s %s/%s" % (code, st, cur, tgt))
    if st in ("completed", "claimed"):
        return True
    skill_id = ""
    try:
        r = s.post(mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/skill/list",
                   json={"page": 1, "page_size": 10}, timeout=20, verify=False).json()
        skills = (r.get("data") or {}).get("skills") or []
        skill_id = next((k.get("id") for k in skills if SKILL_NAME in str(k.get("name", ""))), "")
    except Exception as e:
        print("   取技能 id 失败(忽略): %s" % str(e)[:70])
    if not skill_id:
        skill_id = "skill-" + mod.derive_id(uid, "skill")[:12]
    ge = [{"eventCode": "skill_info", "skillId": skill_id, "skillName": SKILL_NAME,
           "reportDelay": 0, "mode": "CLOUD", "source": "builtin", "userId": uid}]
    meta, rid = meta_for(ge, uid, {"skillId": skill_id, "skillName": SKILL_NAME,
                                   "tags": ["skill:" + SKILL_NAME]})
    conv_id, txt = mod.webchat(s, "skill", "请通过技能系统正式加载 %s 技能，然后回答：技能已加载" % SKILL_NAME, meta)
    print("   真实对话 conversationId=%s 回复%d字" % (conv_id or "(空)", len(txt or "")))
    mod.report(s, uid, nick, [{"eventCode": "skill_info", "skillId": skill_id,
                               "skillName": SKILL_NAME, "conversationId": conv_id,
                               "requestId": rid, "source": "builtin",
                               "mode": "CLOUD", "userId": uid}])
    time.sleep(6)
    return _done(mod, s, code, cur)


def try_buddy(mod, s, uid, nick):
    """用上游那串真实 Buddy 五连事件，搭在真实对话上。"""
    any_ok = False
    for task, bid, bname in (("Buddy_App", "buddy-app-default", "发现应用"),
                             ("Buddy_App_QQ", mod.QQ_TPL, "企鹅教师助手")):
        st, cur, tgt = mod.prog(s, task)
        print("【%s】当前: %s %s/%s" % (task, st, cur, tgt))
        if st in ("completed", "claimed"):
            any_ok = True
            continue
        for tag, mode in (("原样(LOCAL)", None), ("改成CLOUD", "CLOUD")):
            evs = mod.desktop_buddy5_sequence(uid, nick, bid, bname)
            if mode:
                for e in evs:
                    e["mode"] = mode
            meta, _rid = meta_for(evs, uid, {"buddyId": bid, "buddyName": bname})
            conv_id, txt = mod.webchat(s, "buddy", "你好", meta)
            print("   [%s] 真实对话 %s 回复%d字" % (tag, conv_id or "(空)", len(txt or "")))
            time.sleep(6)
            if _done(mod, s, task, cur):
                any_ok = True
                break
    return any_ok


def try_richmeow(mod, s, uid, nick):
    """桌面端对话：用 desktop_chat_sequence 六连事件搭在真实对话上。"""
    code = "RichMeow_Chat"
    st, cur, tgt = mod.prog(s, code)
    print("【%s 桌面端对话】当前: %s %s/%s" % (code, st, cur, tgt))
    if st in ("completed", "claimed"):
        return True
    evs = mod.desktop_chat_sequence(uid, nick, "conv-" + str(uuid.uuid4()),
                                    str(uuid.uuid4()), str(uuid.uuid4()))
    meta, _rid = meta_for(evs, uid, None)
    conv_id, txt = mod.webchat(s, "richmeow", "你好，请用一句话介绍你自己", meta)
    print("   真实对话 conversationId=%s 回复%d字（事件链 %d 条）"
          % (conv_id or "(空)", len(txt or ""), len(evs)))
    time.sleep(8)
    return _done(mod, s, code, cur)


def try_library(mod, s, uid, nick):
    """资料库：把 web_element_click 搭在真实请求上试一把。"""
    code = "Library_read"
    st, cur, tgt = mod.prog(s, code)
    print("【%s 体验资料库】当前: %s %s/%s" % (code, st, cur, tgt))
    if st in ("completed", "claimed"):
        return True
    ev = [{"eventCode": "web_element_click", "pageURL": mod.LIB_DOC_URL,
           "elementId": "library_doc_intro_click", "elementName": "WorkBuddy资料库介绍",
           "userId": uid, "reportDelay": 0, "mode": "CLOUD"}]
    meta, _rid = meta_for(ev, uid, {"pageURL": mod.LIB_DOC_URL})
    conv_id, txt = mod.webchat(s, "library", "你好", meta)
    print("   真实对话 conversationId=%s 回复%d字" % (conv_id or "(空)", len(txt or "")))
    time.sleep(6)
    return _done(mod, s, code, cur)


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    cfg = E.load_config()
    accounts, _ = E.read_switch_accounts(cfg)
    E.write_token_file(accounts)
    sys.argv = ["workbuddy_daily"]
    mod = E.load_upstream()

    acc = accounts[4]          # 第 5 个账号（写这个探针时用它验证；换成你自己的序号即可）
    at = acc["at"]
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = mod.new_api(at)
    print("=" * 70)
    print("测试账号：账号5 %s" % acc["nick"])
    print("=" * 70)

    tests = {"skill": try_skill, "buddy": try_buddy,
             "richmeow": try_richmeow, "library": try_library}
    picked = list(tests.values()) if which == "all" else [tests[which]]
    for fn in picked:
        try:
            fn(mod, s, uid, nick)
        except Exception as exc:
            print("   %s 抛异常: %s: %s" % (fn.__name__, type(exc).__name__, str(exc)[:90]))
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
