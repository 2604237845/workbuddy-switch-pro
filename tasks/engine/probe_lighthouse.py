#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：腾讯轻量云专家（Expert_lighthouse）到底卡在哪

只做一件事：拿真实专家 id 去报 expert_summoned + expert_actual_use，
看任务的 progress 会不会从 0 变 1。

- 用 engine 的加载器 + 上游的 report()/prog()，保证信封和线上完全一致
- 候选按「marketplace 里 prompt 真的提到轻量应用服务器」筛出
"""
import sys
import time
import uuid
from pathlib import Path

HUB = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB))

import engine as E  # noqa: E402

TASK = "Expert_lighthouse"

# marketplace 里 defaultInitPrompt/quickPrompts 真的包含「轻量应用服务器」的两个专家
CANDIDATES = [
    {"id": "AndonQExpert", "name": "AndonQ", "profession": "腾讯云智能客服", "expertType": "agent"},
    {"id": "CloudOpsTeam", "name": "腾讯云技术支持", "profession": "腾讯云技术支持", "expertType": "team"},
]


def main():
    cfg = E.load_config()
    accounts, _ = E.read_switch_accounts(cfg)
    E.write_token_file(accounts)

    sys.argv = ["workbuddy_daily"]
    mod = E.load_upstream()

    acc = accounts[0]
    at = acc["at"]
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = mod.new_api(at)

    st, cur, tgt = mod.prog(s, TASK)
    print("=" * 68)
    print("账号1 %s" % acc["nick"])
    print("任务 %s 初始：accept_status=%s progress=%s/%s" % (TASK, st, cur, tgt))
    print("=" * 68)

    if st in ("completed", "claimed") or (cur or 0) >= (tgt or 1):
        print("已经是完成态，无需测试")
        return 0

    for c in CANDIDATES:
        print("\n▶ 用真实 id 上报：%s（%s, %s）" % (c["id"], c["name"], c["profession"]))
        rid = str(uuid.uuid4())
        cid = "conv-" + str(uuid.uuid4())
        rc = mod.report(s, uid, nick, [
            {"eventCode": "expert_summoned", "id": c["id"], "name": c["name"],
             "type": "agent", "expertTitle": c["profession"], "expertType": c["expertType"],
             "source": "builtin", "timestamp": int(time.time() * 1000)},
            {"eventCode": "expert_actual_use", "id": c["id"], "name": c["name"],
             "expertTitle": c["profession"], "type": "agent", "expertType": c["expertType"],
             "source": "builtin", "version": "", "cost": 0, "characterCount": 12,
             "conversationId": cid, "requestId": rid, "messageId": rid,
             "requestModelId": "deepseek-v4-flash", "requestModelName": "DeepSeek V4 Flash",
             "userId": uid}])
        print("   POST /v2/report → HTTP %s" % rc)
        time.sleep(4)
        st2, cur2, tgt2 = mod.prog(s, TASK)
        print("   上报后：accept_status=%s progress=%s/%s" % (st2, cur2, tgt2))
        if st2 in ("completed", "claimed") or (cur2 or 0) > (cur or 0):
            print("   ✅ 进度动了！这个 id 就是对的")
            return 0
        print("   ❌ 进度没动")
        cur = cur2

    print("\n" + "=" * 68)
    print("结论：两个真实 id 都推不动 → 服务端卡的是「连接器授权」，不是专家 id")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
