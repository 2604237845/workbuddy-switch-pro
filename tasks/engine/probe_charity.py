#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""诊断：体验「公益专家」(Expert_Philanthropy) 能不能不做捐款就点亮

用户 2026-09-20 指出：这个任务的要点是「召唤公益专家 + 对话」，
不是必须真捐款（服务端 task_desc 里那句"完成1次捐款"具有误导性）。

用的就是已经验证成功的那套（skill_1 靠它从 accepted 0/1 变 completed 1/1）：
    真实 POST /console/chat/completions，并把成长埋点塞进
    请求的 _meta["codebuddy.ai"]["growthEvent"]。

用法：
    python probe_charity.py [账号序号，默认 5]
"""
import json
import sys
import time
import uuid
from pathlib import Path

HUB = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB))

import engine as E  # noqa: E402

TASK = "Expert_Philanthropy"

# marketplace 里所有「公益/慈善」相关的专家（categoryId=13-TencentZone 优先）
CANDIDATES = [
    {"id": "TencentCharityExpert", "name": "小益", "profession": "腾讯公益", "expertType": "agent"},
    {"id": "CharityDocFinanceExpert", "name": "小益", "profession": "公益文档财务", "expertType": "agent"},
]


def show(mod, s, note):
    st, cur, tgt = mod.prog(s, TASK)
    print("   %s → accept_status=%s progress=%s/%s" % (note, st, cur, tgt))
    return st


def main():
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    cfg = E.load_config()
    accounts, _ = E.read_switch_accounts(cfg)
    E.write_token_file(accounts)
    sys.argv = ["workbuddy_daily"]
    mod = E.load_upstream()

    acc = accounts[idx - 1]
    at = acc["at"]
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = mod.new_api(at)

    print("=" * 72)
    print("账号%d %s" % (idx, acc["nick"]))
    print("=" * 72)
    st0 = show(mod, s, "初始")
    if st0 in ("completed", "claimed"):
        print("已经是完成态，无需测试")
        return 0

    for c in CANDIDATES:
        print("\n▶ 召唤 %s（%s, %s）并真实对话" % (c["id"], c["name"], c["profession"]))
        rid = str(uuid.uuid4())
        prompt = "你好，我想了解一下公益项目"
        ge = [{"eventCode": "ExpertActualUse", "id": c["id"],
               "extra": {"name": c["name"], "expertTitle": c["profession"],
                         "type": "", "expertType": c["expertType"],
                         "source": "builtin", "version": "", "cost": 8,
                         "characterCount": len(prompt), "requestId": rid,
                         "messageId": "cmb-" + str(uuid.uuid4()),
                         "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2"},
               "expertType": c["expertType"]}]
        meta = {"codebuddy.ai": {
            "growthEvent": json.dumps(ge, ensure_ascii=False),
            "promptRequestId": rid,
            "clientSendTime": int(time.time() * 1000),
            "userId": uid, "mode": "craft", "model": "glm-5.2",
            "expertId": c["id"],
            "expert": {"id": c["id"], "name": c["name"],
                       "profession": c["profession"], "prompt": prompt[:50]},
            "tags": ["expert:" + c["id"]]}}
        conv_id, txt = mod.webchat(s, "charity", prompt, meta)
        print("   真实对话 conversationId=%s 回复%d字" % (conv_id or "(空)", len(txt or "")))

        mod.report(s, uid, nick, [
            {"eventCode": "expert_summoned", "id": c["id"], "name": c["name"],
             "type": "", "expertTitle": c["profession"], "expertType": c["expertType"],
             "source": "builtin"},
            {"eventCode": "expert_actual_use", "id": c["id"], "name": c["name"],
             "expertTitle": c["profession"], "type": "", "expertType": c["expertType"],
             "source": "builtin", "version": "", "cost": 8, "characterCount": len(prompt),
             "conversationId": conv_id, "requestId": rid,
             "messageId": "cmb-" + str(uuid.uuid4()),
             "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2"}])
        time.sleep(6)
        st = show(mod, s, "对话后")
        if st in ("completed", "claimed"):
            print("\n✅ 点亮了！这个专家 id 是对的：%s" % c["id"])
            return 0
        print("   ❌ 没动")

    print("\n" + "=" * 72)
    print("两个候选都没推动 → 这个任务不是「召唤+对话」就能算的")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
