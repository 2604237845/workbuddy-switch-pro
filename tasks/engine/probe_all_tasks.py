#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""盘点：把服务端下发的**全部任务**拉出来，看清哪些已拿、哪些还差。

用途：回答「除了关注公众号/连接器授权/捐款，还有什么任务？」

用法：
    python probe_all_tasks.py             # 全账号汇总（同 code 合并统计）
    python probe_all_tasks.py <账号号>     # 只看某一个账号的逐条明细

只读 `~/.wb-switch/accounts.json` 的 AT，**绝不刷新 token**，也**不发任何写请求**。
"""
import sys
from collections import OrderedDict
from pathlib import Path

HUB = Path(__file__).resolve().parent
sys.path.insert(0, str(HUB))

import engine as E  # noqa: E402

DONE = ("claimed", "completed")


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


def fetch(mod, s):
    """返回 [(code, title, status, cur, tgt), ...]；失败返回 None。"""
    try:
        r = s.get(mod.BASE + "/v2/activity/growth/tasks", timeout=25, verify=False)
        tasks = (r.json().get("data") or {}).get("tasks") or []
    except Exception as exc:  # noqa: BLE001
        print("   查询失败 %s: %s" % (type(exc).__name__, exc))
        return None
    out = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        pr = t.get("progress") or {}
        out.append((t.get("task_code", ""), t.get("title", ""),
                    t.get("accept_status", ""), pr.get("current"), pr.get("target")))
    return out


def cmd_all(mod, accounts):
    agg = OrderedDict()
    for i, acc in enumerate(accounts, 1):
        at = (acc.get("at") or "").strip()
        if not at:
            continue
        rows = fetch(mod, mod.new_api(at))
        if rows is None:
            continue
        for code, title, st, cur, tgt in rows:
            e = agg.setdefault(code, {"title": title, "done": 0, "total": 0,
                                      "status": {}, "prog": ""})
            e["total"] += 1
            e["status"][st] = e["status"].get(st, 0) + 1
            if st in DONE:
                e["done"] += 1
            if not e["prog"] and cur is not None:
                e["prog"] = "%s/%s" % (cur, tgt)

    print("=" * 96)
    print("服务端下发的全部任务（%d 个账号汇总，按「还没拿满」排前面）" % len(accounts))
    print("=" * 96)
    rows = sorted(agg.items(), key=lambda kv: (kv[1]["done"] == kv[1]["total"], kv[0]))
    for code, e in rows:
        full = e["done"] == e["total"]
        mark = "✅" if full else "❌"
        stxt = " ".join("%s×%d" % (k, v) for k, v in sorted(e["status"].items()))
        print("%s %-28s %-2d/%-2d  %-34s %s"
              % (mark, code, e["done"], e["total"], stxt, e["title"][:30]))
    print()
    print("合计 %d 个任务码；已拿满 %d 个，未拿满 %d 个"
          % (len(agg), sum(1 for e in agg.values() if e["done"] == e["total"]),
             sum(1 for e in agg.values() if e["done"] != e["total"])))
    return 0


def cmd_one(mod, accounts, idx):
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    acc = accounts[idx - 1]
    at = (acc.get("at") or "").strip()
    rows = fetch(mod, mod.new_api(at))
    if rows is None:
        return 1
    print("=" * 96)
    print("账号%d %s —— 逐条明细（%d 条）" % (idx, acc.get("nick", "?"), len(rows)))
    print("=" * 96)
    for code, title, st, cur, tgt in rows:
        mark = "✅" if st in DONE else "❌"
        print("%s %-28s %-14s %-8s %s"
              % (mark, code, st, "%s/%s" % (cur, tgt), title[:34]))
    return 0


def cmd_school(mod, accounts, idx):
    """查开学季活动**自己那一套**任务（跟成长中心是两套不同的清单）。"""
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    acc = accounts[idx - 1]
    at = (acc.get("at") or "").strip()
    try:
        s = mod._school_session(at)
        tasks, in_period = mod._school_fetch_tasks(s)
    except Exception as exc:  # noqa: BLE001
        print("开学季查询失败 %s: %s" % (type(exc).__name__, exc))
        return 1
    print("=" * 96)
    print("账号%d %s —— 开学季活动任务（in_period=%s，%d 条）"
          % (idx, acc.get("nick", "?"), in_period, len(tasks)))
    print("=" * 96)
    modes = getattr(mod, "SCHOOL_TASK_MODES", {})
    for t in tasks:
        code = t.get("task_code", "")
        st = t.get("status", "")
        spec = modes.get(code)
        mark = "✅" if st in DONE else "❌"
        known = spec.get("note", "") if spec else "⚠️ 脚本不认识的类型（会被静默跳过）"
        print("%s %-24s %-14s %-30s %s"
              % (mark, code, st, known, t.get("title", "")[:26]))
    return 0


def cmd_school_claim(mod, accounts, idx):
    """把开学季里「已完成但没领」的任务补领掉。

    🔴 为什么会有漏领：上游 `school_run_tasks` 只轮询 5×2=10 秒，超时就往下走，
    而服务端是**延迟入账**的 —— 等它翻成 completed 时，claim 那一步早跑过去了。
    成长中心那边我们做了 `final_claim_sweep` 兜底，**开学季一直没有**。
    """
    targets = accounts if idx == 0 else [accounts[idx - 1]] if 1 <= idx <= len(accounts) else []
    if not targets:
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    total = 0
    for i, acc in enumerate(accounts, 1):
        if acc not in targets:
            continue
        at = (acc.get("at") or "").strip()
        print("=" * 78)
        print("账号%d %s" % (i, acc.get("nick", "?")))
        try:
            s = mod._school_session(at)
            tasks, _ = mod._school_fetch_tasks(s)
        except Exception as exc:  # noqa: BLE001
            print("   查询失败 %s: %s" % (type(exc).__name__, exc))
            continue
        pend = [t for t in tasks if t.get("status") == "completed"]
        if not pend:
            print("   ✅ 无「已完成未领」")
            continue
        for t in pend:
            code = t.get("task_code", "")
            try:
                ok = mod._school_claim(s, code)
            except Exception as exc:  # noqa: BLE001
                print("   %s: 领奖异常 %s" % (code, type(exc).__name__))
                continue
            print("   %s: %s" % (code, "🎁 已领奖" if ok else "❌ 领奖失败"))
            if ok:
                total += 1
    print()
    print("合计补领 %d 项" % total)
    return 0


def cmd_school_lottery(mod, accounts, idx):
    """抽开学季幸运大转盘。

    ⚠️ 为什么要单独有个入口：上游把 `school_lottery` 紧跟在 `school_run_tasks` 后面，
    而**抽奖机会是任务发奖时才给的**（服务端还延迟入账）—— 跑任务那一刻余额往往还是 0，
    于是一整轮就白白跳过了。实测：跑完 17:59 那轮余额 0，补完任务后各账号涨到 1~2。
    """
    targets = accounts if idx == 0 else ([accounts[idx - 1]] if 1 <= idx <= len(accounts) else [])
    if not targets:
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    done = 0
    for i, acc in enumerate(accounts, 1):
        if acc not in targets:
            continue
        at = (acc.get("at") or "").strip()
        try:
            sc = mod._school_session(at)
            d = mod._school_get(sc, mod.SCHOOL_BASE + "/config").json()
            bal = ((d.get("data") or {}).get("chance") or {}).get("balance", 0)
        except Exception as exc:  # noqa: BLE001
            print("账号%d 查余额失败 %s" % (i, type(exc).__name__))
            continue
        if not bal or bal <= 0:
            print("账号%d 余额=0，跳过" % i)
            continue
        print("=" * 60)
        print("账号%d %s（余额 %s）" % (i, acc.get("nick", "?"), bal))
        try:
            mod.school_lottery(sc, mod.uid_of(at), mod.nickname_of(at), print)
            done += 1
        except Exception as exc:  # noqa: BLE001
            print("   抽奖异常 %s: %s" % (type(exc).__name__, str(exc)[:70]))
    print()
    print("已对 %d 个账号执行抽奖" % done)
    return 0


def cmd_school_probe(mod, accounts, idx):
    """只读：看开学季每个任务的 progress/target（判断还差多少）。"""
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    acc = accounts[idx - 1]
    try:
        s = mod._school_session(acc["at"])
        tasks, in_period = mod._school_fetch_tasks(s)
    except Exception as exc:  # noqa: BLE001
        print("查询失败 %s: %s" % (type(exc).__name__, exc))
        return 1
    print("账号%d %s —— 开学季（in_period=%s）" % (idx, acc.get("nick", "?"), in_period))
    for t in tasks:
        print("   %-24s %-13s %s/%-3s 奖 %s分+%s抽奖  [%s]"
              % (t.get("task_code", ""), t.get("status", ""),
                 t.get("progress"), t.get("target_count"),
                 t.get("reward_credit"), t.get("reward_chance"),
                 t.get("task_type", "")))
    return 0


def cmd_school_try(mod, accounts, idx):
    """测：把开学季上报从**信封**换成**裸数组**，看 chat_3_times / expert_use 会不会动。

    🔴 假设依据：`_school_report()` 发的是 `{"common":..., "events":[...]}` 信封，
    而服务端对信封是 HTTP 200 但不计分（已在桌面/资料库/小程序三处证实）。
    """
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    import time
    import uuid
    acc = accounts[idx - 1]
    at = (acc.get("at") or "").strip()
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = mod._school_session(at)
    target = mod.SCHOOL_DOMAIN + "/v2/report"

    def bare_post(events):
        """裸数组直发（**不走** _school_report 的信封）。"""
        return mod.api_retry(s, "POST", target, body=list(events))

    def prog_of(code):
        try:
            ts, _ = mod._school_fetch_tasks(s)
            t = next((x for x in ts if x.get("task_code") == code), None)
            return (t or {}).get("status"), (t or {}).get("progress"), (t or {}).get("target_count")
        except Exception:  # noqa: BLE001
            return None, None, None

    print("=" * 78)
    print("账号%d %s（裸数组试验）" % (idx, nick))
    print("=" * 78)
    tasks, in_period = mod._school_fetch_tasks(s)
    print("in_period=%s" % in_period)

    for code in ("chat_3_times", "expert_use"):
        st, cur, tgt = prog_of(code)
        print("\n【%s】起始: %s %s/%s" % (code, st, cur, tgt))
        if st in DONE:
            print("   已完成，跳过")
            continue
        try:
            print("   viewed 激活 → %s" % mod._school_viewed(s, code))
            time.sleep(1)
        except Exception as exc:  # noqa: BLE001
            print("   viewed 失败: %s" % type(exc).__name__)

        if code == "chat_3_times":
            # 试验：每条事件用**不同的 conversationId**
            # （上游是一次性用同一个 conv 发 3 条 → 服务端按 conv 去重，只算 1 次）
            for i in range(3):
                conv = "conv-" + str(uuid.uuid4())
                ev = mod._school_mini_chat_event(uid, nick, conv)
                try:
                    r = bare_post(ev)
                    st_i, cur_i, _ = prog_of(code)
                    print("   chat #%d (新 conv) → HTTP %s  进度 %s/%s"
                          % (i + 1, getattr(r, "status_code", "?"), cur_i, st_i))
                except Exception as exc:  # noqa: BLE001
                    print("   chat #%d 异常 %s" % (i + 1, type(exc).__name__))
                time.sleep(2)
        else:
            eid, ename = mod._school_fetch_expert(s)
            print("   专家: %r (%r)" % (ename, eid))
            # 试验：上游 _school_fetch_expert 读 e["id"]，但接口返回的是 expert_id
            try:
                ts, _ = mod._school_fetch_tasks(s)
                raw = mod._school_post(
                    s, mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/expert/list",
                    {"page": 1, "page_size": 10,
                     "categories": [mod.SCHOOL_EXPERT_CATEGORY], "expert_type": "agent"})
                ex = (raw.json().get("data") or {}).get("experts") or []
                first = ex[0] if ex else {}
                real_id = first.get("expert_id") or first.get("source_id") or ""
                real_name = (first.get("display_name_zh") or first.get("agent_name")
                             or ename or "开学季专家")
                print("   用 expert_id 重取: %r (%r)" % (real_name, real_id))
                if real_id:
                    conv = "conv-" + str(uuid.uuid4())
                    ev = mod._school_expert_event(uid, nick, real_id, real_name, conv)
                    r = bare_post(ev)
                    print("   expert → HTTP %s" % getattr(r, "status_code", "?"))
            except Exception as exc:  # noqa: BLE001
                print("   expert 试验异常 %s: %s" % (type(exc).__name__, str(exc)[:70]))

        for wait in (3, 5, 10):
            time.sleep(wait)
            st2, cur2, tgt2 = prog_of(code)
            print("   +%2ds 复查: %s %s/%s" % (wait, st2, cur2, tgt2))
            if st2 in DONE:
                print("   ✅ 翻过去了！裸数组是对的")
                try:
                    if st2 == "completed":
                        print("   🎁 领奖 → %s" % mod._school_claim(s, code))
                except Exception as exc:  # noqa: BLE001
                    print("   领奖异常 %s" % type(exc).__name__)
                break
    return 0


def cmd_platforms(mod, accounts, idx):
    """只读：用不同 X-Client-Platform 去查同一接口，比对任务清单有没有差异。

    依据：小程序那两个任务**只在带 miniprogram 头时才下发** ——
    说明"看到多少任务"取决于客户端身份。这里把所有可能的身份都试一遍，
    看有没有第三种隐藏任务清单。
    """
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    acc = accounts[idx - 1]
    s = mod.new_api(acc["at"])
    url = mod.BASE + "/v2/activity/growth/tasks"
    variants = OrderedDict([
        ("(不带头)", {}),
        ("miniprogram", {"X-Client-Platform": "miniprogram"}),
        ("web", {"X-Client-Platform": "web"}),
        ("desktop", {"X-Client-Platform": "desktop"}),
        ("app", {"X-Client-Platform": "app"}),
        ("wx_app_cloud", {"X-Client-Platform": "wx_app_cloud"}),
    ])
    got = OrderedDict()
    for name, hd in variants.items():
        try:
            r = s.get(url, timeout=25, verify=False, headers=hd)
            d = r.json()
            tasks = (d.get("data") or {}).get("tasks") or []
            codes = {t.get("task_code") for t in tasks if isinstance(t, dict)}
            got[name] = codes
        except Exception as exc:  # noqa: BLE001
            print("   %-14s 查询失败 %s" % (name, type(exc).__name__))
            got[name] = set()
    print("账号%d %s —— 不同客户端身份下的任务清单" % (idx, acc.get("nick", "?")))
    for name, codes in got.items():
        print("   %-14s %2d 个: %s" % (name, len(codes), ", ".join(sorted(codes))))
    all_codes = set().union(*got.values()) if got else set()
    base = got.get("(不带头)", set())
    for name, codes in got.items():
        extra = codes - base
        miss = base - codes
        if extra:
            print("   ⭐ %s 比「不带头」多出: %s" % (name, ", ".join(sorted(extra))))
        if miss:
            print("   ⚠️ %s 比「不带头」少了: %s" % (name, ", ".join(sorted(miss))))
    if all(c == base for c in got.values()):
        print("   → 所有身份下发的任务完全一致，没有隐藏清单")
    else:
        print("   → 合计可见 %d 个任务码" % len(all_codes))
    return 0


def cmd_seq2_try(mod, accounts, idx):
    """模拟：Sequential_Tasks_2「完成 1 次专家对话」（200 积分，7 账号全没做）。

    只做试验，**不改任何生产代码**。变体依次试，看哪一个能让服务端认账。
    """
    if not (1 <= idx <= len(accounts)):
        print("账号序号 %d 超范围（共 %d 个）" % (idx, len(accounts)))
        return 2
    import time
    import uuid
    acc = accounts[idx - 1]
    at = (acc.get("at") or "").strip()
    uid = mod.uid_of(at)
    nick = mod.nickname_of(at)
    s = mod.new_api(at)
    CODE = "Sequential_Tasks_2"

    def prog():
        try:
            r = s.get(mod.BASE + "/v2/activity/growth/tasks", timeout=25,
                      verify=False, headers=mod.MP_HEADER)
            for t in r.json().get("data", {}).get("tasks", []):
                if t.get("task_code") == CODE:
                    pr = t.get("progress") or {}
                    return t.get("accept_status"), pr.get("current"), pr.get("target")
        except Exception:  # noqa: BLE001
            pass
        return None, None, None

    print("=" * 78)
    print("账号%d %s —— 模拟 %s" % (idx, nick, CODE))
    print("=" * 78)
    print("起始: %s" % (prog(),))
    print("accept → %s" % mod._mp_accept(s, CODE))
    time.sleep(2)
    print("accept 后: %s" % (prog(),))

    # 取真专家（上游用 e["id"] 会拿到空串，这里用 expert_id）
    eid = ename = ""
    try:
        ex = mod._school_post(
            s, mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/expert/list",
            {"page": 1, "page_size": 10,
             "categories": [mod.SCHOOL_EXPERT_CATEGORY], "expert_type": "agent"})
        experts = (ex.json().get("data") or {}).get("experts") or []
        e = experts[0] if experts else {}
        eid = e.get("expert_id") or e.get("source_id") or ""
        ename = e.get("display_name_zh") or e.get("agent_name") or "开学季专家"
    except Exception as exc:  # noqa: BLE001
        print("取专家失败 %s" % type(exc).__name__)
    print("专家: %r (%r)" % (ename, eid))
    if not eid:
        print("拿不到专家 id，实验中止")
        return 1

    def send(tag, events):
        try:
            http = E._mp_bare_report(mod, s, uid, nick, events)
        except Exception as exc:  # noqa: BLE001
            print("   %s: 发送异常 %s" % (tag, type(exc).__name__))
            return
        print("   %s: HTTP %s" % (tag, http))
        for wait in (4, 8):
            time.sleep(wait)
            st, cur, tgt = prog()
            print("      +%2ds 复查: %s %s/%s" % (wait, st, cur, tgt))
            if st in DONE:
                print("      ✅ 成了！")
                return

    conv = "mini-ex-" + str(uuid.uuid4())
    # 变体 A：上游原样（带 activityId，expert_summoned + expert_actual_use）
    send("A 带 activityId（原样）", mod._school_expert_event(uid, nick, eid, ename, conv))

    conv2 = "mini-ex-" + str(uuid.uuid4())
    evs_b = mod._school_expert_event(uid, nick, eid, ename, conv2)
    for e in evs_b:
        e.pop("activityId", None)
    send("B 去掉 activityId", evs_b)
    return 0


def main():
    args = sys.argv[1:]
    which = args[0].lower() if args else ""
    mod, accounts = load()
    if which == "school":
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        return cmd_school(mod, accounts, idx)
    if which == "school-claim":
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        return cmd_school_claim(mod, accounts, idx)
    if which == "school-probe":
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        return cmd_school_probe(mod, accounts, idx)
    if which == "school-lottery":
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        return cmd_school_lottery(mod, accounts, idx)
    if which == "school-try":
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        return cmd_school_try(mod, accounts, idx)
    if which == "platforms":
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        return cmd_platforms(mod, accounts, idx)
    if which == "seq2-try":
        idx = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
        return cmd_seq2_try(mod, accounts, idx)
    if which.isdigit():
        return cmd_one(mod, accounts, int(which))
    return cmd_all(mod, accounts)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
