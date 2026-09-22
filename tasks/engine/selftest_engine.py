#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""引擎接线自检

selftest.py 只管「上游同步护栏」；这个文件管引擎自己那部分：
  1. 桌面策略解析（desktop_mode / --no-desktop / --desktop-full 的优先级）
  2. 危险实现替换是否真的生效（指纹模式必须替掉上游的换血实现）
  3. 引擎自身绝不含杀进程/起进程的能力 —— 用 AST 查，不看注释里的字样

    python selftest_engine.py
"""

import argparse
import ast
import hashlib
import json
import os
import sys
from pathlib import Path

import engine as E

FAILED = []


def check(label, got, want):
    ok = got == want
    print("%s %s" % ("✅" if ok else "❌", label))
    if not ok:
        FAILED.append(label)
        print("     期望 %r" % (want,))
        print("     实际 %r" % (got,))


def quiet(_msg):
    """吃掉闭包里的日志输出，别把自检结果淹了。"""
    return None


# ------------------------------------------------------------------ 1. 策略解析

def make_args(no_desktop=False, desktop_full=False):
    return argparse.Namespace(no_desktop=no_desktop, desktop_full=desktop_full)


def test_resolve():
    print("\n── 1. 桌面策略解析 ──")
    check("配置 fingerprint + 无开关 → fingerprint",
          E.resolve_desktop_mode({"desktop_mode": "fingerprint"}, make_args()), "fingerprint")
    check("配置缺失 → 默认 fingerprint（安全默认）",
          E.resolve_desktop_mode({}, make_args()), "fingerprint")
    check("配置值乱写 → 回落 fingerprint",
          E.resolve_desktop_mode({"desktop_mode": "WTF"}, make_args()), "fingerprint")
    check("--no-desktop → off",
          E.resolve_desktop_mode({"desktop_mode": "fingerprint"},
                                 make_args(no_desktop=True)), "off")
    check("--desktop-full → full",
          E.resolve_desktop_mode({"desktop_mode": "fingerprint"},
                                 make_args(desktop_full=True)), "full")
    check("配置 full → full（需要显式配置才生效）",
          E.resolve_desktop_mode({"desktop_mode": "full"}, make_args()), "full")
    check("--no-desktop 优先于 --desktop-full（互斥组保证不会同时给）",
          E.resolve_desktop_mode({"desktop_mode": "full"},
                                 make_args(no_desktop=True)), "off")


# ------------------------------------------------------------------ 2. 替换生效

class FakeMod:
    """冒充上游模块，只放桌面相关的名字，并记录谁被调用了。"""

    def __init__(self):
        self.calls = []

    def t_desktop_tasks(self, *a, **k):
        self.calls.append("UNSAFE:上游换血实现被执行")

    def t_workstation(self, *a, **k):
        self.calls.append("UNSAFE:上游工作台换血被执行")

    def _desktop_fingerprint_fallback(self, s, uid, nick, log, need_rich, need_skill):
        self.calls.append("fingerprint:%s/%s" % (need_rich, need_skill))

    def prog(self, s, code):
        """回读任务真实状态（上游同签名）。用于验证引擎补了「回读」这一步。"""
        self.calls.append("prog:%s" % code)
        return "accepted", 0, 1


def test_patch():
    print("\n── 2. 危险实现替换 ──")
    mod = FakeMod()
    replaced = E.apply_desktop_safety(mod, "fingerprint", {"skill_real_chat": False})
    check("fingerprint 模式替换 2 个函数", len(replaced), 2)

    lines = []

    def log(m):
        lines.append(m)

    mod.t_desktop_tasks(None, "uid", "nick", "tok", log, True, True)
    check("桌面任务改走纯 API 指纹上报",
          [c for c in mod.calls if c.startswith("fingerprint")], ["fingerprint:True/True"])
    # 🔴 2026-09-20：上游那句「桌面对话(指纹): ✅ 6连事件已上报」是**无条件打印**的假成功
    #    （实测 RichMeow_Chat 长期停在 accepted 0/1，与日志的 ✅ 直接矛盾）
    #    → 引擎必须补一行**回读真实状态**，免得再被假 ✅ 骗。
    check("补了 RichMeow_Chat 回读", "prog:RichMeow_Chat" in mod.calls, True)
    check("补了 skill_1 回读", "prog:skill_1" in mod.calls, True)
    check("回读结果打到日志（含 accepted 0/1）",
          any("RichMeow_Chat 回读" in x and "accepted 0/1" in x for x in lines), True)

    mod.t_workstation(None, "uid", "nick", log, "tok")
    check("工作台搭建师不再触发换血（无新调用）", mod.calls[-2:], ["prog:RichMeow_Chat",
                                                                "prog:skill_1"])

    for mode in ("off", "full"):
        fresh = FakeMod()
        check("%s 模式不做替换" % mode,
              E.apply_desktop_safety(fresh, mode, {"skill_real_chat": False}), [])


def test_patch_survives_failure():
    """指纹上报抛异常时，账号不能整体挂掉。"""
    print("\n── 3. 指纹上报异常隔离 ──")

    class BoomMod(FakeMod):
        def _desktop_fingerprint_fallback(self, *a, **k):
            raise RuntimeError("模拟接口 500")

    mod = BoomMod()
    E.apply_desktop_safety(mod, "fingerprint")
    try:
        mod.t_desktop_tasks(None, "uid", "nick", "tok", quiet, True, True)
        ok = True
    except Exception:
        ok = False
    check("指纹上报异常被吞掉，不冒泡到 run_accounts", ok, True)


# ------------------------------------------------------------------ 4. 引擎自身能力

def test_engine_is_harmless():
    print("\n── 4. 引擎自身不含杀进程能力（AST 级检查）──")
    source = Path(E.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imports, calls = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module or "")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                calls.add("%s.%s" % (getattr(fn.value, "id", "?"), fn.attr))
            elif isinstance(fn, ast.Name):
                calls.add(fn.id)

    check("不导入 subprocess", "subprocess" in imports, False)
    for bad in ("os.system", "os.popen", "os.kill", "os.remove", "os.unlink"):
        check("不调用 %s" % bad, bad in calls, False)
    check("不调用 eval/exec", bool({"eval", "exec"} & calls), False)

    upstream = (E.VENDOR / "workbuddy_daily.py").read_text(encoding="utf-8", errors="replace")
    check("上游确实仍含 taskkill（说明加固仍有必要）", "taskkill" in upstream, True)


def test_switch_account_store_is_readonly():
    print("\n── 5. workbuddy-switch 账号库只读 ──")
    path = Path(os.path.expanduser("~/.wb-switch/accounts.json"))
    if not path.exists():
        print("⚠️  账号库不存在，跳过（%s）" % path)
        return
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    cfg = E.load_config()
    accounts, skipped = E.read_switch_accounts(cfg)
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    check("读取账号库未改动其内容", before == after, True)
    check("读出了账号（>0 个）", len(accounts) > 0, True)
    print("     账号 %d 个，过滤 %d 个" % (len(accounts), len(skipped)))


def test_only_selection():
    """--only N 必须跑第 N 个账号。

    这里曾经有个真 bug：写成了 ACCOUNTS[0]，于是 --only 4 会拿第 1 个账号的凭据
    去跑、日志却打着「账号4」—— 静默打错账号，比报错危险得多。
    """
    print("\n── 6. --only 序号选择 ──")

    class FakeRunner:
        def __init__(self, n=7):
            self.ACCOUNTS = [{"note": "acc%d" % i} for i in range(1, n + 1)]
            self.ran = []

        def run_account(self, idx, entry, do_desktop):
            self.ran.append((idx, entry["note"]))
            return [], {"idx": idx, "note": entry["note"], "done": 0, "total": 0,
                        "rest": [], "level": "?", "energy": "?"}

    cfg = {"desktop_mode": "off"}
    real_sleep, E.time.sleep = E.time.sleep, lambda _s: None
    try:
        args = make_args()
        args.only = 4
        mod = FakeRunner()
        E.run_accounts(mod, args, cfg)
        check("--only 4 跑的是第 4 个账号", mod.ran, [(4, "acc4")])

        args.only = 1
        mod = FakeRunner()
        E.run_accounts(mod, args, cfg)
        check("--only 1 跑的是第 1 个账号", mod.ran, [(1, "acc1")])

        args.only = 99
        mod = FakeRunner()
        try:
            E.run_accounts(mod, args, cfg)
            raised = False
        except SystemExit:
            raised = True
        check("--only 越界明确报错（而不是跑错账号）", raised, True)
        check("越界时一个账号都没跑", mod.ran, [])

        args.only = None
        mod = FakeRunner(3)
        E.run_accounts(mod, args, cfg)
        check("不给 --only 时按顺序跑全部",
              [n for _, n in mod.ran], ["acc1", "acc2", "acc3"])
    finally:
        E.time.sleep = real_sleep


def test_args_attributes_are_declared():
    """engine 里用到的 args.X 必须都在 parse_args() 里注册过。

    这个检查是必须的：pyflakes 看不出 argparse.Namespace 的属性和法，
    `args.desktop`（早已改名为 --no-desktop/--desktop-full）能一路静态检查全绿地
    活到运行时才 AttributeError —— 实际就这么炸过一次。
    """
    print("\n── 7. 命令行属性与 parse_args 对齐 ──")
    tree = ast.parse(Path(E.__file__).read_text(encoding="utf-8"))

    declared = {"help"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    declared.add(arg.value.lstrip("-").replace("-", "_"))

    used = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                and node.value.id == "args"):
            used.add(node.attr)

    check("args.X 都有对应命令行参数", sorted(used - declared), [])
    print("     已注册 %d 个参数，用到 %d 个" % (len(declared), len(used)))


def test_desktop_scope_and_detection():
    print("\n── 8. 桌面任务适用账号范围 ──")
    args = make_args()
    args.desktop_current = False
    args.desktop_all = False

    check("默认 all", E.resolve_desktop_scope({}, args), "all")
    check("乱写的值回落 all", E.resolve_desktop_scope({"desktop_scope": "zzz"}, args), "all")
    check("配置 current", E.resolve_desktop_scope({"desktop_scope": "current"}, args), "current")
    args.desktop_current = True
    check("--desktop-current 覆盖配置", E.resolve_desktop_scope({"desktop_scope": "all"}, args), "current")
    args.desktop_all = True
    check("--desktop-all 覆盖 --desktop-current", E.resolve_desktop_scope({"desktop_scope": "current"}, args), "all")

    print("\n── 9. 当前桌面账号识别 ──")
    tree = ast.parse(Path(E.__file__).read_text(encoding="utf-8"))
    fns = {n.name: n for n in ast.walk(tree)
           if isinstance(n, ast.FunctionDef)
           and n.name in ("read_desktop_uid", "find_desktop_account")}
    check("两个探测函数都在", sorted(fns), ["find_desktop_account", "read_desktop_uid"])

    banned = {"write_text", "write_bytes", "dump", "copy", "copy2", "copyfile",
              "unlink", "remove", "rmtree", "move", "rename", "mkdir", "chmod"}
    hits = []
    for fn in fns.values():
        for node in ast.walk(fn):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in banned:
                    hits.append("%s→%s" % (fn.name, name))
    check("探测过程不含任何写操作（只读约束）", hits, [])

    uid, note = E.read_desktop_uid()
    check("能读出桌面端 uid", bool(uid), True)
    if uid:
        print("     桌面端 uid=%s…  来源=%s" % (uid[:8], note))
        accounts = [{"nick": "甲", "uid": "11111111-0000-0000-0000-000000000000"},
                    {"nick": "乙", "uid": uid}]
        idx, why = E.find_desktop_account(accounts)
        check("按 uid 匹配到正确账号（序号 2）", idx, 2)
        print("     %s" % why)
        idx2, _ = E.find_desktop_account(
            [{"nick": "丙", "uid": "99999999-0000-0000-0000-000000000000"}])
        check("匹配不上时返回 None（绝不猜一个账号去当桌面账号）", idx2, None)


def test_watcher_decisions():
    """守望者的判定必须是纯函数逻辑 —— 切换补跑要是判断错了就是白跑/重复跑。"""
    print("\n── 10. 守望者判定（账号切换补跑）──")
    import watch as W

    cfg = {"min_gap_minutes": 120}
    now = 1_000_000.0

    check("读不到 uid → no-uid（什么都不做）",
          W.evaluate(None, None, 0, now, cfg)[0], "no-uid")
    check("首次观测 → 只记录不跑",
          W.evaluate(None, "uid-A", 0, now, cfg)[0], "observe")
    check("run_on_start=true 时首次也跑",
          W.evaluate(None, "uid-A", 0, now, {"run_on_start": True})[0], "run")
    check("uid 没变 → idle",
          W.evaluate("uid-A", "uid-A", 0, now, cfg)[0], "idle")
    check("uid 变了 → run（这就是「切换账号自动补跑」）",
          W.evaluate("uid-A", "uid-B", 0, now, cfg)[0], "run")
    check("变了但该账号刚跑过 → cooldown",
          W.evaluate("uid-A", "uid-B", now - 60, now, cfg)[0], "cooldown")
    check("超过冷却期 → run",
          W.evaluate("uid-A", "uid-B", now - 121 * 60, now, cfg)[0], "run")
    check("min_gap_minutes=0 → 不限制",
          W.evaluate("uid-A", "uid-B", now - 1, now, {"min_gap_minutes": 0})[0], "run")

    # 硬约束：守望者不许有能力改桌面认证文件、也不许有能力杀客户端。
    # 直接查那几个「真凶」函数名 —— 想写认证文件就绕不开 swap_info/restore_info，
    # 想杀客户端就得有 taskkill。顺带确认它没加载上游模块（否则就能间接拿到这些能力）。
    src = Path(W.__file__).read_text(encoding="utf-8")
    for bad in ("swap_info", "restore_info", "taskkill", "load_upstream"):
        check("守望者不含 %s" % bad, bad in src, False)
    check("守望者只用只读接口读 uid", "read_desktop_uid" in src, True)
    check("守望者用 engine 的运行标记避让全量运行", "engine_running" in src, True)

    print("\n── 11. PID 存活探测（中文系统编码陷阱）──")
    # 实测踩过：中文 Windows 的 tasklist 输出是 GBK，按 UTF-8 解会在读取线程里炸掉，
    # 结果 .stdout 变成 None，pid_alive 整个 TypeError。
    gbk_msg = "信息: 没有运行的任务匹配指定标准。".encode("gbk")
    check("GBK 编码的 tasklist 输出能解出来",
          "没有运行的任务" in W._decode(gbk_msg), True)
    check("UTF-8 输出也能解出来",
          "ok" in W._decode("ok".encode("utf-8")), True)
    check("自己这个 PID 一定活着", W.pid_alive(os.getpid()), True)
    check("一个肯定不存在的 PID → False", W.pid_alive(999999), False)


def test_post_claim_recheck():
    """领奖后复查：顺序必须是「原流程（含领奖）→ 抽奖 → 盲盒」。

    上游是先开盲盒、最后才领奖，于是本次赚到的能量当天用不上。这个包装就是为了改顺序。
    """
    print("\n── 12. 领奖后复查（盲盒/抽奖）──")

    class BoomSession:
        def get(self, *a, **k):
            raise RuntimeError("self-test 不联网")

    class FakeMod:
        def __init__(self):
            self.calls = []
            self.BASE = "https://example.invalid"

        def run_account(self, idx, acc, do_desktop):
            self.calls.append("run_account")
            return [], {"idx": idx, "note": acc.get("note", ""), "done": 0, "total": 0,
                        "rest": [], "level": "?", "energy": "?"}

        def new_api(self, tok):
            return BoomSession()

        def uid_of(self, tok):
            return "uid"

        def nickname_of(self, tok):
            return "nick"

        def t_lottery(self, s, uid, nick, log):
            self.calls.append("t_lottery")

        def t_blindbox(self, s, uid, nick, log):
            self.calls.append("t_blindbox")

    acc = {"note": "甲", "access_token": "tok"}
    mod = FakeMod()
    replaced = E.apply_post_claim_recheck(mod, {})
    check("默认开启并包装 run_account",
          replaced, ["run_account→领奖后复查盲盒/抽奖"])
    mod.run_account(1, acc, False)
    check("顺序 = 原流程 → 抽奖 → 盲盒",
          mod.calls, ["run_account", "t_lottery", "t_blindbox"])

    # 复查自身报错不能把跑完的账号算成失败
    class BoomLottery(FakeMod):
        def t_lottery(self, s, uid, nick, log):
            raise RuntimeError("模拟抽奖接口炸了")

    mod3 = BoomLottery()
    E.apply_post_claim_recheck(mod3, {})
    try:
        msgs, summary = mod3.run_account(1, acc, False)
        ok = isinstance(summary, dict)
    except Exception:
        ok = False
    check("复查异常被吞掉，summary 仍正常返回", ok, True)

    mod2 = FakeMod()
    check("配置关掉就不包装", E.apply_post_claim_recheck(mod2, {"post_claim_recheck": False}), [])
    mod2.run_account(1, acc, False)
    check("关掉后不复查", mod2.calls, ["run_account"])


def test_skill_real_chat():
    """尝鲜热门技能：埋点必须搭在**真实对话**上才被服务端认。

    实测：只打 /v2/report 的 skill_info → 永远 accepted 0/1；
    真实 POST /console/chat/completions 且把 skill_info 放进
    _meta["codebuddy.ai"]["growthEvent"] → 立刻 completed 1/1。
    """
    print("\n── 13. 尝鲜热门技能：真实对话搭车点亮 ──")

    class BoomSession:
        def post(self, *a, **k):
            raise RuntimeError("self-test 不联网")

    class FakeMod:
        SKILL_NAME = "algorithmic-trading"
        SCHOOL_DOMAIN = "https://school.invalid"
        BASE = "https://base.invalid"

        def __init__(self, state="accepted", cur=0):
            self.state, self.cur = state, cur
            self.chat_meta = None
            self.reported = []
            self.chats = 0

        def prog(self, s, code):
            return self.state, self.cur, 1

        def derive_id(self, uid, kind):
            return "d" * 12

        def webchat(self, s, name, prompt, meta=None, model="glm-5.2"):
            self.chats += 1
            self.chat_meta = meta
            self.state, self.cur = "completed", 1   # 模拟服务端真的点亮
            return "conv-1", "技能已加载"

        def report(self, s, uid, nick, events):
            self.reported.append(events)

    mod = FakeMod()
    ok = E.credit_skill_via_real_chat(mod, BoomSession(), "uid", "nick", lambda m: None)
    check("真实对话后判为已完成", ok, True)
    ge = json.loads(mod.chat_meta["codebuddy.ai"]["growthEvent"])
    check("growthEvent 里是 skill_info", [e["eventCode"] for e in ge], ["skill_info"])
    check("meta 带 skillId / skillName",
          sorted(k for k in mod.chat_meta["codebuddy.ai"] if k.startswith("skill")),
          ["skillId", "skillName"])
    check("另外还补了一条常规埋点", len(mod.reported), 1)

    mod2 = FakeMod("completed", 1)
    check("已完成 → 直接返回 False",
          E.credit_skill_via_real_chat(mod2, BoomSession(), "uid", "nick", lambda m: None), False)
    check("已完成 → 不再白发对话", mod2.chats, 0)

    print("\n── 14. 接口异常不拖垮账号 ──")

    class BoomChat(FakeMod):
        def webchat(self, s, name, prompt, meta=None, model="glm-5.2"):
            raise RuntimeError("模拟对话接口 500")

    mod3 = BoomChat()
    try:
        r = E.credit_skill_via_real_chat(mod3, BoomSession(), "uid", "nick", lambda m: None)
        ok = (r is False)
    except Exception:
        ok = False
    check("对话接口炸了也只是返回 False，不冒泡", ok, True)


def test_library_report_shape():
    """资料库上报：必须是【裸数组】+ 那三个 X- 头，字段也要照前端来。

    实测教训：上游发 {common,events} 信封时服务端一律不给进度；
    改成裸数组后 accepted 0/1 直接变 completed 1/1。
    """
    print("\n── 15. 体验资料库：报文形状必须照前端 ──")

    class FakeResp:
        status_code = 200

    class FakeSession:
        def __init__(self):
            self.calls = []

        def post(self, url, json=None, timeout=None, headers=None, **k):
            self.calls.append({"url": url, "json": json, "headers": headers or {}})
            return FakeResp()

    class FakeMod:
        BASE = "https://base.invalid"
        LIB_DOC_URL = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"

        def __init__(self, state="accepted", cur=0):
            self.state, self.cur = state, cur
            self.posts = 0

        def t_library(self, *a, **k):
            """上游原版（会被替换掉）；这里故意不做事，被调用即视为替换失败。"""
            self.posts += 1

        def prog(self, s, code):
            return self.state, self.cur, 1

        def derive_id(self, uid, kind):
            return "m" * 12

    mod = FakeMod()
    replaced = E.apply_web_report_fix(mod, {})
    check("默认开启并替换 t_library",
          replaced, ["t_library→裸数组报文（贴合前端真实形状）"])
    sess = FakeSession()
    mod.t_library(sess, "uid-1", "甲", lambda m: None)
    check("只发了 1 个请求", len(sess.calls), 1)
    body = sess.calls[0]["json"]
    check("body 是【裸数组】（不是 {common,events} 信封）", isinstance(body, list), True)
    check("数组里就 1 个事件对象", len(body), 1)
    ev = body[0]
    check("eventCode = web_element_click", ev.get("eventCode"), "web_element_click")
    check("elementId = library_doc_intro_click",
          ev.get("elementId"), "library_doc_intro_click")
    check("pageURL 指向那篇文档", ev.get("pageURL"), FakeMod.LIB_DOC_URL)
    for f in ("timestamp", "reportDelay", "elementName", "os", "arch",
              "osVersion", "userAgent", "machineId", "userId",
              "userNickname", "enterpriseId"):
        if f not in ev:
            check("事件里缺字段 %s" % f, True, False)
    hdr = {k.lower() for k in sess.calls[0]["headers"]}
    for h in ("content-type", "x-requested-with", "x-request-trace-id", "x-user-id"):
        check("带上请求头 %s" % h, h in hdr, True)

    mod2 = FakeMod("completed", 1)
    E.apply_web_report_fix(mod2, {})
    s2 = FakeSession()
    mod2.t_library(s2, "uid-1", "甲", lambda m: None)
    check("已完成就不发请求", len(s2.calls), 0)

    mod3 = FakeMod()
    check("配置可关闭", E.apply_web_report_fix(mod3, {"fix_library_report": False}), [])


def test_report_shape_fix():
    """桌面事件上报：必须裸数组 + 每条都带 timestamp/reportDelay。

    服务端缺这两个字段会直接 400；发信封则 200 但不计分（最坑的一种失败）。
    """
    print("\n── 16. 桌面事件上报形状 ──")

    class FakeMod:
        BASE = "https://base.invalid"

        def __init__(self):
            self.sent = None

        def report_desktop_events(self, s, uid, nick, events):
            self.sent = "上游原版不该被调用"

        def desktop_fingerprint(self, uid, nick):
            # 与上游真实实现同形：含 timestamp/presentAt，但**不含 mode**（不能污染业务字段）
            return {"timezone": "Asia/Shanghai", "reportDelay": 2000,
                    "userId": uid, "username": nick, "userNickname": nick,
                    "product": "SaaS", "ideName": "WorkBuddy", "ideType": "WorkBuddy",
                    "ideVersion": "5.5.6", "machineId": "fp-machine", "sessionId": "fp-session",
                    "extName": "workbuddy-desktop", "extVersion": "5.5.6",
                    "os": "win32", "arch": "x64", "osVersion": "10.0.26220",
                    "timestamp": 111, "presentAt": 111}

        def api_retry(self, s, method, url, body=None, **k):
            self.sent = body
            return 200

    # 模拟上游 desktop_buddy5_sequence 的产物：没有 timestamp/reportDelay
    raw = [{"eventCode": "buddyapp_discover_click", "mode": "LOCAL",
            "buddyId": "b1", "buddyName": "发现应用"},
           {"eventCode": "buddyapp_show", "mode": "LOCAL", "buddyId": "b1"},
           {"eventCode": "buddyapp_enter_click", "mode": "LOCAL"}]

    mod = FakeMod()
    replaced = E.apply_report_shape_fix(mod, {})
    check("默认开启并替换 report_desktop_events",
          replaced, ["report_desktop_events→裸数组+时间字段", "t_buddy_apps→补领奖"])
    mod.report_desktop_events(None, "uid-1", "甲", raw)
    body = mod.sent
    check("body 是【裸数组】", isinstance(body, list), True)
    check("事件条数不变", len(body), len(raw))
    check("每条都补了 timestamp", all("timestamp" in e for e in body), True)
    check("每条都补了 reportDelay", all("reportDelay" in e for e in body), True)
    check("timestamp 单调递增（像真实操作序列）",
          all(body[i]["timestamp"] < body[i + 1]["timestamp"] for i in range(len(body) - 1)), True)
    check("补上了 userId", all(e.get("userId") == "uid-1" for e in body), True)
    check("原来的业务字段没被覆盖",
          [e["eventCode"] for e in body], [e["eventCode"] for e in raw])
    check("业务字段 mode 没被信封污染", all(e.get("mode") == "LOCAL" for e in body), True)

    # 🔴 坑②回归护栏（2026-09-20 深夜）：必须把「客户端身份信封」注入回来。
    #    第一版补丁只拆外层信封、忘了注入 fp → 桌面事件以"无任何身份字段"发出 →
    #    服务端 200 但不计分。实证：同账号同分钟 A/B，arm0 一直 0/1、arm1 首读即 1/1。
    check("每条都注入了 ideType", all(e.get("ideType") == "WorkBuddy" for e in body), True)
    check("每条都注入了 ideName", all(e.get("ideName") == "WorkBuddy" for e in body), True)
    check("每条都注入了 extName", all(e.get("extName") == "workbuddy-desktop" for e in body), True)
    check("每条都注入了 machineId", all(e.get("machineId") == "fp-machine" for e in body), True)
    check("每条都注入了 product", all(e.get("product") == "SaaS" for e in body), True)
    check("每条都注入了 os", all(e.get("os") == "win32" for e in body), True)
    check("身份信封没覆盖 userId", all(e.get("userId") == "uid-1" for e in body), True)
    # 信封自带 timestamp=111，但必须让每条事件保留自己的递进时间戳
    check("timestamp 仍逐条递增（没被信封的固定值压平）",
          all(body[i]["timestamp"] < body[i + 1]["timestamp"] for i in range(len(body) - 1)), True)
    check("没被压平成信封那个定值", all(e["timestamp"] != 111 for e in body), True)

    # 调用方已经给了 timestamp 就不要覆盖
    mod2 = FakeMod()
    E.apply_report_shape_fix(mod2, {})
    mod2.report_desktop_events(None, "u", "n",
                               [{"eventCode": "x", "timestamp": 999, "reportDelay": 42}])
    check("已有 timestamp 就保留原值", mod2.sent[0]["timestamp"], 999)
    check("已有 reportDelay 就保留原值", mod2.sent[0]["reportDelay"], 42)

    mod3 = FakeMod()
    check("配置可关闭", E.apply_report_shape_fix(mod3, {"fix_report_shape": False}), [])


# ------------------------------------------------------------------ 16b. 小程序上报形状

def test_mini_report_shape():
    """小程序两处上报（_mini_report / t_school_season）的形状回归测试。

    🔴 2026-09-20 修复：这两处一直发 `{common,events}` **信封**，而服务端对信封是
    静默忽略的（HTTP 200 但不计分）→ 日志长期「mini chat 已上报」却卡 accepted 0/1。
    改成**裸数组**后，账号2 实测 3 秒内 completed 1/1 并领到 +100积分+5能量。
    这两个任务**不需要抓包**，错的只是报文形状。
    """
    import requests

    captured = {}

    class FakeSession:
        """假的 requests.Session：只记录发出去的 body，绝不联网。"""

        def __init__(self):
            self.headers = {}

        def post(self, url, json=None, **kw):
            captured["url"] = url
            captured["body"] = json
            captured["headers"] = dict(self.headers)

            class _R:
                status_code = 200

            return _R()

    class FakeApi:
        headers = {"Authorization": "Bearer tok-123"}

    class FakeMod:
        """只提供补丁会用到的几个上游符号。"""
        BASE = "https://www.workbuddy.cn"
        MP_UA = "WeChat-MiniProgram-UA"
        SCHOOL_ACTIVITY_ID = "school_open_day_2026"
        WRITE_GAP = 0.01

        def __init__(self):
            self.prog_calls = 0

        def _mini_report(self, s, uid, nick, conv_id):
            raise AssertionError("不该调用上游原版 _mini_report")

        def t_school_season(self, s, uid, nick, log):
            raise AssertionError("不该调用上游原版 t_school_season")

        def _mp_prog(self, s, code):
            self.prog_calls += 1
            if self.prog_calls == 1:
                return "not_accepted", 0, 1
            return "completed", 1, 1

        def _mp_accept(self, s, code):
            return True

        def _mp_claim(self, s, code, log):
            captured["claimed"] = code
            return True

    real_session = requests.Session
    requests.Session = FakeSession
    try:
        # ① 换函数
        mod = FakeMod()
        applied = E.apply_mini_report_fix(mod, {})
        check("默认开启并替换两处", applied,
              ["_mini_report→裸数组报文", "t_school_season→裸数组报文"])

        # ② _mini_report 必须是裸数组
        api = FakeApi()
        http = mod._mini_report(api, "uid-1", "甲", "conv-abc")
        check("_mini_report 返回 HTTP 码", http, 200)
        check("body 是【裸数组】而不是信封", isinstance(captured["body"], list), True)
        check("信封里的 common 不见了", "common" in str(captured["body"][:1]), False)
        ev = captured["body"][0]
        check("事件码是 chat_request_send", ev.get("eventCode"), "chat_request_send")
        check("带 timestamp（服务端强制要求）", isinstance(ev.get("timestamp"), int), True)
        check("带 reportDelay", ev.get("reportDelay"), 0)
        check("带 conversationId", ev.get("conversationId"), "conv-abc")
        check("打到 copilot 域", captured["url"], "https://copilot.tencent.com/v2/report")
        check("带 miniprogram 头",
              captured["headers"].get("X-Client-Platform"), "miniprogram")
        check("Authorization 是 Bearer 且没被写坏",
              captured["headers"].get("Authorization"), "Bearer tok-123")

        # ③ 校园日：也走裸数组，且多带 activityId
        captured.clear()
        mod2 = FakeMod()
        E.apply_mini_report_fix(mod2, {})
        mod2.t_school_season(FakeApi(), "uid-1", "甲", quiet)
        check("校园日 body 也是裸数组", isinstance(captured.get("body"), list), True)
        check("校园日带 activityId",
              captured["body"][0].get("activityId"), "school_open_day_2026")
        check("校园日点亮后会自动领奖", captured.get("claimed"), "school_season")

        # ④ 事件构造函数本身
        e1 = E._mini_chat_event("u1", "c1", "act-1")
        check("给了 activityId 就带上", e1.get("activityId"), "act-1")
        e2 = E._mini_chat_event("u1", "c1")
        check("没给 activityId 就不带这个键", "activityId" in e2, False)
        check("两处都带 userId", (e1.get("userId"), e2.get("userId")), ("u1", "u1"))

        # ⑤ 关掉开关就不打补丁
        mod3 = FakeMod()
        check("配置可关闭", E.apply_mini_report_fix(mod3, {"fix_mini_report": False}), [])
        try:
            mod3._mini_report(None, "u", "n", "c")
            still_original = False          # 没抛哨兵 = 被换掉了（不对）
        except AssertionError:
            still_original = True           # 抛了哨兵 = 还是上游原版（对）
        check("关掉后 _mini_report 仍是上游原版", still_original, True)
    finally:
        requests.Session = real_session


# ------------------------------------------------------- 16c. 开学季修复 + 序列任务2

def test_school_activity_fix():
    """开学季三个修复点 + `Sequential_Tasks_2` 接线的回归测试。

    🔴 2026-09-20 实测（详见 probe_all_tasks.py / probe_expert_use.py）：
      · `chat_3_times` 永远卡 1/3 —— 上游 `school_run_tasks` 在 3 次循环**外面**建了一个
        conv_id，3 条 chat 事件共用它；服务端按 conversationId 去重 → 只算 1 次。
        改成每条独立 conv 后实测 1/3 → 3/3 → completed → 领奖成功。
      · `_school_fetch_expert` 读 `e["id"]`，而接口返回的是 **`expert_id`** → 拿到空串 →
        上报代码从未执行过；更糟的是它会回落一个服务端不认的假 id `expert-school-01`，
        于是照发不误还打出一句假的 `expert_use ✅`（日志骗人）。
      · ✅ `expert_use`（召唤1次开学季专家）**已攻克**：抓包（用户手动完成）显示真实事件带
        **完整的小程序身份信封**（`ideType: WorkBuddy_MP` / `extName: workbuddy-mp` /
        `ideName: wx_app_cloud` / `machineId` / `product` / `os` / `timezone`…），
        而白天失败的变体**一个身份字段都没带**，还夹着一堆抓包里没有的伪造字段
        （`activityId` / `source=builtin` / `requestModelId` / `reportDelay`…）。
        ⚠️ **根因未用单变量钉死**：失败的 A1/A2 发的**也是** `expert_actual_use`，
        所以"事件名错了"不是根因，别再这么写。
        实测照抄抓包形状后 3 秒内 0/1 → 1/1 completed → 领奖成功。
      · `Sequential_Tasks_2`（「完成 1 次专家对话」200积分+5能量）从没人处理 ——
        上游 `t_sequential_tasks` 只认 `Sequential_Tasks_1`。它 `locked until 2026-09-21`。
    """
    print("\n── 16c. 开学季修复 + 序列任务2 ──")

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    class FakeMod:
        SCHOOL_DOMAIN = "https://www.codebuddy.cn"
        SCHOOL_BASE = "https://www.codebuddy.cn/portal/activity/school"
        SCHOOL_EXPERT_CATEGORY = "16-BackToSchool"
        SCHOOL_ACTIVITY_ID = "school_open_day_2026"
        WRITE_GAP = 0.01

        def __init__(self, expert_payload=None):
            self.expert_payload = expert_payload
            self.calls = []

        def _school_post(self, s, url, body=None):
            return FakeResp(self.expert_payload or {"data": {"experts": []}})

        def _school_fetch_expert(self, s):
            raise AssertionError("不该调用上游原版 _school_fetch_expert")

        def _school_expert_event(self, uid, nick, expert_id, expert_name, conv_id):
            raise AssertionError("不该调用上游原版 _school_expert_event")

        def derive_id(self, uid, kind):
            return "machine-" + str(uid) + "-" + str(kind)

        def _school_report(self, s, uid, nick, events, host=None, desktop=False):
            raise AssertionError("不该调用上游原版 _school_report")

        def api_retry(self, s, method, url, body=None, headers=None):
            self.calls.append((method, url, body, headers))
            return FakeResp({"code": 0})

        def _school_mini_chat_event(self, uid, nick, conv_id):
            return [{"eventCode": "chat_request_send", "conversationId": conv_id,
                     "requestId": conv_id, "userId": uid}]

    # ① 补丁确实替换了四处
    mod = FakeMod()
    check("默认开启并替换四处", E.apply_school_activity_fix(mod, {}),
          ["_school_mini_chat_event→每次新 conversationId",
           "_school_fetch_expert→读 expert_id + 职业名",
           "_school_expert_event→expert_actual_use（抓包真实形状）",
           "_school_report→裸数组报文"])

    # ② 关键修复：同一个固定 conv 参数连调三次，必须得到三个不同 conversationId
    convs = []
    for _ in range(3):
        evs = mod._school_mini_chat_event("u1", "甲", "conv-FIXED")
        convs.append(evs[0]["conversationId"])
        check("requestId 跟随 conversationId", evs[0]["requestId"],
              evs[0]["conversationId"])
    check("三次上报拿到三个不同 conversationId", len(set(convs)), 3)
    check("传进来的固定 conv 被丢弃（不再被去重）", "conv-FIXED" in convs, False)

    # ③ 专家 id：读到 expert_id（这是接口真实字段）；第二值优先**职业名**（抓包 expertTitle）
    m2 = FakeMod({"data": {"experts": [
        {"expert_id": "exp-777", "display_name_zh": "开学季助手",
         "profession_zh": "教育咨询顾问"}]}})
    E.apply_school_activity_fix(m2, {})
    check("读到 expert_id + 职业名", m2._school_fetch_expert(None), ("exp-777", "教育咨询顾问"))

    # ③b 没有 profession_zh 时回落 display_name_zh
    m2b = FakeMod({"data": {"experts": [
        {"expert_id": "exp-778", "display_name_zh": "开学季助手"}]}})
    E.apply_school_activity_fix(m2b, {})
    check("无职业名回落 display_name_zh", m2b._school_fetch_expert(None),
          ("exp-778", "开学季助手"))

    # ④ 兼容只有 id / name 的老形状
    m3 = FakeMod({"data": {"experts": [{"id": "exp-legacy", "name": "旧专家"}]}})
    E.apply_school_activity_fix(m3, {})
    check("兼容只有 id 的老形状", m3._school_fetch_expert(None), ("exp-legacy", "旧专家"))

    # ⑤ 🔴 拿不到真 id 时必须诚实返回空串，绝不回落假 id
    m4 = FakeMod({"data": {"experts": []}})
    E.apply_school_activity_fix(m4, {})
    eid, _ = m4._school_fetch_expert(None)
    check("没有专家时返回空串（不造假 id）", eid, "")
    check("绝不回落 expert-school-01", eid == "expert-school-01", False)

    # ⑥ 开关可关闭
    check("配置可关闭", E.apply_school_activity_fix(FakeMod(),
          {"fix_school_activity": False}), [])

    # ⑦ `_school_report` 必须发**裸数组**（信封会被服务端静默忽略 —— chat_3_times 的真凶）
    #    实测：只换 conv 而仍发信封 → 进度纹丝不动；换裸数组 → 1/3→2/3→3/3 立刻翻页。
    m6 = FakeMod()
    E.apply_school_activity_fix(m6, {})
    evs = [{"eventCode": "chat_request_send", "userId": "u1"}]
    m6._school_report(None, "u1", "甲", evs)
    verb, url, body, _hdrs = m6.calls[-1]
    check("上报用 POST", verb, "POST")
    check("打到 school 域", url, "https://www.codebuddy.cn/v2/report")
    check("body 是【裸数组】而不是信封", isinstance(body, list), True)
    check("事件原样透传（不改字段）", body[0].get("eventCode"), "chat_request_send")
    check("信封里的 common 不见了", "common" in str(body), False)

    # ⑦b host 参数被尊重（上游支持，别写死域）
    m7 = FakeMod()
    E.apply_school_activity_fix(m7, {})
    m7._school_report(None, "u1", "甲", evs, host="https://other.example")
    check("host 参数被尊重", m7.calls[-1][1], "https://other.example/v2/report")

    # ⑦c 🔴 桌面分支必须走上游原版：`desktop_chat_1_time` 就是靠它完成的，
    #     没有证据说明那条路径也认裸数组 → 不碰。
    m8 = FakeMod()
    E.apply_school_activity_fix(m8, {})
    try:
        m8._school_report(None, "u1", "甲", evs, desktop=True)
        desktop_passthru = False          # 没抛哨兵 = 被我们的裸数组版接管了（不对）
    except AssertionError:
        desktop_passthru = True
    check("桌面分支走上游原版（不改成裸数组）", desktop_passthru, True)
    check("桌面分支不误发 school 域", m8.calls, [])

    # ⑦d ✅ 攻克点回归：`expert_use` 必须照抄抓包的真实报文（含小程序身份信封）
    #     ⚠️ 别断言"根因是事件名错了" —— 白天失败的 A1/A2 发的也是 expert_actual_use。
    #     真正可指认的差异是**身份/环境信封缺失** + 多余伪造字段（根因未单变量隔离）。
    #     实测（2026-09-20 账号2/4）：照抄形状后 3 秒内 0/1 → 1/1 → 领奖成功。
    m9 = FakeMod()
    E.apply_school_activity_fix(m9, {})
    evs9 = m9._school_expert_event("uid-9", "张三", "ex_REAL", "电脑操作与排障顾问", "conv-1")
    check("expert 只发 1 条事件", len(evs9), 1)
    e9 = evs9[0]
    check("事件名是 expert_actual_use（不是 expert_summoned）",
          e9.get("eventCode"), "expert_actual_use")
    check("旧事件名 expert_summoned 已消失", "expert_summoned" in str(evs9), False)
    check("id 用真实 expert_id", e9.get("id"), "ex_REAL")
    check("name 与 id 一致（抓包如此）", e9.get("name"), "ex_REAL")
    check("expertTitle 用职业名", e9.get("expertTitle"), "电脑操作与排障顾问")
    check("type=send_message", e9.get("type"), "send_message")
    check("expertType=agent", e9.get("expertType"), "agent")
    check("characterCount 是整数", isinstance(e9.get("characterCount"), int), True)
    check("ideType=WorkBuddy_MP", e9.get("ideType"), "WorkBuddy_MP")
    check("ideName=wx_app_cloud", e9.get("ideName"), "wx_app_cloud")
    check("extName=workbuddy-mp", e9.get("extName"), "workbuddy-mp")
    check("ideVersion=2.4.2", e9.get("ideVersion"), "2.4.2")
    check("extVersion=2.4.2", e9.get("extVersion"), "2.4.2")
    check("product=SaaS", e9.get("product"), "SaaS")
    check("os=ios", e9.get("os"), "ios")
    check("timezone 正确", e9.get("timezone"), "Asia/Shanghai")
    check("userId 透传", e9.get("userId"), "uid-9")
    check("userNickname 透传", e9.get("userNickname"), "张三")
    check("machineId 由 derive_id 生成", e9.get("machineId"), "machine-uid-9-mp-machine")
    check("timestamp 是毫秒整数", isinstance(e9.get("timestamp"), int), True)
    # 🔴 抓包里没有的伪造字段必须一个不留（正是它们让报文形状对不上）
    for bad in ("activityId", "source", "requestModelId", "requestModelName",
                "reportDelay", "messageId", "version", "cost"):
        check("不再夹带伪造字段 %s" % bad, bad in e9, False)
    # 端到端：裸数组 + 形状不变
    m9._school_report(None, "uid-9", "张三", evs9)
    _v, _u, _b, _h = m9.calls[-1]
    check("expert 事件走裸数组", isinstance(_b, list), True)
    check("裸数组里就是 expert_actual_use", _b[0].get("eventCode"), "expert_actual_use")

    # ⑧ Sequential_Tasks_2 接线（属于成长中心，不是开学季）
    import requests
    captured = {"posts": [], "claims": []}

    class FakeSession:
        def __init__(self):
            self.headers = {}

        def post(self, url, json=None, **kw):
            captured["posts"].append(url)

            class _R:
                status_code = 200

            return _R()

    class SeqMod:
        WRITE_GAP = 0.01

        def __init__(self, prog_seq, accept_ok=True):
            # `_mp_prog` 依次吐出这个序列，用完后一直返回最后一个
            self.prog_seq = list(prog_seq)
            self.accept_ok = accept_ok
            self.ran_1 = False

        def t_sequential_tasks(self, s, uid, nick, log):
            self.ran_1 = True          # 原版 _1 逻辑必须仍被执行

        def _mini_report(self, s, uid, nick, conv_id):
            raise AssertionError("不该调用上游原版 _mini_report")

        def _mp_prog(self, s, code):
            if code != "Sequential_Tasks_2":
                return None
            if len(self.prog_seq) > 1:
                return self.prog_seq.pop(0)
            return self.prog_seq[0]

        def _mp_accept(self, s, code):
            return self.accept_ok

        def _mp_claim(self, s, code, log):
            captured["claims"].append(code)
            return True

    real_session = requests.Session
    requests.Session = FakeSession
    try:
        # 服务端没下发 _2 → 什么都不做
        captured["posts"].clear(); captured["claims"].clear()
        sm = SeqMod([None])
        E.apply_mini_report_fix(sm, {})
        sm.t_sequential_tasks(None, "u", "n", quiet)
        check("_1 原逻辑仍被调用", sm.ran_1, True)
        check("没下发 _2 → 不上报", len(captured["posts"]), 0)
        check("没下发 _2 → 不领奖", captured["claims"], [])

        # 已 completed → 直接补领，不再上报
        captured["posts"].clear(); captured["claims"].clear()
        sm2 = SeqMod([("completed", 1, 1)])
        E.apply_mini_report_fix(sm2, {})
        sm2.t_sequential_tasks(None, "u", "n", quiet)
        check("_2 已 completed → 直接补领", captured["claims"], ["Sequential_Tasks_2"])
        check("_2 已 completed → 不重复上报", len(captured["posts"]), 0)

        # 未解锁（accept 失败）→ 静默跳过，不留副作用
        captured["posts"].clear(); captured["claims"].clear()
        sm3 = SeqMod([("not_accepted", 0, 1)], accept_ok=False)
        E.apply_mini_report_fix(sm3, {})
        sm3.t_sequential_tasks(None, "u", "n", quiet)
        check("未解锁 → accept 失败就跳过（不上报）", len(captured["posts"]), 0)
        check("未解锁 → 不领奖", captured["claims"], [])

        # 解锁 → accept → 上报 → 回读 completed → 领奖
        captured["posts"].clear(); captured["claims"].clear()
        sm4 = SeqMod([("not_accepted", 0, 1), ("completed", 1, 1)])
        check("补丁列表含序列任务2", E.apply_mini_report_fix(sm4, {}),
              ["_mini_report→裸数组报文", "t_sequential_tasks→补 Sequential_Tasks_2"])
        sm4.t_sequential_tasks(None, "u", "n", quiet)
        check("解锁后上报了一次", len(captured["posts"]), 1)
        check("回读完成并领奖", captured["claims"], ["Sequential_Tasks_2"])
        check("上报打到 copilot 域（裸数组口径）",
              captured["posts"][0], "https://copilot.tencent.com/v2/report")
    finally:
        requests.Session = real_session


# ------------------------------------------------------- 16d. 开学季兜底扫尾

def test_school_final_sweep():
    """`school_final_sweep` 回归测试：补领「已完成未领」+ 把没抽的转盘抽掉。

    🔴 2026-09-20：上游 `school_run_tasks` 只轮询 5×2=10 秒就 claim，而服务端是
    **延迟入账**的 → 等翻成 completed 时 claim 早跑过去了（实测 6 个账号的
    `desktop_chat_1_time` 就这样「已完成但没领」= 600积分+6抽奖）。
    且转盘机会是**发奖时才给**的，而 `school_lottery` 跑在任务之前 → 那刻余额还是 0。
    这道闸只做「查询 + 领奖 + 抽奖」，不发埋点、不改任务状态（claim/draw 幂等）。
    """
    print("\n── 16d. 开学季兜底扫尾 ──")

    class FakeResp:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    class SweepMod:
        SCHOOL_BASE = "https://www.codebuddy.cn/portal/activity/school"
        ACCOUNTS = [{"note": "甲", "access_token": "tok-A"},
                    {"note": "乙", "access_token": "tok-B"},
                    {"note": "丙", "access_token": ""}]      # 空 token → 必须跳过

        def __init__(self, tasks, balance):
            self.tasks = tasks
            self.balance = balance
            self.claims = []
            self.lotteries = []

        def _school_session(self, tok):
            return "sess-" + tok

        def _school_fetch_tasks(self, s):
            return self.tasks.get(s, []), True

        def _school_claim(self, s, code):
            self.claims.append((s, code))
            return True

        def _school_get(self, s, url):
            return FakeResp({"code": 0,
                             "data": {"chance": {"balance": self.balance}}})

        def school_lottery(self, s, uid, nick, log):
            self.lotteries.append(s)

        def uid_of(self, tok):
            return "uid-" + tok

        def nickname_of(self, tok):
            return "nick-" + tok

    sw = SweepMod({
        "sess-tok-A": [{"task_code": "chat_3_times", "status": "completed"},
                       {"task_code": "share_invite", "status": "claimed"}],
        "sess-tok-B": [{"task_code": "desktop_chat_1_time", "status": "completed"}],
    }, balance=2)

    stats = E.school_final_sweep(sw, [])
    check("只补领 completed（claimed / 其它状态跳过）", sw.claims,
          [("sess-tok-A", "chat_3_times"), ("sess-tok-B", "desktop_chat_1_time")])
    check("补领计数正确", stats["claimed"], 2)
    check("空 token 的账号被跳过", "sess-" not in sw.lotteries, True)
    check("有余额的账号都抽了转盘", len(sw.lotteries), 2)
    check("抽奖账号计数正确", stats["drawn"], 2)

    # 不在活动期 → 整段跳过
    sw2 = SweepMod({}, balance=3)

    def _out_of_period(s):
        return [], False

    sw2._school_fetch_tasks = _out_of_period
    stats2 = E.school_final_sweep(sw2, [])
    check("非活动期不领不抽", (stats2["claimed"], stats2["drawn"]), (0, 0))

    # 没有账号 / 空账号库 → 安全返回
    class Empty(SweepMod):
        ACCOUNTS = []

    check("没有账号时安全返回", E.school_final_sweep(Empty({}, 0), [])["claimed"], 0)


# ------------------------------------------------------------------ 17. 领奖不漏

def test_claim_after_light():
    """🔴 用户报障「任务做完了但没领取奖励」的回归测试。

    根因：补丁只负责「点亮」任务，从不 claim；而服务端对埋点是延迟入账的，
    上游那个领奖循环跑到时任务还是 accepted。这里逐个确认修复点还在。
    """
    print("\n── 17. 点亮后必须领奖（回归：完成未领）──")

    class ClaimMod:
        def __init__(self, status=None):
            self.claimed = []
            self.status = status or {}

        def prog(self, s, code):
            # (状态, 当前, 目标)
            st = self.status.get(code, "completed")
            return st, 1, 1

        def claim(self, s, code, log):
            self.claimed.append(code)

        def task_cn(self, code):
            return code

        BASE = "https://example.invalid"

    # a) claim_if_completed：completed → 领；claimed → 不重复领；
    #    accepted（延迟未到账）→ 不领但不报错
    m = ClaimMod({"A": "completed", "B": "claimed", "C": "accepted"})
    got = E.claim_if_completed(m, None, ["A", "B", "C"], quiet)
    check("completed 的领了、claimed 的跳过", got, ["A"])

    m2 = ClaimMod({"X": "accepted"})
    check("还没到 completed 就不领（交给兜底）",
          E.claim_if_completed(m2, None, "X", quiet), [])

    # b) claim_all_completed：扫出所有「已完成未领」，且不越权处理别的状态
    class SweepMod(ClaimMod):
        def __init__(self):
            super().__init__()
            self._tasks = [
                {"task_code": "T1", "accept_status": "completed"},
                {"task_code": "T2", "accept_status": "claimed"},
                {"task_code": "T3", "accept_status": "completed"},
                {"task_code": "T4", "accept_status": "accepted"},
                {"task_code": "T5", "accept_status": "not_accepted"},
            ]

        def get(self, url, **k):
            class R:
                @staticmethod
                def json():
                    return {"data": {"tasks": SweepMod._tasks_obj}}
            return R()

    SweepMod._tasks_obj = []
    sm = SweepMod()
    SweepMod._tasks_obj = sm._tasks
    got = E.claim_all_completed(sm, _StubSession(sm._tasks), quiet)
    check("兜底只领 completed 的那两个", sorted(got), ["T1", "T3"])
    check("兜底不碰其他状态", not any(x in got for x in ("T2", "T4", "T5")), True)

    # c) exclude 参数生效（预留给需要排除的场景）
    got2 = E.claim_all_completed(sm, _StubSession(sm._tasks), quiet, exclude=("T3",))
    check("exclude 能排除指定任务", sorted(got2), ["T1"])

    # d) 上游 run_account 的领奖循环必须仍在（兜底只是补充，不是替代）
    src = Path(E.VENDOR / "workbuddy_daily.py").read_text(encoding="utf-8")
    check("上游原有领奖循环仍在（兜底是补充不是替代）",
          'accept_status") == "completed"' in src, True)

    # e) 三个补丁都必须接上领奖 —— 少了任何一个，对应任务又会变「完成未领」
    engine_src = Path(E.__file__).read_text(encoding="utf-8")
    for name in ("claim_if_completed", "claim_all_completed", "final_claim_sweep"):
        check("引擎里存在 %s" % name, ("def %s(" % name) in engine_src, True)
    check("credit_skill_via_real_chat 点亮后领奖",
          "claim_if_completed(mod, s, \"skill_1\", emit)" in engine_src, True)
    check("资料库补丁点亮后领奖",
          'claim_if_completed(mod, s, "Library_read", emit)' in engine_src, True)
    check("发现应用补丁点亮后领奖",
          'claim_if_completed(mod, s, ["Buddy_App", "Buddy_App_QQ"], emit)' in engine_src, True)
    check("全账号跑完有兜底清扫",
          "final_claim_sweep(mod, summaries)" in engine_src, True)

    # f) 默认开启，可关
    check("配置默认开启兜底领奖", E.load_config().get("final_claim_sweep", True), True)

    # g) 最终清扫绝不发埋点（只能查+领，不能改任务状态）
    sweep_src = engine_src.split("def final_claim_sweep(")[1].split("\ndef ")[0]
    for bad in ("/v2/report", "report_web_event", "report_desktop_events"):
        check("最终清扫不含埋点上报（%s）" % bad, bad in sweep_src, False)


class _StubSession:
    """给 claim_all_completed 用的最小 session 桩。"""

    def __init__(self, tasks):
        self._tasks = tasks

    def get(self, url, **k):
        tasks = self._tasks

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"data": {"tasks": tasks}}
        return R()


# ------------------------------------------------------------------ 18. 服务化安全

def test_service_safety():
    """🔴 2026-09-20 审计发现的 5 个真实问题的回归测试。

    这些都是「CLI 一次性运行」不会暴露、但「常驻服务反复运行」一定会踩的坑。
    """
    print("\n── 18. 服务化安全（审计修复回归）──")

    import requests

    # 1) dry-run 补丁必须可逆，且不能套娃
    orig = requests.Session.request
    r1 = E.install_dry_run()
    patched = requests.Session.request
    check("dry-run 确实打了补丁", patched is not orig, True)
    r1()
    check("restore() 把 Session.request 换回原实现", requests.Session.request is orig, True)

    r2 = E.install_dry_run()
    r3 = E.install_dry_run()   # 重复调用：应自动先还原再打，不套娃
    chk = requests.Session.request
    r3()
    check("重复 install 不套娃，一次 restore 即还原",
          requests.Session.request is orig, True)
    check("insert 产生的补丁确非原函数", chk is not orig, True)
    del r2  # 已被 r3 的自动还原处理掉

    # 2) BLOCKED_WRITES 每次 install 都清空（不能跨运行累积）
    E.install_dry_run()
    E.BLOCKED_WRITES.append(("POST", "https://example.invalid/x"))
    check("打补丁前先清空计数器（防跨运行累积）", len(E.BLOCKED_WRITES) >= 1, True)
    r4 = E.install_dry_run()
    check("再次 install 后计数器归零", len(E.BLOCKED_WRITES), 0)
    r4()

    # 3) as_emit 归一：None / 非 callable 都要能安全调用
    check("as_emit(None) 返回可调用对象", callable(E.as_emit(None)), True)
    check("as_emit(print) 原样返回（不包壳）", E.as_emit(print) is print, True)

    class _ClaimMod:
        BASE = "https://example.invalid"

        def prog(self, s, code):
            return ("completed", 1, 1)

        def claim(self, s, code, log):
            log("领奖[%s]" % code)   # 上游就是直接调 log(...)，传 None 会炸

    try:
        got = E.claim_if_completed(_ClaimMod(), None, "X", None)
        ok = got == ["X"]
    except Exception:
        ok = False
    check("claim_if_completed(emit=None) 不再 TypeError（关键路径不能丢奖励）", ok, True)

    try:
        got2 = E.claim_all_completed(_ClaimMod(), _StubSession(
            [{"task_code": "Y", "accept_status": "completed"}]), None)
        ok2 = got2 == ["Y"]
    except Exception:
        ok2 = False
    check("claim_all_completed(emit=None) 不再 TypeError", ok2, True)

    # 4) normalize_args：服务层造出来的 args 必须补齐 parse_args 声明的全部字段
    ns = E.normalize_args(live=True, only=2)
    declared = [a.dest for a in E._argparse_actions() if a.dest != "help"]
    missing = [d for d in declared if not hasattr(ns, d)]
    check("normalize_args 补齐全部 args 字段", missing, [])
    check("normalize_args 的覆盖优先生效", (ns.live, ns.only), (True, 2))
    check("normalize_args 未指定的走默认值", ns.no_desktop, False)

    # 5) run() 必须成对还原 stdout（服务里绝不能永久劫持）
    src = Path(E.__file__).read_text(encoding="utf-8")
    run_src = src.split("def run(args, observer=None")[1].split("\ndef main(")[0]
    check("run() 在 finally 里还原 stdout", "sys.stdout = prev_stdout" in run_src, True)
    check("run() 在 finally 里还原 dry-run 补丁", "restore_dry()" in run_src, True)
    check("run() 用 StreamCapture 而非永久替换", "StreamCapture(prev_stdout" in run_src, True)
    check("run() 返回结构化结果（服务层要能判断）", '"ok": True' in run_src, True)

    # 6) 旧实现的两处坏法不该再出现
    check("不再有『无账号就 SystemExit』（服务层要可判断）",
          "没有可用的国内版账号" in src and "raise SystemExit(\"没有可用" not in src, True)


def test_service_watcher():
    """服务版守望者（wb-hub/server.py 里的 Watcher）必须和独立 watch.py 语义一致。

    背景：守望者原本是独立进程 watch.py，2026-09-20 搬进 wb-hub 常驻服务
    （用户要求「所有定时/常驻任务都别依赖单独开窗口」）。两套实现同时在，
    判定语义**必须逐条对齐**，否则切账号补跑的行为会悄悄变。

    ⚠️ 这一段直接**读源码做断言**，不 import server.py —— 那会真的起调度线程、
    连网络、写配置，在自测里是不该有的副作用。
    """
    print("\n── 19. 服务版守望者（切账号自动补跑）──")

    # 两种布局都要认：
    #   · 本机开发布局  <root>/wb-task-hub/  + <root>/wb-hub/
    #   · 开源仓库布局  <root>/tasks/engine/ + <root>/tasks/service/
    # 之前只认前者 → 别人 clone 下来这一组会因为找不到文件而**整组静默跳过**，
    # 自检变成"绿但没跑"，是最坏的一种失败。
    _here = Path(__file__).resolve().parent
    hub = next((p for p in (_here.parent / "wb-hub" / "server.py",
                            _here.parent / "service" / "server.py") if p.exists()),
               _here.parent / "wb-hub" / "server.py")
    if not hub.exists():
        check("能找到 server.py（wb-hub/ 或 tasks/service/）", str(hub), "(存在)")
        return
    src = hub.read_text(encoding="utf-8")

    # 1) 判定语义必须与独立 watch.py 完全一致（同参数同输入 → 同结论）
    cls = src.split("class Watcher:")[1].split("\nWATCH = Watcher()")[0]
    ev_body = cls.split("def evaluate(")[1].split("\n    # ---- 轮询 ----")[0]
    for token in ('return "no-uid"', 'return "observe"', 'return "idle"',
                  'return "cooldown"', 'return "run"'):
        check("evaluate 保留分支 %s" % token, token in ev_body, True)
    check("冷却判据与独立版一致（min_gap_minutes）",
          "min_gap_minutes" in ev_body, True)
    check("首次观测默认只记录（run_on_start 控制）",
          "run_on_start" in ev_body, True)

    # 2) 安全硬约束：服务里的守望者同样不许有改认证文件 / 杀进程的能力
    #    ⚠️ 只在 **Watcher 类体**（真正会跑的逻辑）里查，不查整个 server.py：
    #    server.py 顶部注释里就写着「绝不 taskkill WorkBuddy」这句承诺，
    #    拿整份源码当样本会把这句承诺本身判成违规（曾误报一次）。
    for bad in ("swap_info", "restore_info", "taskkill"):
        check("服务版守望者不含 %s" % bad, bad in cls, False)
    check("服务版守望者只用只读接口读 uid", "read_desktop_uid" in cls, True)

    # 3) 让路机制：全量在跑时不能抢同一个账号
    check("有任务在跑时让路（ENGINE.running）", "ENGINE.running" in cls, True)

    # 4) 认不出账号就什么都不做（绝不猜一个账号去顶）
    check("认不出账号时不跑", "不猜账号" in cls, True)

    # 5) 防抖：uid 变化后要等稳定再动手
    check("有防抖等待（debounce）", "_debounce_until" in cls and "debounce_seconds" in cls, True)

    # 6) 服务化必须解决的坑（同 run() 那几条）：线程异常不能让它死掉
    check("轮询异常被兜住并计数（守望者不能死）",
          "false_alarms += 1" in cls, True)

    # 7) 🔴 配置深合并：点开关（只发 enabled）不能把其它参数冲掉
    save = src.split("def save_schedule(")[1].split("\ndef ")[0]
    check("save_schedule 对 watch 做深合并（不是整段替换）",
          'if k == "watch" and isinstance(v, dict)' in save, True)

    # 8) 停止后 is_alive 必须为 False（否则页面显示「运行中」骗人）
    check("is_alive 会看停止标志", "not self._stop.is_set()" in cls, True)

    # 9) 🔴 避免双份守望者：独立 watch.py 必须默认给服务版让路
    #    engine 之间没有互斥锁，两个守望者同时补跑同一账号会各跑一遍全量任务。
    wpath = Path(__file__).resolve().parent / "watch.py"
    wsrc = wpath.read_text(encoding="utf-8")
    check("独立 watch.py 会让路给服务版（查 /api/watch）",
          "127.0.0.1:8793/api/watch" in wsrc, True)
    check("让路判定看 enabled 且 alive",
          'd.get("enabled")' in wsrc and 'd.get("alive")' in wsrc, True)
    check("让路可被 --force 覆盖", '"--force"' in wsrc, True)
    check("让路判定失败时放行（不误伤独立跑）",
          "服务没起 / 接口不可用 → 允许独立跑" in wsrc, True)
    check("默认让路的那一行真的调用了", "and service_watcher_active()" in wsrc, True)


# ------------------------------------------------------------------ 21. 夜猫子有界重试

def test_black_cat_bounded():
    """夜猫子：每晚最多发 N 次；**当日计数一到账就停**。

    服务端语义：夜间23点-次日8点，新建对话并成功使用「GLM-5.2」，每天 1 次，累计 3 天。
    🔴 上游 `for attempt in range(8)` 只在 `cur >= tgt(3)` 时 break →
       因为"每天只计 1 次"，当晚第 1 次成功后 cur 只 +1（仍 <3）→ **永不提前退出** →
       每个账号每晚硬发 8 次真实 GLM-5.2 对话（7 账号 ≈ 56 次/夜）。本组就是它的回归护栏。

    ⚠️ 模拟器必须**可变状态**地模拟服务端"每天只 +1"的语义：
       如果用"预置读数列"来 mock，第二次读就会提前返回新值，测不出真实行为。

    ⚠️ 本组会把 `E.BLACK_CAT_STATE` 指向临时目录：否则自检会写进真实 `config/`，
       且各子用例复用同一个 uid 时会因「本夜已计过」互相误跳过（每个用例用独立 uid）。
    """
    print("\n── 21. 夜猫子有界重试 ──")
    import time as _real_time
    import datetime as _dt
    import engine as E

    BJ = _dt.timezone(_dt.timedelta(hours=8))

    def _bj(y, mo, d, h, mi=0):
        """构造北京时间 aware datetime（夜编号逻辑依赖真实 tz，不能只给一个 hour）。"""
        return _dt.datetime(y, mo, d, h, mi, tzinfo=BJ)

    class _FastTime:
        """把 sleep 变空转，其余属性透传给真正的 time（否则测试要跑几十秒）。"""

        def __getattr__(self, k):
            return getattr(_real_time, k)

        def sleep(self, *a, **k):
            pass

    class FakeMod:
        """忠实模拟服务端：一次成功上报最多让当日计数 +1（"每天 1 次"）。"""

        def __init__(self, night=True, start=0, target=3, status="in_progress",
                     counts=True, reply="好的", now=None):
            self.night = night
            self.cur = start
            self.tgt = target
            self.status = status
            self.counts = counts          # 本次上报是否能让当日计数到账
            self.reply = reply
            self.chats = 0
            self.reports = 0
            self.prompts = []
            # 真实 aware datetime：_night_key() 要用 .hour 和 .timestamp()，光有 hour 不够
            self.now = now or _bj(2026, 9, 20, 23, 30)
            self.t_black_cat = lambda *a, **k: None

        def prog(self, s, code):
            return self.status, self.cur, self.tgt

        def within_night_window(self):
            return self.night

        def beijing_now(self):
            return self.now

        def webchat(self, s, name, prompt):
            self.chats += 1
            self.prompts.append(prompt)
            return "conv-%d" % self.chats, self.reply

        def chat_request_events(self, uid, nick, conv, prompt, txt):
            return ([{"eventCode": "chat_request_send"}], "rid")

        def report(self, s, uid, nick, evs):
            self.reports += 1
            if self.counts:
                self.cur += 1
                if self.cur >= self.tgt:
                    self.status = "completed"
            return 200

    import shutil
    import tempfile
    orig_time = E.time
    orig_state = E.BLACK_CAT_STATE            # 🔴 必须换到临时目录，
    tmpdir = Path(tempfile.mkdtemp(prefix="wbhub-cat-"))   # 否则自检会写进真实 config/
    E.time = _FastTime()
    E.BLACK_CAT_STATE = tmpdir / "black_cat_nights.json"
    try:
        # ① 补丁返回值 / 替换生效
        m0 = FakeMod()
        labels = E.apply_black_cat_fix(m0, {})
        check("返回补丁标签", labels, ["t_black_cat→有界重试+当日到账即停"])
        check("t_black_cat 已被替换", getattr(m0.t_black_cat, "__name__", ""), "fixed")

        # ② 非夜间窗口：一次对话都不发
        m1 = FakeMod(night=False, start=0)
        E.apply_black_cat_fix(m1, {})
        m1.t_black_cat(None, "u2", "n", lambda *_: None)
        check("非夜间窗口：0 次对话", m1.chats, 0)

        # ③ 夜间 + 当日计数到账 → **只发 1 次**（核心护栏）
        m2 = FakeMod(night=True, start=0, target=3)
        E.apply_black_cat_fix(m2, {})
        m2.t_black_cat(None, "u3", "n", lambda *_: None)
        check("当日计数到账 → 只发 1 次对话（上游会发 8 次）", m2.chats, 1)
        check("只上报 1 次", m2.reports, 1)
        check("进度确实 +1（0 → 1）", m2.cur, 1)

        # ④ 已完成 3/3 → 一次都不发
        m3 = FakeMod(night=True, start=3, target=3, status="claimed")
        E.apply_black_cat_fix(m3, {})
        m3.t_black_cat(None, "u4", "n", lambda *_: None)
        check("已完成：0 次对话", m3.chats, 0)

        # ⑤ 上报完全不生效 → 上限收紧到 black_cat_attempts（远小于上游的 8）
        m4 = FakeMod(night=True, start=0, counts=False)
        E.apply_black_cat_fix(m4, {"black_cat_attempts": 2})
        m4.t_black_cat(None, "u5", "n", lambda *_: None)
        check("上报无效时最多发 black_cat_attempts 次", m4.chats, 2)
        check("远小于上游的 8 次", m4.chats < 8, True)

        # ⑥ 无回复 → 不谎报成功（不上报），且仍受上限约束
        m5 = FakeMod(night=True, start=0, reply="")
        E.apply_black_cat_fix(m5, {"black_cat_attempts": 2})
        m5.t_black_cat(None, "u6", "n", lambda *_: None)
        check("无回复时不谎报成功（不上报）", m5.reports, 0)
        check("无回复时按上限重试", m5.chats, 2)

        # ⑦ 配置可关闭
        m6 = FakeMod()
        check("配置可关闭", E.apply_black_cat_fix(m6, {"fix_black_cat": False}), [])

        # ⑧ 关键语义：一夜最多只推进 1 天，绝不在一夜内刷满 3/3
        m7 = FakeMod(night=True, start=0, target=3)
        E.apply_black_cat_fix(m7, {})
        m7.t_black_cat(None, "u8", "n", lambda *_: None)
        check("一夜最多推进 1（不会刷满 3/3）", m7.cur, 1)
        check("一夜只发 1 次对话", m7.chats, 1)

        # ⑨ 「本夜已计过」：同一夜被再次触发（手动跑 / 切换补跑 / 多轮调度）→ 跳过
        m8 = FakeMod(night=True, start=0, target=3)
        E.apply_black_cat_fix(m8, {})
        m8.t_black_cat(None, "u9", "n", lambda *_: None)
        check("同一夜首轮正常发 1 次", m8.chats, 1)
        E.apply_black_cat_fix(m8, {})
        m8.t_black_cat(None, "u9", "n", lambda *_: None)
        check("同一夜第二次触发：不再发对话", m8.chats, 1)
        check("同一夜第二次触发：计数不会被推成 2", m8.cur, 1)

        # ⑩ 凌晨 00:30 仍属同一夜（夜 = 23:00-次日08:00）→ 依然跳过
        m9 = FakeMod(night=True, start=1, now=_bj(2026, 9, 21, 0, 30))
        E.apply_black_cat_fix(m9, {})
        m9.t_black_cat(None, "u9", "n", lambda *_: None)
        check("次日 00:30 仍算同一夜：跳过", m9.chats, 0)

        # ⑪ 到了新的一夜 → 正常发，且推进到 2/3
        m10 = FakeMod(night=True, start=1, now=_bj(2026, 9, 21, 23, 30))
        E.apply_black_cat_fix(m10, {})
        m10.t_black_cat(None, "u9", "n", lambda *_: None)
        check("新的一夜：正常发 1 次", m10.chats, 1)
        check("新的一夜：推进到 2/3", m10.cur, 2)

        # ⑫ guard 可配置关闭（关了就该放行重复，方便排查）
        m11 = FakeMod(night=True, start=0, target=3)
        E.apply_black_cat_fix(m11, {"black_cat_night_guard": False})
        m11.t_black_cat(None, "u11", "n", lambda *_: None)
        E.apply_black_cat_fix(m11, {"black_cat_night_guard": False})
        m11.t_black_cat(None, "u11", "n", lambda *_: None)
        check("guard 关闭后可重复触发", m11.chats, 2)

        # ⑬ 未到账不落账 → 保险丝不能反过来把任务卡死
        m12 = FakeMod(night=True, start=0, counts=False)
        E.apply_black_cat_fix(m12, {"black_cat_attempts": 1})
        m12.t_black_cat(None, "u12", "n", lambda *_: None)
        check("上报未到账时不记「本夜已计过」",
              json.loads(E.BLACK_CAT_STATE.read_text(encoding="utf-8")).get("u12"), None)

        # ⑭ fail-open：状态文件读不出（这里让路径指向一个目录）也不能挡住任务
        broken = tmpdir / "as_dir"
        broken.mkdir()
        E.BLACK_CAT_STATE = broken
        m13 = FakeMod(night=True, start=0, target=3)
        E.apply_black_cat_fix(m13, {})
        m13.t_black_cat(None, "u13", "n", lambda *_: None)
        check("状态不可读时放行（fail-open）", m13.chats, 1)
    finally:
        E.time = orig_time
        E.BLACK_CAT_STATE = orig_state
        shutil.rmtree(tmpdir, ignore_errors=True)


# ------------------------------------------------------------------ 22. 已完成即跳过

def test_done_skip():
    """🔴「已完成的任务直接跳过」—— 2026-09-22 用户要求的回归护栏。

    用户原话：有些对话任务要用模型消耗积分，已完成再跑一遍就是浪费；
             应该**先检查任务的完成状态再去判定做不做**。

    本组锁五件事：
      ① `task_done` 的判据 = `completed/claimed` **或** `progress 已满`。
         后者是关键：服务端延迟入账时 progress 满而状态还是 accepted，
         旧判据会认为"没完成" → 再发一次**真实模型对话**（白烧额度）。
      ② 查不到该 code / 查询抛异常 → 一律算「未完成」——宁可多做，绝不静默漏做。
      ③ 守卫只在「该函数负责的 code **全部**已完成」时整体跳过；任一未完成就照常调用。
      ④ 认不出 requests 会话时绝不错杀。
      ⑤ 开跑前的预检**只发 GET**，一个写请求都不发。
    """
    print("\n── 22. 已完成即跳过 ──")
    import contextlib
    import io

    class FakeSession:
        """只记请求，不联网。"""

        def __init__(self):
            self.verbs = []

        def get(self, *a, **k):
            self.verbs.append("GET")
            raise RuntimeError("self-test 不联网")

        def post(self, *a, **k):
            self.verbs.append("POST")
            raise RuntimeError("self-test 不联网")

    class FakeMod:
        BASE = "https://base.invalid"

        def __init__(self, states=None):
            self.states = dict(states or {})
            self.calls = []

        def __getattr__(self, name):
            """给任何 `t_*` 名字都现造一个可调用对象。

            🔴 必须是**通用**的：守卫表有 13 条，假模块少摆一个函数，那一处守卫就会
               `if not callable(...)` 静默不生效 —— 本项目踩过这个老坑（见 MEMORY）。
            """
            if name.startswith("t_"):
                def _rec(*a, **k):
                    self.calls.append(name)
                return _rec
            raise AttributeError(name)

        def prog(self, s, code):
            return self.states.get(code, (None, None, None))

        def task_cn(self, code):
            return code

    s = FakeSession()

    # ---- ① 判据 ----
    m = FakeMod({"a": ("completed", 1, 1), "b": ("claimed", 1, 1),
                 "c": ("accepted", 1, 1), "d": ("accepted", 0, 1),
                 "e": (None, None, None), "f": ("in_progress", 0, 0)})
    check("completed → 已完成", E.task_done(m, s, "a")[0], True)
    check("claimed → 已完成", E.task_done(m, s, "b")[0], True)
    check("🔴 accepted 但 progress 已满 1/1 → 已完成（延迟入账不许重跑）",
          E.task_done(m, s, "c")[0], True)
    check("accepted 0/1 → 未完成", E.task_done(m, s, "d")[0], False)
    check("查不到该 code → 未完成（宁可多做，不可漏做）",
          E.task_done(m, s, "e")[0], False)
    check("target=0 不算已满（别把 0/0 当完成）", E.task_done(m, s, "f")[0], False)

    # ---- ② 异常与兜底口径 ----
    class BoomProg(FakeMod):
        def prog(self, s, code):
            raise RuntimeError("模拟接口 500")

    check("查询抛异常 → 未完成（不静默跳过）", E.task_done(BoomProg(), s, "a")[0], False)

    class MpMod(FakeMod):
        """成长中心口径查不到时，回落到小程序口径。"""

        def prog(self, s, code):
            return self.states.get(code) or (None, None, None)

        def _mp_prog(self, s, code):
            return self.states.get(code, (None, None, None))

    check("成长中心查不到 → 回落小程序口径（Sequential_Tasks_* 就在那边）",
          E.task_done(MpMod({"Sequential_Tasks_1": ("completed", 1, 1)}), s,
                      "Sequential_Tasks_1")[0], True)

    # ---- ③ 守卫行为 ----
    m2 = FakeMod({"Expert_team_use_3": ("completed", 1, 1),
                  "Library_read": ("accepted", 1, 1),          # 延迟入账：状态没翻但进度满
                  "create_canvas": ("completed", 1, 1),
                  "automation_1": ("completed", 1, 1),
                  "playbook_prompt": ("accepted", 0, 1)})      # 这项还没做 → 不能整函数跳过
    def _silent(fn, *a):
        """守卫命中时会 print 一行「⏭️ 跳过…」，这里吞掉，别让它淹了自检结果。

        ⚠️ 重定向只包住「被调用」这一下，**不能**把 check() 也裹进去 ——
           否则 ✅/❌ 会一起被吞进 StringIO，自检看着像没跑。
        """
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*a)

    applied = E.apply_done_skip(m2, {})
    check("守卫表 13 条全部挂上（少一条就是静默不生效）",
          len([x for x in applied if "已完成即跳过" in x]), len(E.DONE_GUARD_TABLE))
    s2 = FakeSession()
    _silent(m2.t_team_3, s2, "uid", "nick", quiet)
    check("全部已完成 → 不执行 t_team_3", m2.calls, [])
    _silent(m2.t_library, s2, "uid", "nick", quiet)
    check("progress 已满也算完成 → 不执行 t_library", m2.calls, [])
    _silent(m2.t_canvas_automation, s2, "uid", "nick", quiet)
    check("三项里有一项没完成 → 照常执行（绝不漏做）",
          m2.calls, ["t_canvas_automation"])

    # ---- ④ 认不出会话 → 绝不错杀 ----
    m3 = FakeMod({"Expert_team_use_3": ("completed", 1, 1)})
    E.apply_done_skip(m3, {})
    _silent(m3.t_team_3, "这不是会话", "uid", "nick", quiet)
    check("认不出 requests 会话 → 照常执行", m3.calls, ["t_team_3"])

    # ---- ⑤ t_chat_n 的 code 在第 5 个位置参数上 ----
    m4 = FakeMod({"chat_5": ("completed", 5, 5)})
    E.apply_done_skip(m4, {})
    _silent(m4.t_chat_n, FakeSession(), "uid", "nick", quiet, "chat_5", 5, ["p"])
    check("t_chat_n 取到 code 参数 → 已完成即跳过", m4.calls, [])
    _silent(m4.t_chat_n, FakeSession(), "uid", "nick", quiet, "Model_chat_GLM5.2", 1, ["p"])
    check("同函数另一个 code 未完成 → 照常执行", m4.calls, ["t_chat_n"])

    # ---- ⑥ 开关 ----
    m5 = FakeMod({"Expert_team_use_3": ("completed", 1, 1)})
    check("skip_done_tasks=false → 一条守卫都不挂",
          E.apply_done_skip(m5, {"skip_done_tasks": False}), [])

    # ---- ⑦ 上游改名护栏：守卫名对不上就是静默失效 ----
    src = (Path(__file__).resolve().parent / "vendor" / "workbuddy_daily.py").read_text(
        encoding="utf-8")
    upstream_defs = {n.name for n in ast.parse(src).body
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    missing = [fn for fn, _ in E.DONE_GUARD_TABLE if fn not in upstream_defs]
    check("🔴 守卫表里的函数名在上游 vendor 里都存在（否则守卫静默失效）", missing, [])
    check("task_done 依赖的上游 prog() 存在", "prog" in upstream_defs, True)
    check("task_done 回落依赖的 _mp_prog() 存在", "_mp_prog" in upstream_defs, True)

    # ---- ⑧ 预检：只读 + 先给结论 ----
    class PreflightSession:
        def __init__(self):
            self.writes = 0

        def get(self, *a, **k):
            class _R:
                status_code = 200

                @staticmethod
                def json():
                    return {"data": {"tasks": [
                        {"task_code": "Expert_team_use_3", "accept_status": "completed",
                         "progress": {"current": 3, "target": 3}},
                        {"task_code": "chat_5", "accept_status": "accepted",
                         "progress": {"current": 0, "target": 5}},
                    ]}}
            return _R()

        def post(self, *a, **k):
            self.writes += 1
            raise RuntimeError("预检绝不能发写请求")

    class TasksMod(FakeMod):
        def __init__(self):
            FakeMod.__init__(self)
            self.session = PreflightSession()
            self.runs = 0

        def run_account(self, idx, acc, do_desktop):
            self.runs += 1
            return [], {}

        def new_api(self, tok):
            return self.session

    m6 = TasksMod()
    E.apply_done_skip(m6, {})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m6.run_account(1, {"access_token": "tok"}, False)
    out = buf.getvalue()
    check("预检全程只读（0 个写请求）", m6.session.writes, 0)
    check("预检之后仍照常执行原流程", m6.runs, 1)
    check("预检打印「跳过（已完成）」结论", "跳过（已完成）" in out, True)
    check("预检打印「本轮要做」", "本轮要做" in out, True)
    check("💬 预检单独标出「要调用模型、会耗额度」的项", "会耗额度" in out, True)
    check("💬 未完成的对话类任务被点名为待做", "chat_5" in out, True)

    # 没有 token 的账号不该白拉一次接口
    m7 = TasksMod()
    E.apply_done_skip(m7, {})
    m7.runs = 0
    with contextlib.redirect_stdout(io.StringIO()):
        m7.run_account(1, {"note": "无 token"}, False)
    check("无 AT 的账号不预检（不白拉接口）", m7.runs, 1)


# --------------------------------------------------- 23. 抽奖/盲盒不设上限

def test_drain_rewards():
    """🔴「抽奖 / 开盲盒不设上限，每次查余额，有多少用多少」—— 2026-09-22 用户要求。

    实测上游两处不一致（vendor 910-960）：
      · `t_blindbox` 有硬上限 `min(affordable, 5)` → 额度 30 也只开 5 个；
      · `t_lottery` 只读一次次数就开抽 → 中途到账的新次数被漏掉。

    本组锁：**每个任务都不再有人工上限**、**每轮重查余额**、
            接口不扣数时能自己收手（防死循环）、余额为 0 时一次都不发。
    """
    print("\n── 23. 抽奖 / 盲盒：不设上限，用干净 ──")
    import time as _real_time

    class _FastTime:
        """把 sleep 变空转（30 次开盒要跑 45 秒，不然自检太慢）。"""

        def __getattr__(self, k):
            return getattr(_real_time, k)

        def sleep(self, *a, **k):
            pass

    class Resp:
        def __init__(self, payload):
            self._p = payload
            self.status_code = 200

        def json(self):
            return self._p

    class Sess:
        """假会话：按 URL 后缀分发，余额状态挂在 FakeMod 上。"""

        def __init__(self, m):
            self.m = m

        def get(self, url, *a, **k):
            m = self.m
            if "lottery/chances" in url:
                return Resp({"code": 0, "data": {"balance": m.chances}})
            if "buddy/quota" in url:
                return Resp({"code": 0, "data": {"balance": m.energy,
                                                 "affordable": m.quota}})
            raise AssertionError("未预期的 GET %s" % url)

        def post(self, url, *a, **k):
            m = self.m
            if "lottery/draw" in url:
                m.draws += 1
                if not m.stuck:
                    m.chances = max(0, m.chances - 1)
                if m.draws in m.grant_at_draw:      # 模拟"抽奖中途又到账了"
                    m.chances += m.grant_at_draw[m.draws]
                return Resp({"code": 0, "data": {"prize_name": "6积分"}})
            if "buddy/open" in url:
                m.opens += 1
                if not m.stuck:
                    m.quota = max(0, m.quota - 1)
                return Resp({"code": 0, "data": {"results": [
                    {"instance": {"name": "猫猫", "rarity": "R"}}]}})
            raise AssertionError("未预期的 POST %s" % url)

    class FakeMod:
        BASE = "https://base.invalid"

        def __init__(self, quota=0, chances=0, stuck=False, energy=999,
                     grant_at_draw=None):
            self.quota = quota
            self.chances = chances
            self.energy = energy
            self.stuck = stuck                    # True → 接口不扣数（模拟异常）
            self.grant_at_draw = grant_at_draw or {}
            self.opens = 0
            self.draws = 0
            self.ACCOUNTS = []

        def t_lottery(self, *a, **k):
            raise AssertionError("t_lottery 应已被替换")

        def t_blindbox(self, *a, **k):
            raise AssertionError("t_blindbox 应已被替换")

        def new_api(self, tok):
            return Sess(self)

        def uid_of(self, tok):
            return "uid"

        def nickname_of(self, tok):
            return "nick"

    orig_time = E.time
    E.time = _FastTime()
    try:
        # ① 盲盒：30 个额度必须**全开**（上游会卡在 5 个）
        m = FakeMod(quota=30)
        labels = E.apply_drain_rewards(m, {})
        check("两个函数都被替换",
              sorted(labels), ["t_blindbox→去掉 min(…,5) 硬上限",
                               "t_lottery→每轮重查（不设上限）"])
        n = m.t_blindbox(Sess(m), "uid", "nick", quiet)
        check("🔴 额度 30 → 开满 30 个（上游只开 5 个）", m.opens, 30)
        check("返回真实用量 30", n, 30)

        # ② 抽奖：中途到账的新次数也要用掉（上游只按首读开抽）
        m2 = FakeMod(chances=2, grant_at_draw={2: 3})
        E.apply_drain_rewards(m2, {})
        n2 = m2.t_lottery(Sess(m2), "uid", "nick", quiet)
        check("🔴 抽到 0 才停（中途补发的 3 次也被用掉）→ 共 5 次", m2.draws, 5)
        check("返回真实用量 5", n2, 5)
        check("余额被抽空", m2.chances, 0)

        # ③ 余额为 0 → 一次都不发
        m3 = FakeMod(quota=0, chances=0)
        E.apply_drain_rewards(m3, {})
        m3.t_blindbox(Sess(m3), "uid", "nick", quiet)
        m3.t_lottery(Sess(m3), "uid", "nick", quiet)
        check("余额为 0：一次都不发", [m3.opens, m3.draws], [0, 0])

        # ④ 接口不扣数 → 连续 3 轮后自己收手（防死循环 / 防空转刷接口）
        m4 = FakeMod(quota=10, stuck=True)
        E.apply_drain_rewards(m4, {})
        m4.t_blindbox(Sess(m4), "uid", "nick", quiet)
        check("接口不扣数时收手（不为 0 就无限试）", m4.opens, 3)

        # ⑤ 保险丝可配（不是使用上限，只是防跑飞）
        m5 = FakeMod(quota=10, stuck=True)
        E.apply_drain_rewards(m5, {"drain_max_rounds": 1})
        m5.t_blindbox(Sess(m5), "uid", "nick", quiet)
        check("drain_max_rounds=1 → 只试 1 轮", m5.opens, 1)

        # ⑥ 开关可关
        m6 = FakeMod()
        check("drain_rewards=false → 不替换",
              E.apply_drain_rewards(m6, {"drain_rewards": False}), [])

        # ⑦ 收尾清空：把「领奖后才到账」的余额用掉并如实统计
        m7 = FakeMod(quota=3, chances=2)
        m7.ACCOUNTS = [{"access_token": "tok1", "note": "甲"}]
        E.apply_drain_rewards(m7, {})
        st = E.final_drain_sweep(m7, {})
        check("收尾清空：抽奖 2 次", st["lottery"], 2)
        check("收尾清空：开盒 3 个", st["blindbox"], 3)
        check("收尾清空：覆盖 1 个账号", st["accounts"], 1)
        check("收尾清空后余额清零", [m7.opens, m7.draws], [3, 2])

        # ⑧ 无 AT 的账号不建会话，也不该炸
        m8 = FakeMod(quota=1)
        m8.ACCOUNTS = [{"note": "无 token"}]
        E.apply_drain_rewards(m8, {})
        st8 = E.final_drain_sweep(m8, {})
        check("无 AT 的账号被跳过", [st8["accounts"], m8.opens], [0, 0])
    finally:
        E.time = orig_time


def test_zh_status():
    """日志状态中文化 —— 用户 2026-09-20 要求「日志一律中文」。

    🔴 关键红线：只翻译**独立成词**的状态枚举，绝不能碰任务代号 / URL / 域名，
    否则会把 `desktop_chat_1_time` 这种接口主键译坏，任务直接失效。
    """
    print("\n── 20. 日志状态中文化 ──")
    sys.argv = ["workbuddy_daily"]
    import engine as E

    check("zh_status 存在", callable(getattr(E, "zh_status", None)), True)

    # 1) 常见状态枚举都译成中文
    pairs = [("claimed", "已领取"), ("completed", "已完成"), ("accepted", "已接受"),
             ("not_accepted", "未接受"), ("already_claimed", "已领过"),
             ("in_progress", "进行中"), ("skipped", "已跳过")]
    for en, cn in pairs:
        got = E.zh_status("  任务X: %s 1/1" % en)
        check("zh_status(%s) → %s" % (en, cn), cn in got and en not in got, True)

    # 2) 🔴 `not_accepted` 必须整体命中，不能被 `accepted` 咬掉一半
    got = E.zh_status("skill_1: not_accepted None/None")
    check("not_accepted 不被 accepted 误替换（无残留 not_）", "not_" not in got, True)
    check("not_accepted 译对了", "未接受" in got, True)

    # 3) 🔴 任务代号 / URL / 域名一律不许动
    guards = [
        "desktop_chat_1_time: claimed 已完成/已领，跳过",
        "wb_wechat_oa_subscribe_task 关注公众号",
        "wb_wechat_oa_subscribe_task",
        "https://www.workbuddy.cn/path",
        "RichMeow_Chat: accepted 0/1",
        "Model_chat_GLM5.2: claimed 1/1",
        "share_invite: claimed",
        "校园日活动: accepted 0/1（服务端暂未关联）",
    ]
    for s in guards:
        got = E.zh_status(s)
        for code in ("desktop_chat_1_time", "wb_wechat_oa_subscribe_task",
                     "workbuddy.cn", "RichMeow_Chat", "Model_chat_GLM5.2",
                     "share_invite", "校园日活动"):
            if code in s:
                check("任务代号/域名原样保留: %s" % code, code in got, True)

    # 4) 同时含状态词和代号的混合行，两边都要对
    got = E.zh_status("   desktop_chat_1_time: claimed 已完成/已领，跳过")
    check("混合行：代号保留", "desktop_chat_1_time" in got, True)
    check("混合行：状态被翻译", "已领取" in got and "claimed" not in got, True)

    # 5) 非字符串输入不炸
    check("zh_status 对 None 不炸", E.zh_status(None) is None, True)

    # 5b) 上游写的是 `log("... 已 %s" % st)` → 译完会出现「已 已领取」叠字，必须收掉
    for raw, want in [("   和平精英主题: 已 claimed", "和平精英主题: 已领取"),
                      ("   体验资料库: 已 claimed", "体验资料库: 已领取"),
                      ("   已 completed", "已已完成")]:
        got = E.zh_status(raw)
        check("叠字被收掉: %s" % raw.strip(), "已 已" not in got and "已已" not in got, True)
    check("叠字修复后仍是正确中文",
          E.zh_status("   体验资料库: 已 claimed").strip() == "体验资料库: 已领取", True)
    # 正文里正常的「已」不许被动
    check("正文正常的「已」不受影响",
          E.zh_status("   已完成这个任务").strip() == "已完成这个任务", True)

    # 6) 🔴 必须在输出层生效（三个出口一次覆盖），而不是改上游
    check("StreamCapture.write 里调用了 zh_status",
          "data = zh_status(data)" in Path(E.__file__).read_text(encoding="utf-8"), True)
    vendor = (Path(E.__file__).resolve().parent / "vendor" / "workbuddy_daily.py")
    check("上游 vendor 未被改动（不塞中文替换逻辑）",
          "zh_status" not in vendor.read_text(encoding="utf-8"), True)

    # 7) 🔴 最终报告的「未完成」清单必须每次刷新，不能只在「这次领到了」时才刷。
    #    实测踩坑：skill_1 是单账号阶段之后才被服务端翻成 completed 的，
    #    兜底「无奖可领」→ 走不到刷新分支 → 报告仍显示过期结论。
    src = Path(E.__file__).read_text(encoding="utf-8")
    sweep = src.split("def final_claim_sweep(")[1].split("\ndef run(")[0]
    check("兜底清单刷新不在 if got 分支内",
          sweep.index('sm["rest"] =') > sweep.index('if got:'), True)
    check("兜底清单刷新每个账号都跑（缩进与 if got 同级）",
          '\n            sm = by_idx.get(idx)' in sweep, True)


def main():
    print("=" * 70)
    print("引擎接线自检")
    print("=" * 70)
    test_resolve()
    test_patch()
    test_patch_survives_failure()
    test_engine_is_harmless()
    test_switch_account_store_is_readonly()
    test_only_selection()
    test_args_attributes_are_declared()
    test_desktop_scope_and_detection()
    test_service_safety()
    test_watcher_decisions()
    test_service_watcher()
    test_post_claim_recheck()
    test_skill_real_chat()
    test_library_report_shape()
    test_report_shape_fix()
    test_mini_report_shape()
    test_school_activity_fix()
    test_school_final_sweep()
    test_claim_after_light()
    test_black_cat_bounded()
    test_done_skip()
    test_drain_rewards()
    test_zh_status()
    print("\n" + "=" * 70)
    if FAILED:
        print("❌ %d 项不符合预期：" % len(FAILED))
        for f in FAILED:
            print("   · %s" % f)
        return 1
    print("✅ 全部通过，引擎接线与安全约束成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
