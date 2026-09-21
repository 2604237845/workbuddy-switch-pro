#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""RichMeow_Chat（桌面端对话1次）定向探针 —— 阶梯式单变量实验。

背景（2026-09-20 深夜）：
  用户实测：**在桌面端 WorkBuddy 里真发一条「你好」、等回复回来，任务就完成了**
  → 所以这项的判定靠的是"真实桌面端对话"产生的埋点链。

  引擎当前的 6 连（`desktop_chat_sequence` + `apply_report_shape_fix`）**点不亮**，
  且已定位到一个具体缺陷：
      上游本意是 `report_desktop_events()` 给每条事件**注入 `desktop_fingerprint(uid,nick)`**，
      但 `engine.apply_report_shape_fix()` 把该函数**整个替换成裸数组版本**，
      替换时**没有把 fingerprint 注入回来** →
      **RichMeow 的 6 连是在"没有任何客户端身份字段"的情况下发出去的**
      （与 `expert_use` 当年的失败原因同一类）。

本探针按"一次只加一个变量"的阶梯设计（在每个账号上顺序试，失败不消耗账号）：

  arm0  现状基线：6 连 + 仅 timestamp/reportDelay/userId/userNickname（= 引擎现在发的）
  arm1  + 上游 desktop_fingerprint 信封（ideType/ideName=WorkBuddy、extName=workbuddy-desktop、
        product/commit/releaseDate/os/arch/osVersion/cpuCores/memorySize/timezone），machineId 仍为派生假值
  arm2  + 你自己的 machineId（见下方「设备指纹」说明；未配置则本臂退化为 arm1）
  arm3  + 你自己的 qimei36 + 真实机器画像（未配置则本臂部分字段沿用 arm1 的默认值）

用法：
    python probe_richmeow.py status <账号序号1-7>
    python probe_richmeow.py arms   <账号序号1-7> [--max-arm N] [--gap 4]
    python probe_richmeow.py arm    <账号序号1-7> <臂号0-3>

⚠️ 只做「上报 + 回读 + 达标即领奖」，**不碰 vendor / 不改 engine.py / 不动桌面端进程**。
"""
import json
import os
import ssl
import sys
import time
import uuid

import requests

ssl._create_default_https_context = ssl._create_unverified_context

ACCOUNTS = os.path.expanduser("~/.wb-switch/accounts.json")
TASK_CODE = "RichMeow_Chat"

# —— 设备指纹：**请填你自己的值** ——
# 🔴 开源版刻意留空：这些是**每台机器独有**的标识，属于个人设备信息，不该随仓库分发。
#    也不要照抄别人的值 —— 服务端那边它代表"这台设备"，抄了会串号。
#
# 怎么取到自己的值：
#   客户端本地遥测日志里就有真实报文：
#     ~/.workbuddy/logs/<YYYY-MM-DD>/*.log
#     找 `[TelemetryDebug] report code=chat_request_send payload={…}` 这一行，
#     里面的 machineId / qimei36 / os / osVersion / cpuModel / cpuCores / memorySize 直接抄。
#
# 两种填法（环境变量优先，便于不改文件就跑）：
#   set WB_PROBE_MACHINE_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   set WB_PROBE_QIMEI36=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# 只想验证 arm0 / arm1（不需要设备值）时，什么都不用配。
def _env(name, default=""):
    v = os.environ.get(name)
    return v if v not in (None, "") else default


REAL_MACHINE_ID = _env("WB_PROBE_MACHINE_ID")
REAL_QIMEI36 = _env("WB_PROBE_QIMEI36")
REAL_OS = _env("WB_PROBE_OS", "win32")
REAL_ARCH = _env("WB_PROBE_ARCH", "x64")
REAL_OS_VERSION = _env("WB_PROBE_OS_VERSION")
REAL_CPU_MODEL = _env("WB_PROBE_CPU_MODEL")
REAL_CPU_CORES = int(_env("WB_PROBE_CPU_CORES", "8"))
REAL_MEMORY_SIZE = int(_env("WB_PROBE_MEMORY_SIZE", "16"))
REAL_COMMIT = _env("WB_PROBE_COMMIT")
REAL_RELEASE_DATE = int(_env("WB_PROBE_RELEASE_DATE", "1788443875561"))
# 桌面端版本号：客户端升级后会变（可用 WB_PROBE_IDE_VERSION 覆盖，免得改代码）
IDE_VERSION = _env("WB_PROBE_IDE_VERSION", "5.5.6")


def load_account(idx):
    d = json.load(open(ACCOUNTS, encoding="utf-8"))
    accs = d if isinstance(d, list) else (d.get("accounts") or d.get("data") or [])
    if not (1 <= idx <= len(accs)):
        raise SystemExit("账号序号 %s 超出范围（1-%s）" % (idx, len(accs)))
    a = accs[idx - 1]
    tok = a.get("access_token") or (a.get("auth_raw") or {}).get("auth", {}).get("accessToken")
    dom = a.get("domain") or "www.codebuddy.cn"
    uid = a.get("uid") or ((a.get("profile_raw") or {}).get("uid"))
    if not tok or not uid:
        raise SystemExit("账号 %s 缺 AT 或 uid，无法使用" % idx)
    s = requests.Session()
    s.trust_env = False  # 绕开环境里的 http_proxy
    s.headers.update({
        "Authorization": "Bearer " + tok,
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://" + dom,
        "Referer": "https://" + dom + "/profile/growth-center",
        "x-client-platform": "web",
        # 浏览器侧 UA（成长中心是网页；网页与桌面端是同一套 /v2/report）
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36 Edg/151.0.0.0"),
    })
    return s, "https://" + dom, uid, a.get("nickname") or ""


def read_progress(s, base):
    """回读真实进度 —— 唯一判据。"""
    r = s.get(base + "/v2/activity/growth/tasks", timeout=25)
    data = r.json().get("data") or {}
    for t in data.get("tasks") or []:
        if t.get("task_code") == TASK_CODE:
            pr = t.get("progress") or {}
            return t.get("accept_status"), pr.get("current"), pr.get("target")
    return None, None, None


def claim_if_done(s, base):
    st, cur, tgt = read_progress(s, base)
    if st in ("completed", "claimed"):
        r = s.post(base + "/activity/growth/tasks/%s/claim" % TASK_CODE, json={}, timeout=20)
        try:
            dd = r.json().get("data") or {}
            extra = "已领过" if dd.get("already_claimed") else "+%s积分+%s能量" % (
                dd.get("credit"), dd.get("energy"))
        except Exception:
            extra = "HTTP %s" % r.status_code
        return st, "   🎁 领奖: " + extra
    return st, ""


def build_events(uid, nick, arm):
    """6 连事件链；arm 越高，信封越接近真实桌面端。"""
    now = int(time.time() * 1000)
    conv = "rm-%s" % uuid.uuid4()
    req = uuid.uuid4().hex
    msg = "rm-%s" % uuid.uuid4()
    model_id, model_name = "custom-local:BC-Hy4-Preview", "BC-Hy4-Preview"

    def mk(code, extra):
        e = {"eventCode": code}
        e.update(extra)
        return e

    evs = [
        mk("agent_task_created", {
            "source": "LOCAL", "name": "working", "task_target": "local", "mode": "craft",
            "requestModelId": model_id, "requestModelName": model_name,
            "has_repo": False, "repo_type": "none", "workspace_type": "empty",
            "has_connector": False, "connector_types": [],
            "has_mention": False, "mention_types": [],
            "has_template": False, "action": "", "template_name": "",
            "has_expert": False, "expert_id": "", "expert_name": "", "expert_industry_id": "",
            "has_skill": False, "skill_names": [],
            "conversationId": conv, "messageId": msg,
            "buddyId": "", "buddyName": ""}),
        mk("chat_message_send", {
            "messageId": msg + "-assistant", "historyCount": 0,
            "isContextTruncated": False, "currentStepCount": 1,
            "traceId": req, "rootRequestId": req, "parentConversationId": conv,
            "conversationId": conv,
            "agentName": "cli", "agentType": "main"}),
        mk("chat_request_send", {
            "mode": "craft", "conversationId": conv,
            "requestId": req, "inputLength": 2,
            "requestModelId": model_id, "requestModelName": model_name,
            "isPlan": False, "isAutoExecuteTerminal": False,
            "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
            "maxSteps": 500, "temperature": 0, "maxRetries": 0,
            "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
            "codebaseId": "", "mentionContextCount": 0, "command": "",
            "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
            "traceId": req, "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main"}),
        mk("chat_message_response", {
            "messageId": msg + "-assistant", "responseModelId": model_id,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "firstTokenAt": now, "traceId": req, "conversationId": conv,
            "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main"}),
        mk("chat_message_status", {
            "messageId": msg + "-assistant", "messageErrorCode": "0",
            "traceId": req, "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main"}),
        mk("chat_request_response", {
            "mode": "craft", "toolCallCount": 0,
            "inputToken": 120, "outputToken": 80, "totalToken": 200,
            "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
            "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
            "rootRequestId": req, "parentConversationId": conv,
            "agentName": "cli", "agentType": "main"}),
    ]

    # —— 逐级加变量 ——
    env = {}
    if arm >= 1:  # 上游 desktop_fingerprint 那套信封（引擎补丁把它丢了）
        env.update({
            "timezone": "Asia/Shanghai",
            "userId": uid, "username": nick, "userNickname": nick,
            "product": "SaaS", "releaseDate": REAL_RELEASE_DATE, "commit": REAL_COMMIT,
            "ideName": "WorkBuddy", "ideType": "WorkBuddy", "ideVersion": IDE_VERSION,
            "machineId": "fp-%s" % uuid.uuid5(uuid.NAMESPACE_DNS, uid).hex[:16],  # arm1: 派生假值
            "sessionId": str(uuid.uuid4()),
            "extName": "workbuddy-desktop", "extVersion": IDE_VERSION,
            "os": "win32", "arch": "x64", "osVersion": "10.0.26220",
            "cpuCores": 20, "memorySize": 24,
        })
    if arm >= 2 and REAL_MACHINE_ID:  # 换成你自己的 machineId（未配置则保持 arm1 的派生假值）
        env["machineId"] = REAL_MACHINE_ID
    if arm >= 3:  # 你自己的 qimei36 + 真实机器画像（未配置的字段保持 arm1 的默认值）
        if REAL_QIMEI36:
            env["qimei36"] = REAL_QIMEI36
        if REAL_OS:
            env["os"] = REAL_OS
        if REAL_ARCH:
            env["arch"] = REAL_ARCH
        if REAL_OS_VERSION:
            env["osVersion"] = REAL_OS_VERSION
        if REAL_CPU_MODEL:
            env["cpuModel"] = REAL_CPU_MODEL
        env["cpuCores"] = REAL_CPU_CORES
        env["memorySize"] = REAL_MEMORY_SIZE

    now = int(time.time() * 1000)
    out = []
    for i, e in enumerate(evs):
        m = dict(e)
        m.setdefault("timestamp", now + i * 120)
        m.setdefault("reportDelay", 0)
        m.setdefault("userId", uid)
        m.setdefault("userNickname", nick)
        m.update(env)  # 信封覆盖同名业务键（与上游 desktop_fingerprint 语义一致）
        out.append(m)
    return out


ARM_DESC = {
    0: "现状基线（无任何身份字段）",
    1: "+ 上游 desktop_fingerprint 信封（假 machineId）",
    2: "+ 真 machineId",
    3: "+ 真 qimei36 + 真机器画像",
}


def send(s, base, evs):
    r = s.post(base + "/v2/report", data=json.dumps(evs, ensure_ascii=False).encode("utf-8"),
               timeout=20)
    return r.status_code, (r.text or "")[:300]


def run_arm(s, base, uid, nick, arm, gap=4, tries=5):
    st0, cur0, tgt0 = read_progress(s, base)
    code, body = send(s, base, build_events(uid, nick, arm))
    print("   arm%d %s" % (arm, ARM_DESC[arm]))
    print("      上报 → HTTP %s %s" % (code, body.replace("\n", " ")[:160]))
    st, cur, tgt = st0, cur0, tgt0
    for _ in range(tries):
        time.sleep(gap)
        st, cur, tgt = read_progress(s, base)
        print("      回读 → %s %s/%s" % (st, cur, tgt))
        if st in ("completed", "claimed"):
            break
    if st in ("completed", "claimed"):
        st2, note = claim_if_done(s, base)
        print("   ✅ arm%d 生效！最终 %s %s" % (arm, st2, note))
        return True
    return False


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return
    cmd, idx = sys.argv[1], int(sys.argv[2])
    gap = 4
    max_arm = 3
    if "--gap" in sys.argv:
        gap = int(sys.argv[sys.argv.index("--gap") + 1])
    if "--max-arm" in sys.argv:
        max_arm = int(sys.argv[sys.argv.index("--max-arm") + 1])

    s, base, uid, nick = load_account(idx)
    print("=" * 66)
    print("账号%d  %s   uid=%s…   目标=%s" % (idx, nick, uid[:8], TASK_CODE))
    print("=" * 66)
    st, cur, tgt = read_progress(s, base)
    print("当前进度：%s %s/%s" % (st, cur, tgt))
    if cmd == "status":
        return
    if st in ("completed", "claimed"):
        print("已完成，无需处理（如需领奖用 arms 亦可）。")
        st2, note = claim_if_done(s, base)
        print("领奖：%s %s" % (st2, note))
        return

    arms = [int(sys.argv[3])] if cmd == "arm" else list(range(max_arm + 1))
    for arm in arms:
        if run_arm(s, base, uid, nick, arm, gap=gap):
            return
    print("\n❌ 全部臂都没点亮 —— 说明门槛可能不在「报文形状/身份信封」层面"
          "（例如服务端会校验该 conversationId 是否真实存在）。")


if __name__ == "__main__":
    main()
