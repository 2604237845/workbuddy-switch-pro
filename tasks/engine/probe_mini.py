#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：小程序类任务（Sequential_Tasks_1 / school_season）到底卡在哪。

背景（2026-09-20）：
    日志长期是「mini chat 已上报」但「已接受 0/1（服务端暂未关联）」。
    同样症状（埋点发了、进度不动）在**桌面任务**上已被证实是**报文形状**问题：
      · 上游 `report()`               发**裸数组**            → 服务端认
      · 上游 `report_desktop_events()` 发 `{"common":...,"events":[...]}` **信封** → HTTP 200 但不计分
    `engine.py` 的 `apply_report_shape_fix` / `apply_web_report_fix` 修了桌面 + 资料库两处，
    **但 `_mini_report`（上游 1588）和校园日那处（上游 1664）一直是信封，没人修过。**

用法：
    python probe_mini.py status          # 只读：列出所有账号这两个任务的状态
    python probe_mini.py try [账号序号]  # 写入：用裸数组形状重发一次，看进度是否翻

⚠️ `try` 会真实发包（就等于跑一次该账号的小程序任务），只读盘不影响其它账号。
   只读 `~/.wb-switch/accounts.json` 的 AT，**绝不刷新 token**。
"""
import sys
import time
import uuid
from pathlib import Path

HUB = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB))

import engine as E  # noqa: E402

CODES = ("Sequential_Tasks_1", "school_season")


def load():
    """返回 (mod, accounts)。"""
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


def mp_prog(mod, s, code):
    """带 miniprogram 头查任务状态；查不到就返回 (None, None, None)。"""
    try:
        r = s.get(mod.BASE + "/v2/activity/growth/tasks", timeout=25, verify=False,
                  headers=mod.MP_HEADER)
        for t in r.json().get("data", {}).get("tasks", []):
            if t.get("task_code") == code:
                pr = t.get("progress") or {}
                return t.get("accept_status", ""), pr.get("current"), pr.get("target")
    except Exception as exc:  # noqa: BLE001
        return ("ERR:" + type(exc).__name__, None, None)
    return (None, None, None)


def cmd_status(mod, accounts):
    print("=" * 78)
    print("小程序类任务现状（mp 口径查询，只读）")
    print("=" * 78)
    for i, acc in enumerate(accounts, 1):
        at = (acc.get("at") or "").strip()
        if not at:
            continue
        try:
            s = mod.new_api(at)
        except Exception as exc:  # noqa: BLE001
            print("账号%-2d %-16s 建会话失败 %s" % (i, acc.get("nick", "?"), type(exc).__name__))
            continue
        cells = []
        for code in CODES:
            st, cur, tgt = mp_prog(mod, s, code)
            if st is None:
                cells.append("%s=未下发" % code)
            else:
                cells.append("%s=%s %s/%s" % (code, st, cur, tgt))
        print("账号%-2d %-16s %s" % (i, acc.get("nick", "?"), " | ".join(cells)))
    print()
    print("说明：`未下发` = 服务端认为该任务不适用这个账号（不是失败）。")
    return 0


def build_report(mod, uid, nick, conv_id):
    """把小程序 chat_request_send 事件拼成一个**裸数组**（服务端要的形状）。"""
    rid = str(uuid.uuid4())
    return [{
        "eventCode": "chat_request_send", "timestamp": int(time.time() * 1000),
        "reportDelay": 0, "source": "mini_program", "ideName": "wx_app_cloud",
        "ideType": "WorkBuddy_MP", "extName": "workbuddy-mp", "extVersion": "2.4.0",
        "mode": "chat", "conversationId": conv_id, "requestId": rid,
        "inputLength": 12, "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2",
        "isPlan": False, "codebaseEnable": False, "maxToken": 0, "maxSteps": 0,
        "temperature": 0, "mentionContexts": [], "knowledgeId": [],
        "agentName": "default", "agentType": "conversation", "userId": uid,
    }]


def mp_post(mod, s, uid, nick, body):
    """用 mp 头把 body 原样 POST 到 /v2/report；返回 (status, 文本前 200 字)。"""
    import requests
    mp_s = requests.Session()
    mp_s.trust_env = False
    auth = s.headers.get("Authorization", "")
    mp_s.headers.update({
        "Authorization": auth if auth.startswith("Bearer ") else "Bearer " + auth,
        "Content-Type": "application/json", "Accept": "application/json",
        "X-Client-Platform": "miniprogram",
        "User-Agent": mod.MP_UA,
    })
    try:
        r = mp_s.post("https://copilot.tencent.com/v2/report", json=body,
                      timeout=20, verify=False)
        return r.status_code, r.text[:200]
    except Exception as exc:  # noqa: BLE001
        return 0, "%s: %s" % (type(exc).__name__, exc)


def cmd_try(mod, accounts, idx):
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    acc = accounts[idx - 1]
    at = (acc.get("at") or "").strip()
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = mod.new_api(at)
    print("=" * 78)
    print("测试账号%d %s" % (idx, nick))
    print("=" * 78)

    for code in CODES:
        st, cur, tgt = mp_prog(mod, s, code)
        print("\n【%s】起始: %s %s/%s" % (code, st, cur, tgt))
        if st is None:
            print("   服务端没下发这个任务 → 跳过")
            continue
        if st in ("completed", "claimed"):
            print("   已经是 %s → 无需处理" % st)
            continue
        if st == "not_accepted":
            ok = mod._mp_accept(s, code)
            print("   accept → %s" % ("成功" if ok else "失败"))
            if not ok:
                continue
            time.sleep(2)

        conv = "mini-" + str(uuid.uuid4())
        body = build_report(mod, uid, nick, conv)
        print("   发裸数组（1 条事件，含 timestamp/reportDelay）...")
        code_http, txt = mp_post(mod, s, uid, nick, body)
        print("   HTTP %s  %s" % (code_http, txt.replace("\n", " ")[:150]))

        for wait in (3, 6, 12):
            time.sleep(wait)
            st2, cur2, tgt2 = mp_prog(mod, s, code)
            print("   +%2ds 复查: %s %s/%s" % (wait, st2, cur2, tgt2))
            if st2 in ("completed", "claimed"):
                print("   ✅ 翻过去了！裸数组形状是对的")
                if st2 == "completed":
                    mod._mp_claim(s, code, print)
                break
    return 0


def cmd_patched(mod, accounts, idx):
    """打上**生产补丁**（E.apply_mini_report_fix）后调上游原函数，验证真实代码路径。

    与 cmd_try 的区别：cmd_try 用的是本文件自己拼的报文，只能证明「形状对了」；
    这里用的是 engine.py 里那份补丁 + 上游 `t_sequential_tasks` / `t_school_season`，
    证明的是**生产链路真的通了**。
    """
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    acc = accounts[idx - 1]
    at = (acc.get("at") or "").strip()
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = mod.new_api(at)

    applied = E.apply_mini_report_fix(mod, {})
    print("=" * 78)
    print("账号%d %s" % (idx, nick))
    print("补丁已应用：%s" % (", ".join(applied) if applied else "（无）"))
    print("=" * 78)

    for code in CODES:
        st, cur, tgt = mp_prog(mod, s, code)
        print("【%s】起始: %s %s/%s" % (code, st, cur, tgt))

    print("\n--- 调上游 t_sequential_tasks（内部会走补丁后的 _mini_report）---")
    try:
        mod.t_sequential_tasks(s, uid, nick, print)
    except Exception as exc:  # noqa: BLE001
        print("   抛异常: %s: %s" % (type(exc).__name__, exc))

    print("\n--- 调补丁版 t_school_season ---")
    try:
        mod.t_school_season(s, uid, nick, print)
    except Exception as exc:  # noqa: BLE001
        print("   抛异常: %s: %s" % (type(exc).__name__, exc))

    print("\n--- 最终状态 ---")
    for code in CODES:
        st, cur, tgt = mp_prog(mod, s, code)
        mark = "✅" if st in ("completed", "claimed") else "❌"
        print("   %s %s: %s %s/%s" % (mark, code, st, cur, tgt))
    return 0


def main():
    which = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    mod, accounts = load()
    print("账号总数：%d" % len(accounts))
    if which == "status":
        return cmd_status(mod, accounts)
    if which == "try":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        return cmd_try(mod, accounts, idx)
    if which == "patched":
        idx = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        return cmd_patched(mod, accounts, idx)
    print("未知命令：%s（可用：status / try [账号序号] / patched [账号序号]）" % which)
    return 2


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
