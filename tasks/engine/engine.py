#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wb-task-hub 引擎

把上游 WorkBuddy-Daily 的任务逻辑跑起来，但只跑 workbuddy-switch 没有覆盖的部分：
  - 排除：每日签到（t_sign）、猫猫旅行（t_travel）—— 这两个 workbuddy-switch 已经在做
  - 保留：成长中心任务、盲盒/抽奖/连签兑换/补签卡/礼包补偿/徽章、开学季、小程序任务、领奖

设计要点（不是随便写的，每条都有原因）：
  1. 上游脚本文件原样保留在 vendor/，运行期用 monkey-patch 掏掉不要的任务函数。
     这样上游更新时直接替换 .py 文件即可，我们的改动永远不和上游冲突。
  2. 完全不刷新 token：只读 workbuddy-switch 的账号库拿 AT，refresh_token 一律留空。
     因为 refresh token 是单链轮换的，两边同时刷会互相踢掉，会让 wb-switch 的保活失效。
  3. dry-run 在 requests.Session.request 层统一拦截非 GET 请求 —— 一处拦截覆盖全部调用点，
     包括脚本内部自己 new 的 session，不用逐个函数去挂钩子。
"""

import argparse
import base64
import importlib.util
import json
import os
import re
import sys
import time
import uuid
import urllib3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
CONFIG_PATH = ROOT / "config" / "tasks.json"
LOG_DIR = ROOT / "logs"
# 运行标记：watch.py 靠它避开「全量运行 + 切换补跑」同时打到同一个账号
RUN_MARKER = LOG_DIR / "engine.running"
# 夜猫子「本夜已计过」记录：{uid: "YYYY-MM-DD"}，防止同一夜被多轮运行重复计数
BLACK_CAT_STATE = ROOT / "config" / "black_cat_nights.json"

UPSTREAM_MODULE = "wb_daily_upstream"
TOKEN_FILE_NAME = "WORKBUDDY_ACCESS_TOKEN.txt"
REFRESH_STORE_NAME = "wb_refresh_tokens.json"

# 真实桌面端的认证文件 —— 上游「换血」流程改的就是它。这里只读，用来判断
# 此刻桌面端登录的是哪个账号（换血/切换后这个文件里的 account.uid 就是当前账号）。
DESKTOP_INFO_DIR = (Path(os.path.expanduser("~")) / "AppData" / "Local"
                    / "CodeBuddyExtension" / "Data" / "Public" / "auth")
# 2026-09 桌面端升级为 -ai 变体；判定顺序与上游一致（先 -ai，再旧版）
DESKTOP_INFO_NAMES = ["workbuddy-desktop-ai.info", "workbuddy-desktop.info"]

# 浏览器 UA —— 资料库/space 这类 web 任务上报时要用，别用 WorkBuddy 客户端那串
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

urllib3.disable_warnings()

# dry-run 期间被拦下来的写请求，用于运行后汇总
BLOCKED_WRITES = []


# ---------------------------------------------------------------- 基础工具

class _FakeResp:
    """dry-run 用的假响应：形状对齐 requests.Response 的常用接口。"""

    def __init__(self, url="", method="POST"):
        self.status_code = 200
        self.url = url
        self.text = '{"code":0,"data":{}}'
        self.headers = {"Content-Type": "application/json"}
        self.reason = "DRY-RUN"
        self.method = method

    def json(self):
        return {"code": 0, "data": {}}

    def raise_for_status(self):
        return None

    def __bool__(self):
        return True


def log_line(msg):
    ts = time.strftime("%H:%M:%S")
    print("[%s] %s" % (ts, msg))


def as_emit(log):
    """把任意「日志出口」归一成可调用对象。

    🔴 为什么必须有（2026-09-20 审计发现）：上游 `claim()` 内部直接 `log(...)`，
    不允许 log 是 None；我们的补丁函数签名多样（`log=` 关键字、位置参数、None）。
    服务层调用时若不慎传 None 就会 `TypeError: 'NoneType' object is not callable`，
    而且发生在「已经点亮了任务、正要领奖」的关键路径上 —— 奖励会丢。
    统一走这里，任何输入都返回一个能安全调用的函数。
    """
    if callable(log):
        return log

    def _fallback(msg):
        ts = time.strftime("%H:%M:%S")
        print("[%s] %s" % (ts, msg))

    return _fallback


# ── 任务状态中文化（用户 2026-09-20 要求：日志一律中文，不要英文状态码）──
#
# 服务端返回的 `accept_status` 是英文枚举，上游脚本把它原样打进日志：
#     `召唤3次专家团: claimed 3/3`、`腾讯轻量云专家: accepted 0/1`
# 用户看不懂。这里在**输出层**统一译成中文。
#
# 🔴 为什么放在输出层而不是直接改上游：`vendor/workbuddy_daily.py` 必须保持
# 「与上游仓库逐字节一致」，否则 `update.py` 每次同步都会整文件替换、护栏也会误判。
# 放在 StreamCapture（所有 print 的唯一出口）上，控制台 / 日志文件 / SSE 一次全覆盖。
#
# 只翻译「整词」，且按值长度从长到短替换 —— 否则 `not_accepted` 会先被 `accepted` 咬掉一半。
STATUS_CN = {
    "not_accepted": "未接受",
    "already_claimed": "已领过",
    "in_progress": "进行中",
    "completed": "已完成",
    "accepted": "已接受",
    "claimed": "已领取",
    "skipped": "已跳过",
    "pending": "待处理",
    "failed": "失败",
    "unknown": "未知",
    "blocked": "已拦截",
    "error": "错误",
    "success": "成功",
}
_STATUS_RE = re.compile(
    r"\b(" + "|".join(sorted(STATUS_CN, key=len, reverse=True)) + r")\b")
# 必须词边界匹配：`desktop_chat_1_time`、`wb_wechat_oa_subscribe_task` 这类
# **任务代号**是接口主键，绝不能动；只有**独立成词**的状态枚举才翻译。

# 上游有两处写的是 `log("... 已 %s" % st)`（st 是英文状态），译完会变成
# 「已 已领取」「已 已完成」这种叠字。这里把这个冗余前缀收掉，
# 只吸收「一行里恰好只有这一处状态词」的情况，正文里的「已」不动。
_REDUNDANT_PREFIX_RE = re.compile(r"已\s+(已领取|已完成|已接受|已跳过|已领过)\b")


def zh_status(line):
    """把日志行里独立的英文状态枚举译成中文（任务代号等英文原样保留）。"""
    if not isinstance(line, str):
        return line
    out = _STATUS_RE.sub(lambda m: STATUS_CN[m.group(1)], line)
    return _REDUNDANT_PREFIX_RE.sub(r"\1", out)


class StreamCapture:
    """把 stdout 同时写进「原来的流」和「一个内存缓冲 + 回调」。

    服务化用：运行期间把引擎的输出实时喂给订阅者（SSE 实时日志），
    同时照旧落控制台/日志文件，行为与 CLI 完全一致。

    上游大量用 print，只能从 stdout 层截流 —— 这是唯一能覆盖全部输出的位置。
    由服务层决定要不要订阅；**必须用 `with` 或 try/finally 保证还原**。
    """

    def __init__(self, stream, on_line=None, sink=None):
        self._stream = stream
        self._on_line = on_line      # 每凑满一行调一次（可 None）
        self._sink = sink            # 额外写入对象（如打开的日志文件，可 None）
        self._buf = ""

    def write(self, data):
        if not isinstance(data, str):
            data = str(data)
        # 状态中文化放在「写出去之前」，控制台 / 日志文件 / SSE 就都是中文了。
        # ⚠️ 不要在这里碰 `_buf` 的分行逻辑：替换只按整词做，不会动换行符。
        data = zh_status(data)
        try:
            self._stream.write(data)
        except Exception:
            pass
        if self._sink is not None:
            try:
                self._sink.write(data)
                self._sink.flush()
            except Exception:
                pass
        if self._on_line is not None:
            self._buf += data
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                try:
                    self._on_line(line)
                except Exception:
                    pass
        return len(data)

    def flush(self):
        for o in (self._stream, self._sink):
            if o is None:
                continue
            try:
                o.flush()
            except Exception:
                pass

    def reconfigure(self, *a, **k):
        return getattr(self._stream, "reconfigure", lambda *a, **k: None)(*a, **k)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def normalize_args(**overrides):
    """造一个「补齐了所有字段」的 args 命名空间，供服务层调用。

    🔴 为什么需要（2026-09-20 审计）：`run_accounts` 等函数到处读 `args.X`，
    而 `parse_args()` 依赖命令行。服务层直接构造 Namespace 很容易漏字段 →
    运行时 AttributeError（pyflakes 查不出来）。这里以 parse_args 的定义为准，
    用默认值补齐，再叠加调用方指定项。
    """
    import argparse

    ns = argparse.Namespace()
    for action in _argparse_actions():
        setattr(ns, action.dest, action.default)
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


_argparse_actions_cache = None


def _argparse_actions():
    """缓存 parse_args() 里注册的所有 action（拿默认值用）。"""
    global _argparse_actions_cache
    if _argparse_actions_cache is None:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                ap = _build_parser()
            except Exception:
                ap = None
        _argparse_actions_cache = list(ap._actions) if ap is not None else []
    return _argparse_actions_cache


def mark_running():
    """写一个带自己 PID 的运行标记，供 watch.py 判断「是否已有 engine 在跑」。

    崩溃残留没关系 —— 守望者会核对 PID 是否还活着，死了就当残留清掉。
    """
    try:
        RUN_MARKER.parent.mkdir(parents=True, exist_ok=True)
        RUN_MARKER.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass


def clear_running_mark():
    """只清掉属于自己的那个标记 —— 并发跑时别把别人的抹了。"""
    try:
        if RUN_MARKER.exists() and RUN_MARKER.read_text(encoding="utf-8").strip() == str(os.getpid()):
            RUN_MARKER.unlink()
    except Exception:
        pass


def load_config():
    if not CONFIG_PATH.exists():
        raise SystemExit("缺少配置文件: %s" % CONFIG_PATH)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def jwt_payload(token):
    """解 JWT payload，失败返回 {}。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


# ---------------------------------------------------------------- 账号

def read_switch_accounts(cfg):
    """只读 workbuddy-switch 的账号库，排除国际版。

    过滤按域名做，不按 variant —— 实测 codebuddy.cn 的账号 variant 字段为空，
    用 variant 过滤会把这些国内账号误伤掉。本机实际存在的域名是
    workbuddy.cn（桌面端）与 codebuddy.cn（CLI），国际版 workbuddy.ai 一个都没有。

    返回 [{'note','nick','at','rt','exp','variant','domain'}]，顺序与 accounts.json 一致，
    因此 --only N 的序号跟 wb-switch 页面上看到的顺序对得上。
    """
    raw_path = cfg.get("accounts_source", "~/.wb-switch/accounts.json")
    path = Path(os.path.expanduser(raw_path))
    if not path.exists():
        raise SystemExit("读不到 workbuddy-switch 账号库: %s" % path)

    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise SystemExit("账号库格式异常（期望 list）: %s" % path)

    want_variant = (cfg.get("only_variant") or "").strip()
    exclude_domains = [d.lower() for d in (cfg.get("exclude_domains") or [])]

    accounts, skipped = [], []
    for item in data:
        if not isinstance(item, dict):
            continue
        variant = (item.get("variant") or "").strip()
        domain = (item.get("domain") or "").strip()
        nick = item.get("nickname") or "?"

        if any(bad in domain.lower() for bad in exclude_domains):
            skipped.append("%s(%s 国际版)" % (nick, domain))
            continue
        if want_variant and variant != want_variant:
            skipped.append("%s(variant=%s)" % (nick, variant or "空"))
            continue

        at = (item.get("access_token") or "").strip()
        if not at:
            skipped.append("%s(无AT)" % nick)
            continue
        pay = jwt_payload(at)
        phone = pay.get("preferred_username") or ""
        accounts.append({
            "note": phone or nick,
            "nick": nick,
            "at": at,
            "rt": "",  # 刻意留空：不参与 refresh token 轮换
            "exp": pay.get("exp") or item.get("expiresAt", 0) // 1000,
            "variant": variant or "-",
            "domain": domain,
            # 用于和桌面端认证文件里的 account.uid 对齐（判断「当前桌面账号」）
            "uid": (item.get("uid") or pay.get("sub") or "").strip(),
        })
    return accounts, skipped


def write_token_file(accounts):
    """写上游回退用的 AT 文件（@ 分隔），并确保 refresh store 不存在。

    refresh store 不存在是关键：
      - auto_refresh() 开头的 `if not os.path.exists(REFRESH_STORE): return` 会直接返回，
        于是不会有任何刷新请求、也不可能动到 RT。
      - load_accounts() 随之走 TOKEN_FILE 回退分支，读到我们准备的 AT。
    """
    store = VENDOR / REFRESH_STORE_NAME
    if store.exists():
        store.unlink()

    token_file = VENDOR / TOKEN_FILE_NAME
    token_file.write_text("@".join(a["at"] for a in accounts), encoding="utf-8")
    return token_file


# ---------------------------------------------------------------- 当前桌面账号

def read_desktop_uid():
    """读真实桌面端当前登录账号的 uid。

    数据源是桌面端自己的认证文件 —— 上游「换血」流程改的、workbuddy-switch 切换账号时改的，
    都是同一个文件。这里**只读不写**。
    返回 (uid, 说明)；读不到时 uid 为 None，说明里带原因（供日志用）。
    """
    for name in DESKTOP_INFO_NAMES:
        path = DESKTOP_INFO_DIR / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return None, "%s 解析失败（%s）" % (name, type(exc).__name__)
        uid = ((data.get("account") or {}).get("uid") or "").strip()
        if not uid:
            at = ((data.get("auth") or {}).get("accessToken") or "").strip()
            uid = (jwt_payload(at).get("sub") or "").strip()
        if uid:
            return uid, name
        return None, "%s 里既无 account.uid 也无 auth.accessToken" % name
    return None, "找不到桌面端认证文件（%s）" % DESKTOP_INFO_DIR


def find_desktop_account(accounts):
    """找出「此刻真实桌面端登录的那个账号」在账号库里的序号（从 1 开始）。

    返回 (序号 或 None, 说明)。**找不到时序号为 None，调用方必须按「没检测到」处理，
    绝不能随便挑一个账号冒充桌面账号** —— 那正好会写坏真实桌面端的会话。
    """
    uid, src = read_desktop_uid()
    if not uid:
        return None, src
    for i, acc in enumerate(accounts, 1):
        if acc.get("uid") and acc["uid"] == uid:
            return i, "%s（来源 %s）" % (acc["nick"], src)
    return None, "桌面端登录的 uid=%s… 不在本次账号库里" % uid[:8]


# ---------------------------------------------------------------- 上游加载

def load_upstream():
    """加载 vendor/workbuddy_daily.py。

    上游在模块级就会执行 _bootstrap_store() 和 load_accounts()，所以调用前
    sys.argv 和环境变量必须已经就位。
    """
    script = VENDOR / "workbuddy_daily.py"
    if not script.exists():
        raise SystemExit("缺少上游脚本: %s（先跑 update.py 拉取）" % script)

    spec = importlib.util.spec_from_file_location(UPSTREAM_MODULE, script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[UPSTREAM_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod


def apply_exclusions(mod, cfg):
    """把不要的任务函数替换成空操作。

    上游 run_account() 是直接按全局名调用 t_xxx(...) 的，所以替换模块全局名字即可生效。
    原文件一个字节都不用动 → 上游更新可以无脑替换。
    """
    excluded = cfg.get("exclude_functions") or {}
    applied = []
    for name, reason in excluded.items():
        if not hasattr(mod, name):
            log_line("⚠️  排除项 %s 在上游脚本里不存在（上游可能改过名），已跳过" % name)
            continue

        def _skipper(s=None, uid=None, nick=None, log=None, *a, _n=name, _r=reason, **k):
            msg = "  ⏭️  跳过 %s（%s）" % (_n, _r)
            if callable(log):
                try:
                    log(msg)
                    return
                except Exception:
                    pass
            print(msg)

        setattr(mod, name, _skipper)
        applied.append(name)
    return applied


def install_dry_run():
    """在 requests 层拦截所有非 GET 请求。

    上游的写操作分散在 new_api() 建的 session、_school_session() 建的 session、
    以及零散的 requests.post 里。逐个挂钩子必然漏，所以统一拦 Session.request。
    requests 的便利方法（get/post/put/...）最终都会走到这里。

    🔴 必须是**可逆**的（2026-09-20 审计发现的原 bug）：
        原实现直接改 `requests.Session.request` 且从不恢复 —— 作为常驻服务反复运行时
        ① dry-run 跑过一次后，之后所有**真实**运行都会被静默拦成假响应（最危险）；
        ② 每跑一次 dry-run 就在原函数外面再套一层，越套越深。
    现在返回一个 `restore()`，调用方（main 或服务层）必须在 finally 里调它。
    同时清空 BLOCKED_WRITES，避免多次运行之间计数互相污染。
    """
    import requests

    # 已经是打过补丁的状态 → 先还原，避免套娃
    prev = getattr(requests.Session, "_wb_hub_dryrun_original", None)
    if prev is not None:
        requests.Session.request = prev
        del requests.Session._wb_hub_dryrun_original

    original = requests.Session.request
    requests.Session._wb_hub_dryrun_original = original
    BLOCKED_WRITES.clear()

    def guarded(self, method, url, *a, **k):
        verb = (method or "GET").upper()
        if verb in ("GET", "HEAD", "OPTIONS"):
            return original(self, method, url, *a, **k)
        BLOCKED_WRITES.append((verb, str(url)))
        return _FakeResp(url=str(url), method=verb)

    requests.Session.request = guarded

    def restore():
        """把 Session.request 换回原实现（幂等，可重复调用）。"""
        try:
            import requests as _rq
            if getattr(_rq.Session, "_wb_hub_dryrun_original", None) is not None:
                _rq.Session.request = _rq.Session._wb_hub_dryrun_original
                del _rq.Session._wb_hub_dryrun_original
        except Exception:
            pass

    return restore


def relabel_accounts(mod, accounts):
    """把上游 ACCOUNTS 的 note 换成可读昵称，便于看日志。

    上游给的回退路径 note 是「账号N」，没法一眼看出是谁。
    注意：这里对应的是全部账号 —— 挑账号由引擎自己的循环决定，不给上游传 --only。
    """
    if len(mod.ACCOUNTS) != len(accounts):
        return
    for entry, acc in zip(mod.ACCOUNTS, accounts):
        # 只放昵称：上游汇总报告里 note 会按 16 字符截断，带上手机号会被切掉尾巴
        entry["note"] = acc["nick"]


# ---------------------------------------------------------------- 入口

def _build_parser():
    """构建命令行解析器（拆出来是为了让 normalize_args 能读到 action 默认值）。"""
    ap = argparse.ArgumentParser(description="WorkBuddy 任务引擎（只跑 workbuddy-switch 没做的任务）")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="拦截所有写请求，只读验证（默认）")
    mode.add_argument("--live", action="store_true", help="真实提交任务")
    ap.add_argument("--only", type=int, default=None, help="只跑第 N 个账号（从 1 开始，顺序同 wb-switch）")
    ap.add_argument("--no-desktop", action="store_true", help="本次完全跳过桌面类任务")
    ap.add_argument("--desktop-full", action="store_true",
                    help="改用上游原始桌面换血流程（会 taskkill /F /IM WorkBuddy.exe，危险）")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--desktop-current", action="store_true",
                       help="桌面类任务只做「此刻真实桌面端登录的那个账号」，其余账号跳过")
    scope.add_argument("--desktop-all", action="store_true",
                       help="桌面类任务对所有账号都做（用指纹上报建立会话）")
    ap.add_argument("--no-school", action="store_true", help="跳过开学季活动")
    ap.add_argument("--gap", type=float, default=None, help="写动作间隔秒数，最低 1.0")
    ap.add_argument("--list", action="store_true", help="只列账号与将要执行的任务，不联网")
    ap.add_argument("--no-update", action="store_true", help="跳过上游静默同步")
    return ap


def parse_args():
    return _build_parser().parse_args()


def describe_plan(cfg, accounts, only=None, desktop_mode=None,
                  desktop_scope=None, desktop_idx=None, desktop_note=None):
    print("=" * 66)
    print("📋 本次将执行的任务（已剔除 workbuddy-switch 已覆盖的部分）")
    print("=" * 66)
    print("  ⛔ 排除：")
    for name, reason in (cfg.get("exclude_functions") or {}).items():
        print("       %-12s %s" % (name, reason))
    print("  ✅ 执行：")
    print("       成长中心任务（设计画布/探索灵感/专家团/主题/资料库/模板/GLM-5.2/夜猫子/…）")
    print("       互动玩法（抽奖 · 盲盒 · 连签兑换 · 补签卡 · 礼包补偿 · 徽章）")
    if cfg.get("school_activity", True):
        print("       开学季活动（含幸运大转盘）")
    else:
        print("       开学季活动：已在配置中关闭")
    print("       小程序任务 · 自动领奖 · 未覆盖任务检测")
    mode = desktop_mode or (cfg.get("desktop_mode") or "fingerprint")
    if mode == "off":
        print("       🖥️ 桌面类任务：完全跳过")
    elif mode == "fingerprint":
        print("       🖥️ 桌面类任务：指纹上报模式（纯 API，不碰桌面端进程、不杀 WorkBuddy）")
        print("          这是默认策略：新账号必须先有桌面会话，否则后续任务接受会被服务端回滚。")
    else:
        print("       🚨 桌面类任务：full 模式 —— 会 taskkill /F /IM WorkBuddy.exe 并改写桌面端认证文件，")
        print("          且可能与 workbuddy-switch 的账号切换相互写坏状态。除非你清楚在做什么，否则别用。")
    if mode != "off":
        if (desktop_scope or cfg.get("desktop_scope") or "all") == "current":
            print("       🎯 适用账号：只做「此刻真实桌面端登录的那个账号」，其余账号跳过桌面类任务")
            if desktop_idx:
                print("          已识别 → 账号%d %s" % (desktop_idx, desktop_note))
            else:
                print("          ⚠️ 没识别出当前桌面账号 → 本次谁都不做桌面类任务")
                print("             （%s）" % (desktop_note or "原因未知"))
        else:
            print("       🎯 适用账号：账号库全部（用指纹上报给每个账号建立桌面会话）")
    print()
    if only:
        print("👥 目标账号：只跑第 %d 个（账号库共 %d 个）" % (only, len(accounts)))
    else:
        print("👥 目标账号：账号库全部 %d 个（每次运行都重读账号库，新账号自动纳入）" % len(accounts))
    for i, acc in enumerate(accounts):
        left = ""
        if acc["exp"]:
            days = (acc["exp"] - time.time()) / 86400
            left = "  AT 剩余 %.1f 天" % days
            if days <= 0:
                left = "  ⚠️ AT 已过期"
        picked = "★" if only == i + 1 else " "
        desk = "  🖥️ 当前桌面端" if desktop_idx == i + 1 else ""
        print("  %s %d) %-22s %s%s%s" % (picked, i + 1, acc["nick"], acc["domain"], left, desk))
    print()


def resolve_desktop_mode(cfg, args):
    """决定桌面任务策略：off / fingerprint / full。"""
    mode = (cfg.get("desktop_mode") or "fingerprint").strip().lower()
    if mode not in ("off", "fingerprint", "full"):
        mode = "fingerprint"
    if args.no_desktop:
        mode = "off"
    if args.desktop_full:
        mode = "full"
    return mode


def resolve_desktop_scope(cfg, args):
    """决定桌面任务的**适用账号范围**：all / current。

    - all     ：每个账号都做（用指纹上报替它建立桌面会话）→ 覆盖最大
    - current ：只做此刻真实桌面端登录的那个账号 → 不替别的账号造会话，最"真实"
    """
    scope = (cfg.get("desktop_scope") or "all").strip().lower()
    if scope not in ("all", "current"):
        scope = "all"
    if getattr(args, "desktop_current", False):
        scope = "current"
    if getattr(args, "desktop_all", False):
        scope = "all"
    return scope


def claim_if_completed(mod, s, codes, emit):
    """点亮任务之后**立刻领奖**。

    🔴 为什么必须有这个函数（2026-09-20 用户报障「做完了但没领取奖励」）：
        上游 `run_account` 的领奖循环写在 **所有任务跑完之后**：

            … → 各任务 → 🎁 ── 领奖 ── → 终态统计

        而我们的几个补丁（`_fixed_library` / `credit_skill_via_real_chat` /
        指纹上报）是在**云端阶段中途**把任务点亮的。更麻烦的是服务端对某些事件
        是**延迟入账**的 —— 埋点发出去的那一刻查 `accept_status` 还是 `accepted`，
        等领奖循环跑到时仍未翻成 `completed`，于是那一轮就**白白错过**。

    实测（账号5，本轮）：
        11:24 那轮日志 `发现应用/企鹅教师助手: accepted / accepted`、`体验资料库: accepted`
        领奖循环 → 「无待领奖励」
        但稍后再查：这 3 个 + skill_1 全部变成 `completed` 却**没人领**
        → 完成 11/19，4 个奖励躺在服务端没人要。

    所以：点亮之后补一次 claim；`claim()` 本身幂等（已领过会回 already_claimed），
    重复调用没有副作用。

    codes: 任务 code 字符串或列表。
    """
    emit = as_emit(emit)
    if isinstance(codes, str):
        codes = [codes]
    got = []
    for code in codes:
        try:
            st, cur, tgt = mod.prog(s, code)
        except Exception as exc:
            emit("   🎁 领奖前查进度失败[%s]: %s" % (code, type(exc).__name__))
            continue
        if st == "completed":
            try:
                mod.claim(s, code, emit)
                got.append(code)
            except Exception as exc:
                emit("   🎁 领奖[%s] 异常: %s: %s" % (code, type(exc).__name__, str(exc)[:50]))
        elif st == "claimed":
            emit("   🎁[%s]: 已领过" % code)
    return got


def claim_all_completed(mod, s, emit, exclude=()):
    """兜底清扫：把所有 `completed` 但没领的任务一次领完。

    这是「补丁点亮 → 服务端延迟入账 → 领奖循环已经跑过」这条漏网路径的最后一道闸。
    放在整轮流程的最后（领奖后复查之后）执行，把当轮所有能领的都收干净。
    """
    emit = as_emit(emit)
    try:
        r = s.get(mod.BASE + "/v2/activity/growth/tasks", timeout=25, verify=False).json()
        tasks = (r.get("data") or {}).get("tasks") or []
    except Exception as exc:
        emit("   🎁 兜底领奖：查询失败 %s" % type(exc).__name__)
        return []
    skipped = set(exclude)
    pend = [t.get("task_code", "") for t in tasks
            if isinstance(t, dict) and t.get("accept_status") == "completed"
            and t.get("task_code") and t.get("task_code") not in skipped]
    if not pend:
        return []
    emit("   🎁 兜底领奖：发现 %d 个「已完成未领」→ %s" % (len(pend), ", ".join(pend)))
    got = []
    for code in pend:
        try:
            mod.claim(s, code, emit)
            got.append(code)
        except Exception as exc:
            emit("   🎁 兜底领奖[%s] 异常: %s" % (code, type(exc).__name__))
        time.sleep(1)
    return got


def credit_skill_via_real_chat(mod, s, uid, nick, emit):
    """用「真实对话 + growthEvent 搭车」点亮「尝鲜热门技能」(skill_1)。

    🔑 这是本次挖出来的核心机制（2026-09-20 实测，账号5）：
        只打 /v2/report 的 skill_info 埋点 → 服务端**永远不给进度**，卡在 accepted 0/1；
        真实 POST /console/chat/completions，并把 skill_info 塞进
        请求的 _meta["codebuddy.ai"]["growthEvent"] → **直接变 completed 1/1**。

    依据来自客户端 app.asar 里的 `parseGrowthEvents(meta.growthEvent)`
    （注释写明 meta 支持 codebuddy.ai 嵌套形式）；上游能点亮的任务
    （t_team_3 / t_black_cat）走的也正是这条路。
    一句话：**埋点必须搭在真实请求上，光发 /v2/report 不算数。**
    """
    st, cur, tgt = mod.prog(s, "skill_1")
    if st in ("completed", "claimed"):
        return False

    skill_name = getattr(mod, "SKILL_NAME", "algorithmic-trading")
    skill_id = ""
    try:
        r = s.post(mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/skill/list",
                   json={"page": 1, "page_size": 10}, timeout=20, verify=False).json()
        skills = (r.get("data") or {}).get("skills") or []
        skill_id = next((k.get("id") for k in skills if skill_name in str(k.get("name", ""))), "")
    except Exception:
        pass
    if not skill_id:
        # 取不到就用稳定派生值 —— 实测这个也能点亮（服务端并不强校验 skillId）
        skill_id = "skill-" + mod.derive_id(uid, "skill")[:12]

    ge = [{"eventCode": "skill_info", "skillId": skill_id, "skillName": skill_name,
           "reportDelay": 0, "mode": "CLOUD", "source": "builtin", "userId": uid}]
    rid = str(uuid.uuid4())
    meta = {"codebuddy.ai": {
        "growthEvent": json.dumps(ge, ensure_ascii=False),
        "promptRequestId": rid,
        "clientSendTime": int(time.time() * 1000),
        "userId": uid, "mode": "craft", "model": "glm-5.2",
        "skillId": skill_id, "skillName": skill_name}}

    try:
        conv_id, txt = mod.webchat(
            s, "skill", "请通过技能系统正式加载 %s 技能，然后回答：技能已加载" % skill_name, meta)
        reply_len = len(txt or "")
        # 再补一条常规埋点（带上真实 conversationId），双保险
        mod.report(s, uid, nick, [{"eventCode": "skill_info", "skillId": skill_id,
                                   "skillName": skill_name, "conversationId": conv_id,
                                   "requestId": rid, "source": "builtin",
                                   "mode": "CLOUD", "userId": uid}])
        time.sleep(6)
        st2, cur2, tgt2 = mod.prog(s, "skill_1")
    except Exception as exc:
        # 这一步是「锦上添花」，任何失败都不该影响该账号的其余任务
        emit("   尝鲜热门技能(真实对话)失败（已忽略）：%s: %s"
             % (type(exc).__name__, str(exc)[:70]))
        return False
    emit("   尝鲜热门技能(真实对话): %s %s/%s（回复%d字）" % (st2, cur2, tgt2, reply_len))
    # 点亮之后必须当场领奖 —— 否则要等下一轮的领奖循环，而那时可能又错过（见 claim_if_completed 注释）
    claim_if_completed(mod, s, "skill_1", emit)
    return st2 in ("completed", "claimed")


def apply_desktop_safety(mod, mode, cfg=None):
    """把桌面相关的危险实现换成安全实现（只改内存里的函数引用，不动上游文件）。

    封的是两处会执行 `taskkill /F /IM WorkBuddy.exe` 的代码：
      1. t_desktop_tasks 的 Windows 分支：swap_info 改认证文件 → restart_desktop 杀进程+CDP →
         跑完再 taskkill 一遍。它会和 workbuddy-switch 的账号切换对撞
         （切换流程是「关 WorkBuddy → 写目标账号 → 重启」，被并发杀掉/重启会把账号状态写错）。
      2. t_workstation：在 run_account 里是**无条件调用**的，不受 --no-desktop 控制，
         只要腾讯上线「工作台搭建师」任务就会直接杀 WorkBuddy。

    换成 fingerprint 后走的是上游自己的合法降级路径 `_desktop_fingerprint_fallback`
    （注释写明「task_runner 同款，无需真实桌面端」）—— 纯 API 事件上报，不碰任何本地进程。
    保留它的原因是：新账号必须先有桌面会话，否则后续任务接受会被服务端回滚、遥测不计数。
    """
    if mode != "fingerprint":
        return []

    replaced = []

    def _safe_desktop_tasks(s=None, uid=None, nick=None, tok=None, log=None,
                            need_rich=False, need_skill=False, *a, **k):
        """替掉上游的 Windows「换血」实现，改走指纹上报。

        上游原始实现会 swap_info 改写桌面端认证文件 → restart_desktop 杀进程 →
        跑完再 taskkill 一遍，和 workbuddy-switch 的账号切换是同一批文件、同一个进程，
        并发起来会把账号状态写坏。这里换成纯 API 上报，效果对齐上游的合法降级路径。
        """
        emit = log if callable(log) else print
        emit("   🖥️ 桌面任务：指纹上报（RichMeow=%s skill_1=%s，不碰桌面端进程）"
             % (need_rich, need_skill))
        try:
            mod._desktop_fingerprint_fallback(s, uid, nick, log, need_rich, need_skill)
        except Exception as exc:
            # 指纹上报失败不该拖垮这个账号 —— 后面还有十几个云端任务要跑
            emit("   🖥️ 桌面指纹上报异常（已忽略，继续云端任务）：%s: %s"
                 % (type(exc).__name__, str(exc)[:80]))

        # 🔴 回读真实状态（2026-09-20）：上游那句 `桌面对话(指纹): ✅ 6连事件已上报`
        #    是**无条件打印**的 —— 只证明请求发出去了，不证明任务计数。
        #    实测（logs/run-20260920-191046.log 等）RichMeow_Chat 长期停在 `已接受 0/1`，
        #    日志上的 ✅ 与回读结果完全矛盾。这里补一行真实回读，免得再被假 ✅ 骗。
        for _code, _need in (("RichMeow_Chat", need_rich), ("skill_1", need_skill)):
            if not _need:
                continue
            try:
                _st, _cur, _tgt = mod.prog(s, _code)
                emit("   🖥️ %s 回读: %s %s/%s" % (_code, _st, _cur, _tgt))
            except Exception as exc:  # noqa: BLE001
                emit("   🖥️ %s 回读失败: %s" % (_code, type(exc).__name__))

        # 指纹上报点亮不了 skill_1（服务端只认「真实对话」），所以再补一手真实对话。
        # 这一步实测能把 accepted 0/1 直接推到 completed 1/1。
        if need_skill and (cfg or {}).get("skill_real_chat", True):
            try:
                credit_skill_via_real_chat(mod, s, uid, nick, emit)
            except Exception as exc:
                emit("   🖥️ 技能真实对话失败（已忽略，继续云端任务）：%s: %s"
                     % (type(exc).__name__, str(exc)[:80]))

    if hasattr(mod, "t_desktop_tasks"):
        mod.t_desktop_tasks = _safe_desktop_tasks
        replaced.append("t_desktop_tasks→指纹上报")

    def _safe_workstation(s=None, uid=None, nick=None, log=None, tok=None, *a, **k):
        msg = "   🧰 工作台搭建师：已跳过桌面换血（上游实现会 taskkill WorkBuddy），需人工处理"
        if callable(log):
            log(msg)
        else:
            print(msg)

    if hasattr(mod, "t_workstation"):
        mod.t_workstation = _safe_workstation
        replaced.append("t_workstation→跳过换血")

    return replaced


def apply_report_shape_fix(mod, cfg):
    """把 `report_desktop_events()` 的报文形状改成服务端真正要的样子。

    这里其实踩了**两个**坑，第二个是本补丁自己引入的（2026-09-20 深夜修正）：

    坑①「信封」：上游有**两个**打到 `/v2/report` 的函数 ——
        · `report()`            发的是**裸数组**，且每条事件都带 timestamp / reportDelay → 一直能用
        · `report_desktop_events()` 发的是 `{"common": {...}, "events": [...]}` **信封**，
          且 desktop_*_sequence 造出来的事件**没有 timestamp/reportDelay**
      → 服务端对信封一律 **HTTP 200 但不计分**，表现就是「埋点明明发了、进度死活不动」。
      实测（账号5，裸数组 + 补时间字段）：
        Buddy_App / Buddy_App_QQ  accepted 0/1 → completed 1/1
        只改形状不补时间字段 → HTTP 400 `event missing both reportDelay and timestamp fields`

    坑②「身份信封被弄丢」（本函数第一版自己造的 bug）：
        上游原版是 `fp = desktop_fingerprint(uid, nick)` **逐条 `m.update(fp)`**，
        再由 `{"common":..., "events":[arr]}` 包起来。
        第一版补丁只把外层信封拆成裸数组，**却没把 `fp` 注入回来** →
        桌面类事件是以「**没有任何客户端身份字段**」的形状发出去的
        （ideType/ideName/extName/product/machineId/os… 全缺，只剩 eventCode + 业务字段）。
        🔴 这与 `expert_use` 当年的失败原因同一类：**报文缺客户端身份信封**。

    坑②的实证（`probe_richmeow.py arms 3`，同账号、同一分钟内做 A/B）：
        arm0 = 现状基线（无身份字段）          → 5 次回读全是 `accepted 0/1`
        arm1 = 仅加回 desktop_fingerprint 信封 → **首读即 `completed 1/1`**，当场领到 +100积分+5能量
      ⚠️ 未做进一步隔离：arm1 一次加了整组字段，**具体哪个字段是必要条件尚未收窄**
        （`machineId` 用派生假值即可，真 qimei36 / 真 machineId 均**不需要**，arm2/arm3 未再跑）。

    这个补丁同时覆盖 RichMeow_Chat（走 _desktop_fingerprint_fallback → report_desktop_events）
    和 t_buddy_apps，一处改对，两处受益。
    """
    if not cfg.get("fix_report_shape", True):
        return []
    if not hasattr(mod, "report_desktop_events"):
        return []

    _fp_fn = getattr(mod, "desktop_fingerprint", None)

    def fixed(s, uid, nick, events):
        now = int(time.time() * 1000)
        try:
            fp = _fp_fn(uid, nick) if _fp_fn else {}
        except Exception:
            fp = {}
        out = []
        for i, e in enumerate(events):
            m = dict(e)
            # 🔴 必须把客户端身份信封注入回来（坑②）。fp 里含 timestamp/presentAt，
            #    所以顺序是「先 update(fp) 再显式写每条的 timestamp」，保留逐条时间递进。
            m.update(fp)
            m["timestamp"] = e.get("timestamp", now + i * 120)
            m["reportDelay"] = e.get("reportDelay", 0)
            m.setdefault("userId", uid)
            m.setdefault("userNickname", nick)
            out.append(m)
        return mod.api_retry(s, "POST", mod.BASE + "/v2/report", body=out)

    mod.report_desktop_events = fixed

    # 顺带给 t_buddy_apps 补领奖：上游这个函数只负责发埋点，从不 claim
    # （实测账号5 的「发现应用」「企鹅教师助手」就是这样变成 completed 却没人领的）
    if hasattr(mod, "t_buddy_apps"):
        _orig_buddy = mod.t_buddy_apps

        def _buddy_with_claim(s=None, uid=None, nick=None, log=None, *a, **k):
            emit = log if callable(log) else print
            _orig_buddy(s, uid, nick, log)
            try:
                claim_if_completed(mod, s, ["Buddy_App", "Buddy_App_QQ"], emit)
            except Exception as exc:
                emit("   🎁 发现应用领奖异常（已忽略）：%s" % type(exc).__name__)

        mod.t_buddy_apps = _buddy_with_claim

    return ["report_desktop_events→裸数组+时间字段", "t_buddy_apps→补领奖"]


def apply_black_cat_fix(mod, cfg):
    """把 `t_black_cat`（夜猫子）换成「有界重试 + 当日计数到手即停」的版本。

    任务语义（服务端原文，`/v2/activity/growth/tasks`）：
        title     = 参与「夜猫子」夜间折扣活动
        task_desc = 夜间23点-次日8点，新建对话并成功使用「GLM-5.2」，每天 1 次，累计 3 天。
        progress  = n/3

    🔴 上游实现（2026-09-20 自检发现）的坑**不在埋点、在控制流**：
        `for attempt in range(8)` 只在 `cur >= tgt(3)` 时才 break。
        而"每天只计 1 次"→ 跑完当晚第 1 次对话后 cur 只 +1，**仍 < 3** →
        循环**不会提前退出**，于是同一个夜里给每个账号**硬发 8 次真实 GLM-5.2 对话**
        （7 个账号 ≈ 56 次/夜）→ 纯空烧额度，且容易被风控盯上。
        ⚠️ 这正是"只改对了一半"的典型：机制没问题，**控制流**有问题。别只看埋点。

    本补丁：每晚最多发 `black_cat_attempts` 次（默认 3），且**一旦回读确认当日计数到账就停**
        （cur 相比本轮开始时变大，或状态变 completed/claimed）→ 正常每晚只发 1 次，
        只有上报真没生效时才重试。

    再加一层「本夜已计过」记录（`config/black_cat_nights.json`，按 uid 记「夜编号」）：
        **只有回读确认到账才落账**；同一夜再被触发（手动跑 / 切换补跑 / 多轮调度）直接跳过。
        这样「每天 1 次」在客户端就成立，不必赌服务端是否真做了每日封顶。
        ⚠️ guard 全程 **fail-open**：状态文件读不出、写不进都一律放行 ——
        保险丝的作用是防多发，绝不能反过来把任务卡死三天。

    ✅ 身份信封**不需要动**（这点已用证据确认，别照着 RichMeow 的经验乱改）：
        本任务与已 **7/7 完成**的 `Model_chat_GLM5.2`（服务端要求同样是
        「新建对话并使用「GLM-5.2」模型成功对话一次」）走的是同一条
        `webchat()`（真实 POST /console/webchat/conversations + completions，model=glm-5.2）
        + `chat_request_events()` + `report()` 的路；那项用 `ideType=web-Agents` 就能计数，
        且 `chat_5`(5/5) 同样如此 → 说明这条路的信封是**对的**，只差时间窗与控制流。
    """
    if not cfg.get("fix_black_cat", True):
        return []
    if not hasattr(mod, "t_black_cat"):
        return []

    max_attempts = int(cfg.get("black_cat_attempts", 3) or 3)
    max_attempts = max(1, min(max_attempts, 8))
    guard_on = bool(cfg.get("black_cat_night_guard", True))
    PROMPTS = ["今天天气怎么样？", "1+1等于几？", "讲个笑话"]

    def _night_key():
        """给「夜」编号：23点后算当天，凌晨(<8点)算前一天 → 同一夜 (23:30 / 次日 00:30) 同一个 key。

        全程用北京时间推导，不依赖系统时区（与 `within_night_window()` 同口径）。
        """
        try:
            now = mod.beijing_now()          # aware datetime，UTC+8
            ts = now.timestamp()
            hour = now.hour
        except Exception:
            ts = time.time()
            hour = time.localtime(ts).tm_hour
        if hour < 8:
            ts -= 86400                       # 凌晨 → 归到前一夜
        return time.strftime("%Y-%m-%d", time.gmtime(ts + 8 * 3600))

    def _load_nights():
        """读「本夜已计过」记录；读不出/坏了都当空表（guard 是保险，宁可多发一次也绝不卡任务）。"""
        try:
            with open(BLACK_CAT_STATE, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _mark_night(uid, key):
        """原子写回。写失败**静默忽略**：这只是防重复的保险，绝不能因为它报错而中断任务。"""
        try:
            BLACK_CAT_STATE.parent.mkdir(parents=True, exist_ok=True)
            d = _load_nights()
            d[str(uid)] = key
            tmp = BLACK_CAT_STATE.with_name(BLACK_CAT_STATE.name + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, BLACK_CAT_STATE)
        except Exception:
            pass

    def fixed(s, uid, nick, log):
        emit = log if callable(log) else print
        st, cur, tgt = mod.prog(s, "black_cat")
        if st in ("completed", "claimed"):
            return
        if not mod.within_night_window():
            # 显式用北京时间：GitHub Actions runner 是 UTC，localtime() 会误导排查
            emit("   夜猫子: 仅23:00-08:00计数（CST），当前北京时间%d点，跳过"
                 % mod.beijing_now().hour)
            return
        key = _night_key()
        if guard_on and _load_nights().get(str(uid)) == key:
            emit("   夜猫子: 本夜（%s）已计过 1 次，跳过（每天只计 1 次）" % key)
            return
        tgt = tgt or 3
        for attempt in range(max_attempts):
            st, cur, tgt2 = mod.prog(s, "black_cat")
            tgt = tgt2 or tgt
            if st in ("completed", "claimed") or (cur or 0) >= tgt:
                break
            cur_before = cur or 0
            conv_id, txt = mod.webchat(s, "night", PROMPTS[attempt % len(PROMPTS)])
            if not txt:
                emit("   夜猫子: 第%d次对话 ❌（无回复，将重试）" % (attempt + 1))
                time.sleep(5)
                continue
            evs, _ = mod.chat_request_events(uid, nick, conv_id, "聊天", txt)
            mod.report(s, uid, nick, evs)
            emit("   夜猫子: 第%d次对话 ✅（回复%d字）" % (attempt + 1, len(txt)))
            # 回读确认「当日 1 次」是否到账 —— 到账就收手（每天只计 1 次）
            landed = False
            for _ in range(5):
                time.sleep(4)
                st2, cur2, tgt2 = mod.prog(s, "black_cat")
                if st2 in ("completed", "claimed") or (cur2 or 0) > cur_before:
                    landed = True
                    cur, tgt = cur2, (tgt2 or tgt)
                    break
            if landed:
                if guard_on:
                    _mark_night(uid, key)   # 只在**确认到账**后才记账，绝不凭空拦掉下一夜
                if (cur or 0) < (tgt or 3):
                    emit("   夜猫子: 当日 1 次已到账（%s/%s），本夜不再发（每天只计 1 次）"
                         % (cur, tgt))
                break
            time.sleep(1)
        st, cur, tgt = mod.prog(s, "black_cat")
        emit("   夜猫子: %s %s/%s" % (st, cur, tgt))

    mod.t_black_cat = fixed
    return ["t_black_cat→有界重试+当日到账即停"]


def apply_web_report_fix(mod, cfg):
    """修正「体验资料库」(Library_read) 的上报形状。

    🔑 上游 `report_web_event()` 发的是 `{"common": {...}, "events": [...]}` 信封，
    但资料库前端（`static.workbuddy.cn/web/library/<hash>/js/main~4.js`）**实际发的是裸数组**：

        fetch("/v2/report", {
            method: "POST",
            headers: {"Accept": "application/json, text/plain, */*",
                      "Content-Type": "application/json;charset=UTF-8",
                      "X-Requested-With": "XMLHttpRequest",
                      "X-Request-Trace-Id": <uuid>, "X-User-Id": <uid>},
            body: JSON.stringify([{
                eventCode: "web_element_click", timestamp, reportDelay: 0, pageURL,
                elementId: "library_doc_intro_click", elementName: "Workbuddy资料库介绍",
                os, arch, osVersion, userAgent, machineId, userId, userNickname, enterpriseId}]))
        // 目标文档在前端是按 nodeId 匹配的：nodeId = o0KWYeynteVv06UnAZqIFm

    实测（2026-09-20 账号5）：只改形状，`accepted 0/1` → **`completed 1/1`**。
    也就是说上游不是"伪造不出来"，而是**报文的形状不对**。
    """
    if not cfg.get("fix_library_report", True):
        return []
    if not hasattr(mod, "t_library"):
        return []

    doc_url = getattr(mod, "LIB_DOC_URL",
                      "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm")

    def _fixed_library(s=None, uid=None, nick=None, log=None, *a, **k):
        emit = log if callable(log) else print
        try:
            st, cur, tgt = mod.prog(s, "Library_read")
        except Exception as exc:
            emit("   体验资料库: 查进度失败 %s" % type(exc).__name__)
            return
        if st is None:
            emit("   体验资料库: 不在当前任务列表，跳过")
            return
        if st in ("completed", "claimed"):
            emit("   体验资料库: 已 %s" % st)
            return

        # 完全照前端那一份报文来（裸数组 + 真字段 + 那几个 X- 头）
        ev = {"eventCode": "web_element_click",
              "timestamp": int(time.time() * 1000),
              "reportDelay": 0,
              "pageURL": doc_url,
              "elementId": "library_doc_intro_click",
              "elementName": "Workbuddy资料库介绍",
              "os": "Win32", "arch": "", "osVersion": "10.0.26220",
              "userAgent": WEB_UA,
              "machineId": mod.derive_id(uid, "machine"),
              "userId": uid, "userNickname": nick or "", "enterpriseId": ""}
        try:
            r = s.post(mod.BASE + "/v2/report", json=[ev], timeout=20,
                       headers={"Accept": "application/json, text/plain, */*",
                                "Content-Type": "application/json;charset=UTF-8",
                                "X-Requested-With": "XMLHttpRequest",
                                "X-Request-Trace-Id": str(uuid.uuid4()),
                                "X-User-Id": uid})
            emit("   体验资料库: 已按前端真实形状上报（HTTP %s）" % r.status_code)
        except Exception as exc:
            # 只是锦上添花，失败不该影响这个账号其余任务
            emit("   体验资料库: 上报异常（已忽略）%s: %s"
                 % (type(exc).__name__, str(exc)[:60]))
            return
        time.sleep(6)
        st2, cur2, tgt2 = mod.prog(s, "Library_read")
        emit("   体验资料库: %s %s/%s" % (st2, cur2, tgt2))
        # 点亮即领奖（服务端可能延迟入账，这里先试一次；没到 completed 也无妨，
        # 末尾的 claim_all_completed 兜底会再扫一遍）
        claim_if_completed(mod, s, "Library_read", emit)

    mod.t_library = _fixed_library
    return ["t_library→裸数组报文（贴合前端真实形状）"]


def _mp_bare_report(mod, s, uid, nick, events):
    """用小程序口径把 events 以**裸数组**发到 copilot.tencent.com/v2/report。

    返回 HTTP 状态码（失败返回 0）。这里跟上游 `_mini_report` 唯一的区别就是
    **body 是裸数组而不是信封** —— 别的头/域名/事件字段全部照抄。

    ⚠️ 不能改用 `s`（普通会话）发：mp 口径必须带 `X-Client-Platform: miniprogram`
    与微信小程序 UA，否则服务端不认这个来源。
    """
    import requests
    mp_s = requests.Session()
    mp_s.trust_env = False
    # 用 getattr 兜一下：上游原版直接写 `s.headers.get(...)`，s 为 None 时
    # 会抛一个不知所云的 AttributeError；这里宁可降级成空 auth 也别炸。
    hdrs = getattr(s, "headers", None) or {}
    auth = hdrs.get("Authorization", "")
    mp_s.headers.update({
        "Authorization": auth if auth.startswith("Bearer ") else "Bearer " + auth,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Client-Platform": "miniprogram",
        "User-Agent": getattr(mod, "MP_UA", ""),
    })
    try:
        r = mp_s.post("https://copilot.tencent.com/v2/report", json=list(events),
                      timeout=20, verify=False)
        return r.status_code
    except Exception:  # noqa: BLE001
        return 0


def _mini_chat_event(uid, conv_id, activity_id=""):
    """拼一条小程序 `chat_request_send` 事件（字段照抄上游，一字不改）。"""
    ev = {"eventCode": "chat_request_send", "timestamp": int(time.time() * 1000),
          "reportDelay": 0, "source": "mini_program", "ideName": "wx_app_cloud",
          "ideType": "WorkBuddy_MP", "extName": "workbuddy-mp", "extVersion": "2.4.0",
          "mode": "chat", "conversationId": conv_id, "requestId": conv_id,
          "inputLength": 12, "requestModelId": "glm-5.2", "requestModelName": "GLM-5.2",
          "isPlan": False, "codebaseEnable": False, "maxToken": 0, "maxSteps": 0,
          "temperature": 0, "mentionContexts": [], "knowledgeId": [],
          "agentName": "default", "agentType": "conversation", "userId": uid}
    if activity_id:
        ev["activityId"] = activity_id
    return ev


def apply_mini_report_fix(mod, cfg):
    """修正小程序类任务（Sequential_Tasks_1 / school_season）的上报形状。

    🔑 这就是 `apply_report_shape_fix` 那个真凶，只是**这两处一直没人修**：
        · 上游 `_mini_report()`                  发 `{"common":...,"events":[...]}` **信封**
        · 上游 `t_school_season()` **内联**的 mp 上报  同样是信封
        → 服务端一律 **HTTP 200 但不计分**，日志表现就是打印了
          「mini chat 已上报」却永远停在 `accepted 0/1（服务端暂未关联）`。

    实测（2026-09-20 账号2，改成裸数组 + timestamp/reportDelay）：
        Sequential_Tasks_1  `accepted 0/1` → **3 秒内 completed 1/1**，并领到 +100积分+5能量。

    ⚠️ 结论：这两个任务**不需要抓包**。事件码、字段、域名、请求头上游本来就抄对了，
    错的只是**信封形状** —— 服务端对信封是静默忽略的，所以看起来"发出去了却没反应"。
    """
    if not cfg.get("fix_mini_report", True):
        return []
    if not hasattr(mod, "_mini_report"):
        return []

    applied = []

    def _fixed_mini_report(s, uid, nick, conv_id):
        return _mp_bare_report(mod, s, uid, nick,
                               [_mini_chat_event(uid, conv_id)])

    mod._mini_report = _fixed_mini_report
    applied.append("_mini_report→裸数组报文")

    # `Sequential_Tasks_2`（「完成 1 次专家对话」，200积分+5能量）是**序列任务的第 2 天**，
    # 上游 `t_sequential_tasks` 只认 `Sequential_Tasks_1` → 这个码从来没人处理。
    # 今天它是 `locked until 2026-09-21`（accept 直接回 task locked），所以逻辑先接上，
    # 明天解锁后自动生效；没解锁时 accept 失败就跳过，不会有副作用。
    if hasattr(mod, "t_sequential_tasks"):
        _orig_seq = mod.t_sequential_tasks

        def _seq_with_2(s=None, uid=None, nick=None, log=None, *a, **k):
            emit = log if callable(log) else print
            _orig_seq(s, uid, nick, log)
            code = "Sequential_Tasks_2"
            try:
                st, cur, tgt = mod._mp_prog(s, code)
            except Exception:  # noqa: BLE001
                return
            if st is None:
                return                       # 服务端没下发 → 不适用这个账号
            if st in ("completed", "claimed"):
                if st == "completed":
                    mod._mp_claim(s, code, emit)
                return
            if st == "not_accepted":
                if not mod._mp_accept(s, code):
                    # 最常见就是还没解锁（服务端回 task locked until …），静默跳过即可
                    return
                time.sleep(1.5)
            conv = "mini-seq2-" + str(uuid.uuid4())
            try:
                mod._mini_report(s, uid, nick, conv)
                emit("   序列任务2(专家对话): mini chat 已上报（%s）" % code)
            except Exception as exc:  # noqa: BLE001
                emit("   序列任务2: 上报异常 %s: %s" % (type(exc).__name__, str(exc)[:60]))
                return
            for _ in range(3):
                time.sleep(2.5)
                try:
                    st2, cur2, tgt2 = mod._mp_prog(s, code)
                except Exception:  # noqa: BLE001
                    continue
                if st2 in ("completed", "claimed"):
                    emit("   序列任务2: ✅ 已完成 %s/%s" % (cur2, tgt2))
                    if st2 == "completed":
                        mod._mp_claim(s, code, emit)
                    return
                if st2 is not None:
                    emit("   序列任务2: %s %s/%s" % (st2, cur2, tgt2))

        mod.t_sequential_tasks = _seq_with_2
        applied.append("t_sequential_tasks→补 Sequential_Tasks_2")

    # 校园日：上游把上报**内联**在 t_school_season 里（没有独立函数），只能整函数替换。
    # 逻辑与上游逐行对应，唯一差别是上报改成裸数组 + 多带 activityId。
    if hasattr(mod, "t_school_season"):
        def _fixed_school_season(s=None, uid=None, nick=None, log=None, *a, **k):
            emit = log if callable(log) else print
            try:
                st, cur, tgt = mod._mp_prog(s, "school_season")
                if st is None:
                    emit("   校园日活动: mp 口径未下发该任务，跳过")
                    return
                if st in ("completed", "claimed"):
                    if st == "completed":
                        mod._mp_claim(s, "school_season", emit)
                    else:
                        emit("   校园日活动: 已领取，跳过")
                    return
                if st == "not_accepted":
                    if not mod._mp_accept(s, "school_season"):
                        emit("   校园日活动: accept 失败，跳过")
                        return
                    time.sleep(float(getattr(mod, "WRITE_GAP", 1.5) or 1.5))
                conv = "mini-ss-" + str(uuid.uuid4())
                http = _mp_bare_report(
                    mod, s, uid, nick,
                    [_mini_chat_event(uid, conv, getattr(mod, "SCHOOL_ACTIVITY_ID", ""))])
                emit("   校园日活动: mini chat+activityId 已上报（HTTP %s）" % http)
                time.sleep(2.5)
                st2, cur2, tgt2 = mod._mp_prog(s, "school_season")
                if st2 in ("completed", "claimed"):
                    emit("   校园日活动: ✅ 已完成 %s/%s" % (cur2, tgt2))
                    if st2 == "completed":
                        mod._mp_claim(s, "school_season", emit)
                else:
                    emit("   校园日活动: %s %s/%s（服务端暂未关联）" % (st2, cur2, tgt2))
            except Exception as exc:  # noqa: BLE001
                emit("   校园日活动: 失败 %s: %s"
                     % (type(exc).__name__, str(exc)[:60]))

        mod.t_school_season = _fixed_school_season
        applied.append("t_school_season→裸数组报文")

    return applied


def apply_school_activity_fix(mod, cfg):
    """修正开学季活动（`www.codebuddy.cn/portal/activity/school`）的三个问题。

    🔴 2026-09-20 实测定位（详见 `probe_all_tasks.py` / `probe_expert_use.py`）：

      1. **`chat_3_times` 永远卡 1/3** —— `school_run_tasks` 在循环**外面**建了一个
         `conv_id`，3 条 `chat_request_send` 全用它；服务端按 conversationId 去重，
         所以三次只算一次。实测改成每条独立 conv → 1/3 → **3/3 completed** → 领奖成功。
         修法：包一层 `_school_mini_chat_event`，**每次调用都生成新的 conversationId**
         （签名不动，`school_run_tasks` 一个字不用改）。

      2. ✅ **`expert_use`（召唤1次开学季专家）报文信封缺失** —— 2026-09-20 攻克。
         🔴 2026-09-20 用户手动做完该任务并抓包（见 `probe_har.py` / `probe_expert_mp.py`），
         真实链路是**一条小程序遥测事件**（ POST `www.codebuddy.cn/v2/report`，**裸数组**）：

             {"eventCode": "expert_actual_use", "ideType": "WorkBuddy_MP",
              "ideVersion": "2.4.2", "extName": "workbuddy-mp", "extVersion": "2.4.2",
              "product": "SaaS", "os": "ios", "osVersion": "27.0", "arch": "",
              "machineId": <设备id>, "timezone": "Asia/Shanghai",
              "userId": <uid>, "userNickname": <昵称>, "ideName": "wx_app_cloud",
              "id": <expert_id>, "name": <expert_id>, "expertTitle": <职业>,
              "type": "send_message", "characterCount": 2, "expertType": "agent"}

         ✅ 实测（2026-09-20，账号2/账号4）：按上面形状发**一条**裸数组事件，
         3 秒内 `in_progress 0/1` → `completed 1/1` → 领奖成功。

         ⚠️ **根因尚未用单变量实验钉死**（重要，别把它当已证结论）：
         白天失败的 A1/A2 变体（`probe_expert_use.py`）**发的就是 `expert_actual_use`**
         —— 事件名是对的，照样失败。所以「名字错了」不是根因。
         失败版与成功版逐字段对比，最刺眼的一组差异是：失败版**一个客户端身份/环境字段
         都没带**（无 `ideType` / `extName` / `ideName` / `machineId` / `product` / `os` /
         `timezone` / `userNickname`），成功版带着完整的小程序身份信封。
         佐证：同活动里**能点亮**的 `chat_3_times` 事件恰好带
         `ideType: WorkBuddy_MP` + `extName: workbuddy-mp` + `ideName: wx_app_cloud`；
         而成长中心能用的 `t_expert_5` 走的是**桌面身份**（`ideName: WorkBuddy`）。
         → **最强候选是「缺客户端身份信封」**（开学季是小程序专属活动，服务端很可能按客户端
         身份归因），并列候选还有 `type` 值（`"agent"` vs `"send_message"`）、
         多余伪造字段污染、以及同批多发的 `expert_summoned`。四条未做隔离验证。
         ⚠️ 另外 `expert_summon_click`（真实流程里还有两条：type=`expert_list_card`
         position=2、type=`quick_prompt` position=0）**单独发不点亮**，不是必要条件；
         AS 会话（`tags:["expert:<id>"]`）+ ACP 对话回合**也不是必要条件**（已实测证伪）。
         `id` 必须是**真实 expert_id**（从 `expert/list` 的 `categories:["16-BackToSchool"]`
         取），旧代码读的 `e["id"]` 字段不存在 → 空串；更不能回落假 id `expert-school-01`。

      3. **`_school_report` 发的是信封** —— `{"common":..., "events":[...]}` 被服务端**静默
         忽略**（HTTP 200 但一分不记）。这是 `chat_3_times` 卡 1/3 的**真凶**：
         只改 conversationId 而仍发信封时，进度**纹丝不动**；换成裸数组就立刻翻页。
         修法：非桌面分支改发**裸数组**；桌面分支保持上游原样（那条路径实测可用）。

      4. **抽奖时机太早** —— 上游把 `school_lottery` 紧跟在 `school_run_tasks` 之后，
         而抽奖机会是**任务发奖时才发**的（服务端还延迟入账）→ 跑任务那一刻余额常常还是 0，
         整轮就白白跳过。实测：17:59 那轮各账号余额都是 0，补完任务后涨到合计 **7 次机会**
         没人抽（后来手工抽掉，得 162 积分）。
         → 抽奖挪到 `school_final_sweep()`（全账号跑完后再统一抽 + 兜底领奖）。
    """
    if not cfg.get("fix_school_activity", True):
        return []
    applied = []

    # ---- ① 每条 chat 事件一个独立 conversationId（破掉服务端按 conv 去重）----
    if hasattr(mod, "_school_mini_chat_event"):
        _orig_chat_ev = mod._school_mini_chat_event

        def _chat_ev_fresh_conv(uid, nick, conv_id=None):
            evs = _orig_chat_ev(uid, nick, "conv-" + str(uuid.uuid4()))
            # 上游还会把同一个 conv 塞进 requestId；一并换新，避免又撞去重键
            fresh = "conv-" + str(uuid.uuid4())
            for e in evs:
                if "conversationId" in e:
                    e["conversationId"] = fresh
                if "requestId" in e:
                    e["requestId"] = fresh
            return evs

        mod._school_mini_chat_event = _chat_ev_fresh_conv
        applied.append("_school_mini_chat_event→每次新 conversationId")

    # ---- ② 专家 id 读对字段，且不再回落假 id；第二个返回值改为**职业名**（当 expertTitle）----
    if hasattr(mod, "_school_fetch_expert"):
        def _fetch_expert_fixed(s):
            """照抄抓包：expert/list（categories=["16-BackToSchool"]）取真实 expert_id。

            返回 (expert_id, profession_zh)；后者会被 `_school_expert_event` 当作
            `expertTitle` 上报（抓包里就是职业名，不是显示名）。
            """
            try:
                r = mod._school_post(
                    s, mod.SCHOOL_DOMAIN + "/v2/operation-platform/market/expert/list",
                    {"edition_mode": "all,domestic", "page": 1, "page_size": 100,
                     "sort_by": "use_count", "sort_order": "desc",
                     "categories": [mod.SCHOOL_EXPERT_CATEGORY], "expert_type": "agent"})
                experts = (r.json().get("data") or {}).get("experts") or []
                if experts:
                    e = experts[0]
                    eid = e.get("expert_id") or e.get("source_id") or e.get("id") or ""
                    prof = (e.get("profession_zh") or e.get("profession")
                            or e.get("display_name_zh") or e.get("display_name")
                            or e.get("agent_name") or e.get("name") or "开学季专家")
                    if eid:
                        return eid, prof
            except Exception:  # noqa: BLE001
                pass
            # 🔴 不再回落 `expert-school-01`：服务端不认这个 id，只会白白发一发、
            #    还让日志出现假的「✅」。宁可返回空串让上层跳过。
            return "", "开学季专家"

        mod._school_fetch_expert = _fetch_expert_fixed
        applied.append("_school_fetch_expert→读 expert_id + 职业名")

    # ---- ②b ✅ 攻克点：`expert_use` 照抄抓包的真实报文（含小程序身份信封）----
    # 🔴 2026-09-20 关键突破。⚠️ 别把根因说成"事件名错了" —— 白天失败的 A1/A2 变体
    #    发的**也是** `expert_actual_use`，名字没错，照样失败。
    #    失败版与成功版最大的差异：失败版**没有任何客户端身份/环境字段**
    #    （无 ideType/extName/ideName/machineId/product/os/timezone/userNickname），
    #    成功版带着完整的小程序身份信封；同活动能点亮的 `chat_3_times` 也带这一套。
    #    → 最强候选是"缺客户端身份信封"（开学季是小程序专属活动，服务端按身份归因）；
    #      并列候选：`type` 值（agent→send_message）、多余伪造字段污染、同批的 expert_summoned。
    #      四条**未做单变量隔离**，别当已证结论（详见 docstring 第 2 条）。
    #    实测（账号2/4）：照抄下面形状后 3 秒内 0/1 → 1/1 completed → 领奖 ✅。
    #    （`_school_report` 的裸数组补丁 ③ 负责把 list 原样发出，不套信封。）
    if hasattr(mod, "_school_expert_event"):
        def _expert_actual_use_event(uid, nick, expert_id, expert_name, conv_id=None):
            ev = {"eventCode": "expert_actual_use", "timestamp": int(time.time() * 1000),
                  "ideType": "WorkBuddy_MP", "ideVersion": "2.4.2",
                  "extName": "workbuddy-mp", "extVersion": "2.4.2",
                  "product": "SaaS", "os": "ios", "osVersion": "27.0", "arch": "",
                  "machineId": mod.derive_id(uid, "mp-machine"),
                  "timezone": "Asia/Shanghai", "userId": uid, "userNickname": nick,
                  "ideName": "wx_app_cloud",
                  "id": expert_id, "name": expert_id, "expertTitle": expert_name,
                  "type": "send_message", "characterCount": 2, "expertType": "agent"}
            return [ev]

        mod._school_expert_event = _expert_actual_use_event
        applied.append("_school_expert_event→expert_actual_use（抓包真实形状）")

    # ---- ③ `_school_report` 发的也是**信封** → 服务端静默忽略 ----
    # 🔴 2026-09-20 实测：这才是 `chat_3_times` 永远卡 1/3 的**真凶**。
    #    上游 `_school_report()` 发 `{"common":..., "events":[...]}`，服务端回 HTTP 200
    #    却一分不记；日志里那句 `chat #N/3 ✅` 是**无条件打印**的假成功（跟 expert_use 同款坑）。
    #    实测对比（账号2，同一天同一账号）：
    #      · 只换 conversationId + 仍发信封 → `in_progress 1/3` **纹丝不动**
    #      · 换成裸数组       + 每次新 conv → 1/3 → 2/3 → **3/3 completed → 领奖 ✅**
    #    ⚠️ 桌面分支（desktop=True，走 copilot 域 + X-Product 头）**保持上游原样不碰**：
    #       `desktop_chat_1_time` 就是靠那条路径完成并领到奖的，没有证据说它也认裸数组。
    if hasattr(mod, "_school_report"):
        _orig_school_report = mod._school_report

        def _bare_school_report(s, uid, nick, events, host=None, desktop=False):
            if desktop:
                return _orig_school_report(s, uid, nick, events, host=host, desktop=True)
            target = (host or mod.SCHOOL_DOMAIN) + "/v2/report"
            return mod.api_retry(s, "POST", target, body=list(events))

        mod._school_report = _bare_school_report
        applied.append("_school_report→裸数组报文")

    return applied


def school_final_sweep(mod, summaries):
    """全账号跑完后的**开学季**兜底：补领「已完成未领」+ 把没抽的转盘抽掉。

    🔴 为什么必须有（2026-09-20）：
      · 上游 `school_run_tasks` 只轮询 5×2=10 秒就往下走，而服务端对埋点/状态是
        **延迟入账**的 → 等它翻成 `completed` 时，那一步的 claim 早跑过去了。
        实测 6 个账号的 `desktop_chat_1_time` 就这样「已完成但没领」（=600积分+6抽奖）。
        成长中心那边有 `final_claim_sweep` 兜底，**开学季一直没有**。
      · 抽奖机会是**发奖时才给**的，而 `school_lottery` 跑在任务之前 → 余额通常为 0。
        等所有账号都跑完，奖该到的都到了，这时候抽才抽得着。

    只做「查询 + 领奖 + 抽奖」，**不发任何埋点、不改任务状态**，无副作用
    （claim / draw 都是幂等的）。
    """
    stats = {"claimed": 0, "drawn": 0, "detail": []}
    if not getattr(mod, "ACCOUNTS", None):
        return stats

    for idx, acc in enumerate(mod.ACCOUNTS, 1):
        tok = (acc.get("access_token") or "").strip()
        if not tok:
            continue
        nick = acc.get("note", "?")
        tag = "账号%d" % idx
        try:
            sc = mod._school_session(tok)
            tasks, in_period = mod._school_fetch_tasks(sc)
        except Exception as exc:  # noqa: BLE001
            print("   %s %s: 开学季查询失败 %s" % (tag, nick, type(exc).__name__))
            continue
        if not in_period:
            continue

        # ① 补领「已完成未领」
        for t in tasks:
            if not isinstance(t, dict) or t.get("status") != "completed":
                continue
            code = t.get("task_code", "")
            if not code:
                continue
            try:
                ok = mod._school_claim(sc, code)
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                stats["claimed"] += 1
                stats["detail"].append((idx, nick, code))
                print("   %s %s: 🎁 补领 %s" % (tag, nick, code))

        # ② 没抽的转盘抽掉
        try:
            d = mod._school_get(sc, mod.SCHOOL_BASE + "/config").json()
            bal = ((d.get("data") or {}).get("chance") or {}).get("balance", 0) or 0
            if bal > 0:
                print("   %s %s: 🎡 转盘余额 %s，开始抽" % (tag, nick, bal))
                mod.school_lottery(sc, mod.uid_of(tok), mod.nickname_of(tok),
                                   lambda m: print("   " + m.strip()))
                stats["drawn"] += 1
        except Exception as exc:  # noqa: BLE001
            print("   %s %s: 抽奖失败 %s" % (tag, nick, type(exc).__name__))
    return stats


def apply_post_claim_recheck(mod, cfg):
    """给上游 run_account 包一层「领奖后复查」。

    上游 run_account 的执行顺序是：

        … → t_lottery（抽奖）→ t_blindbox（盲盒）→ … → 🎁 领奖（每个 +5 能量）
                                                          ↑ 钱在这儿才到账

    也就是说：**本次运行里刚领到的能量，要等下一次运行才能拿去开盲盒**。
    实测账号5 开跑时能量 3、盲盒要求 ≥10 → 直接 `📦盲盒: 能量不足`，
    而这一轮领奖又能进账几十点能量，全白等到明天。

    这里包一层：原流程（含领奖）跑完后，再查一次抽奖次数与盲盒额度，把新到账的立刻用掉。
    t_lottery / t_blindbox 都是「先查余额、够了才动手」，重复调用天然幂等，不会多花。
    """
    if not cfg.get("post_claim_recheck", True):
        return []
    if not hasattr(mod, "run_account"):
        return []

    orig = mod.run_account

    def wrapped(idx, acc, do_desktop):
        msgs, summary = orig(idx, acc, do_desktop)
        tok = (acc.get("access_token") or "").strip()
        if not tok:
            return msgs, summary

        tag = "账号%d" % idx

        def log(m):
            line = "[%s][%s] %s" % (time.strftime("%H:%M:%S"), tag, m)
            print(line)
            msgs.append(line)

        try:
            s = mod.new_api(tok)
            uid = mod.uid_of(tok)
            nick = mod.nickname_of(tok)
            log("  🔁 ── 领奖后复查（本次新到账的能量/次数立刻用掉）──")
            if hasattr(mod, "t_lottery"):
                mod.t_lottery(s, uid, nick, log)
            if hasattr(mod, "t_blindbox"):
                mod.t_blindbox(s, uid, nick, log)
            # 关键：桌面阶段跑得比 t_accept_all 还早，那时 skill_1 可能还是 not_accepted，
            # 真实对话的埋点就白费了（实测账号6 就这样失败）。领奖之后任务肯定已被接受，补一次。
            if cfg.get("skill_real_chat", True):
                try:
                    credit_skill_via_real_chat(mod, s, uid, nick, log)
                except Exception as exc:
                    log("   🔁 技能复查异常（已忽略）：%s: %s"
                        % (type(exc).__name__, str(exc)[:60]))
            # 🔴 兜底：上面所有补丁点亮的任务，服务端常常是**延迟入账**的 ——
            # 上游那个领奖循环跑到时它们还是 accepted，于是整轮白干（用户报障根因）。
            # 这里最后再全量扫一遍，把「已完成未领」统统收掉。
            try:
                claimed = claim_all_completed(mod, s, log)
                if claimed:
                    # 顺手刷新完成度，免得报告里显示的还是领奖前的旧数字
                    r2 = s.get(mod.BASE + "/v2/activity/growth/tasks", timeout=25, verify=False).json()
                    lst = (r2.get("data") or {}).get("tasks") or []
                    summary["done"] = sum(1 for t in lst if isinstance(t, dict)
                                          and t.get("accept_status") in ("claimed", "completed"))
                    summary["total"] = len(lst)
                    summary["rest"] = [mod.task_cn(t.get("task_code", "")) for t in lst
                                       if isinstance(t, dict)
                                       and t.get("accept_status") not in ("claimed", "completed")]
                    log("   📈 兜底领奖后更新完成度: %s/%s"
                        % (summary.get("done"), summary.get("total")))
            except Exception as exc:
                log("   🔁 兜底领奖异常（已忽略）：%s: %s" % (type(exc).__name__, str(exc)[:60]))
            # 能量会变，报告里那个数顺手更新，免得显示的是领奖前的旧值
            try:
                e = s.get(mod.BASE + "/v2/activity/growth/energy", timeout=25, verify=False)
                bal = (e.json().get("data") or {}).get("balance")
                if bal is not None:
                    summary["energy"] = bal
                    log("   ⚡ 复查后能量: %s" % bal)
            except Exception:
                pass
        except Exception as exc:
            # 复查失败不能把已经跑完的账号算成失败
            log("   🔁 复查异常（已忽略）：%s: %s" % (type(exc).__name__, str(exc)[:60]))
        return msgs, summary

    mod.run_account = wrapped
    return ["run_account→领奖后复查盲盒/抽奖"]


def run_accounts(mod, args, cfg, desktop_idx=None):
    """逐个账号执行，返回 (summaries, failures)。

    刻意不复用上游的 main() 循环 —— 那个循环没有异常隔离，任何一个账号
    （token 失效、接口 401、网络抖动）抛出异常都会让后面所有账号一起不跑。
    账号每天只跑一两次，一个账号偶发失败是很正常的事，不该连坐。

    desktop_idx：此刻真实桌面端登录的账号序号（来自 find_desktop_account）。
    scope=current 时只有它允许做桌面类任务；认不出来（None）则谁都不给 ——
    宁可少做，也不替真实桌面账号造会话。
    """
    do_desktop = resolve_desktop_mode(cfg, args) in ("fingerprint", "full")
    scope = resolve_desktop_scope(cfg, args)

    if args.only:
        # 必须取 ACCOUNTS[only-1]：引擎刻意不给上游传 --only，上游加载的是全部账号，
        # 所以下标 = 序号 - 1，和账号库/页面上的顺序一致。
        if not 1 <= args.only <= len(mod.ACCOUNTS):
            raise SystemExit("--only %d 超出范围（账号库共 %d 个）" % (args.only, len(mod.ACCOUNTS)))
        targets = [(args.only, mod.ACCOUNTS[args.only - 1])]
    else:
        targets = list(enumerate(mod.ACCOUNTS, 1))

    summaries, failures = [], []
    for pos, (idx, entry) in enumerate(targets):
        if pos:
            time.sleep(3)  # 账号之间喘口气，别把写动作排得太密
        nick = entry.get("note", "?")
        print("\n" + "─" * 66)
        print("▶ 开始第 %d/%d 个账号：%s" % (pos + 1, len(targets), nick))
        print("─" * 66)
        # scope=current 时，桌面类任务只交给「此刻真实桌面端登录的那个账号」
        allow_desktop = do_desktop and (scope == "all" or idx == desktop_idx)
        try:
            _, sm = mod.run_account(idx, entry, allow_desktop)
            summaries.append(sm)
        except SystemExit:
            raise
        except Exception as exc:
            msg = "%s: %s" % (type(exc).__name__, exc)
            failures.append((idx, nick, msg))
            print("\n❌ 账号%d %s 执行异常，已跳过继续下一个 —— %s" % (idx, nick, msg))
            import traceback
            traceback.print_exc()
            summaries.append({
                "idx": idx, "note": nick, "credits": "", "usage": "",
                "done": 0, "total": 0, "rest": ["执行异常(见日志)"], "level": "?", "energy": "?",
            })
    return summaries, failures


def final_claim_sweep(mod, summaries):
    """全账号跑完后的最后一道兜底：再扫一遍所有账号的「已完成未领」。

    🔴 为什么还要再来一遍（2026-09-20 用户报障）：
        服务端对某些埋点是**延迟入账**的 —— 事件发出后要过几十秒才把任务从
        accepted 翻成 completed。单账号内部那次 `claim_all_completed` 也可能
        刚好卡在延迟窗口内什么都没扫到。等所有账号都跑完再回来扫，这点延迟早就过了。

    这一步只做「查 + 领」，不发任何埋点、不动任何任务状态，不会造成副作用
    （`claim` 幂等，已领过会回 already_claimed）。
    """
    stats = {"checked": 0, "claimed": 0, "detail": []}
    by_idx = {}
    for sm in summaries:
        if isinstance(sm, dict) and sm.get("idx"):
            by_idx[sm["idx"]] = sm
    for idx, acc in enumerate(mod.ACCOUNTS, 1):
        tok = (acc.get("access_token") or "").strip()
        if not tok:
            continue
        nick = acc.get("note", "?")
        tag = "账号%d" % idx

        def make_log(_tag=None):
            """每个账号单独造一个 logger。

            ⚠️ 不能用循环变量做闭包默认值那种写法去共享同一个函数 —— 这里显式
            按账号造新函数，`_tag` 绑定死在当前账号上，不会串号。
            """
            _t = _tag or tag

            def _log(m):
                print("[%s][%s] %s" % (time.strftime("%H:%M:%S"), _t, m))
            return _log

        log = make_log()

        try:
            s = mod.new_api(tok)
            got = claim_all_completed(mod, s, log)
            stats["checked"] += 1
            if got:
                stats["claimed"] += len(got)
                stats["detail"].append((idx, nick, got))
            # 🔴 报告里的「未完成」清单必须**每次都刷新**，不能只在「这次领到了」时才刷。
            # 2026-09-20 实测踩的坑：尝鲜热门技能(skill_1) 是单账号阶段之后才被服务端
            # 翻成 completed 的，本次兜底「无奖可领」→ 走不到原来的刷新分支 →
            # 最终报告仍显示「剩余: 尝鲜热门技能」，而实际再查已经是 14/19 了。
            # 报告是给人看的，宁可多查一次接口，也不能显示过期结论。
            sm = by_idx.get(idx)
            if sm is not None:
                try:
                    r2 = s.get(mod.BASE + "/v2/activity/growth/tasks",
                               timeout=25, verify=False).json()
                    lst = (r2.get("data") or {}).get("tasks") or []
                    sm["done"] = sum(1 for t in lst if isinstance(t, dict)
                                     and t.get("accept_status") in ("claimed", "completed"))
                    sm["total"] = len(lst)
                    sm["rest"] = [mod.task_cn(t.get("task_code", "")) for t in lst
                                  if isinstance(t, dict)
                                  and t.get("accept_status") not in ("claimed", "completed")]
                except Exception:
                    pass
        except Exception as exc:
            log("兜底领奖失败（已忽略）：%s: %s" % (type(exc).__name__, str(exc)[:60]))
    return stats


def run(args, observer=None, dry_run_override=None, log_sink=None):
    """执行一轮任务（CLI 与服务层共用这一条路径）。

    参数：
        args            已经补齐字段的命名空间（服务层用 normalize_args() 造）
        observer        可选，签名 observer(str) —— 每产出一行日志回调一次，
                        服务层用它做 SSE 实时日志。None 表示不要。
        dry_run_override  强制指定是不是 dry-run（服务层按配置传）；None 表示按原逻辑推断
        log_sink        可选，额外的日志写入对象（如已打开的文件句柄）

    返回 dict：{"ok", "dry_run", "summaries", "failures", "blocked", "logfile", "elapsed"}

    🔴 与旧实现的关键区别（2026-09-20 审计修复）：
        1. **stdout 劫持严格成对还原** —— 服务长驻进程里绝不能把 sys.stdout 永久换掉
           （那会吞掉 uvicorn 自己的日志、且 WebSocket 也会被牵连）。
        2. **dry-run 补丁在 finally 里还原** —— 否则跑过一次 dry-run 之后，
           所有真实运行都会被静默拦成假响应（最隐蔽也最危险的一种坏法）。
        3. 不再把「无可用账号」当 SystemExit 直接崩 —— 服务层要的是一个可判断的结果。
    """
    cfg = load_config()
    desktop_mode = resolve_desktop_mode(cfg, args)
    desktop_scope = resolve_desktop_scope(cfg, args)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    accounts, skipped = read_switch_accounts(cfg)

    # 只有 scope=current 才需要判定「此刻真实桌面端是谁」。
    # 判定失败时 desktop_idx 保持 None —— 宁可谁都不做，也不猜一个账号去当桌面账号。
    desktop_idx, desktop_note = None, "scope=all，未做判定"
    if desktop_scope == "current":
        desktop_idx, desktop_note = find_desktop_account(accounts)

    if skipped:
        print("⏭️  已过滤：%s" % ", ".join(skipped))

    if not accounts:
        print("❌ 没有可用的国内版账号（检查 wb-switch 里是否已登录）")
        return {"ok": False, "reason": "no_accounts", "dry_run": True,
                "summaries": [], "failures": [], "blocked": 0,
                "logfile": None, "elapsed": 0.0}

    if getattr(args, "list", False):
        describe_plan(cfg, accounts, getattr(args, "only", None), desktop_mode,
                      desktop_scope, desktop_idx, desktop_note)
        return {"ok": True, "reason": "list", "dry_run": True,
                "summaries": [], "failures": [], "blocked": 0,
                "logfile": None, "elapsed": 0.0}

    if dry_run_override is True:
        dry_run = True
    elif dry_run_override is False:
        dry_run = False
    else:
        dry_run = not getattr(args, "live", False)
        if dry_run and not cfg.get("dry_run_default", True):
            dry_run = False

    logfile = LOG_DIR / ("run-%s%s.log" % (time.strftime("%Y%m%d-%H%M%S"),
                                           "-dryrun" if dry_run else ""))
    fh = None
    restore_dry = None
    prev_stdout = sys.stdout
    started = time.time()
    summaries, failures = [], []
    try:
        fh = open(logfile, "w", encoding="utf-8")
        # 严格成对：离开这个函数时一定还原成 prev_stdout。
        # on_line 给 SSE 订阅者用；sink 用于落日志文件（两个都可有可无）。
        sys.stdout = StreamCapture(prev_stdout, on_line=observer, sink=fh)
        mark_running()

        print("=" * 66)
        print("🌱 wb-task-hub  %s" % ("【DRY-RUN 只读模式】" if dry_run else "【真实执行】"))
        print("=" * 66)
        describe_plan(cfg, accounts, getattr(args, "only", None), desktop_mode,
                      desktop_scope, desktop_idx, desktop_note)

        if dry_run:
            print("🔒 只读模式：所有非 GET 请求都会被拦截，不会产生任何任务进度。\n")

        # 上游任务逻辑静默同步（默认开启，12 小时内不重复检查）。
        # 安全检查不过时会写告警但保留旧版，这里继续用旧版往下跑，不阻塞任务。
        if not getattr(args, "no_update", False):
            try:
                import update as updater
                result = updater.sync(verbose=True)
                if result.get("status") == "blocked":
                    print("⚠️  上游新版本未通过安全检查，本次仍使用旧版运行\n")
            except Exception as exc:
                print("⚠️  上游同步跳过（%s: %s）\n" % (type(exc).__name__, exc))

        write_token_file(accounts)

        # 上游模块级代码会读 sys.argv，必须在我们控制的参数集下加载
        argv = ["workbuddy_daily"]
        if desktop_mode == "off":
            argv.append("--no-desktop")
        if getattr(args, "no_school", False) or not cfg.get("school_activity", True):
            argv.append("--no-school")
        if getattr(args, "gap", None):
            argv.extend(["--gap", str(args.gap)])
        elif cfg.get("write_gap"):
            argv.extend(["--gap", str(cfg["write_gap"])])
        # 刻意不传 --only：让上游把账号库里所有账号都加载进来，
        # 「跑哪个」由 run_accounts 自己决定 —— 这样日志里的「账号N」始终对应账号库的真实序号，
        # 也才能做到单账号失败不连坐。
        sys.argv = argv

        if dry_run:
            restore_dry = install_dry_run()

        mod = load_upstream()
        relabel_accounts(mod, accounts)

        applied = apply_exclusions(mod, cfg)
        print("⛔ 已掏空上游函数：%s" % (", ".join(applied) if applied else "（无）"))
        hardened = apply_desktop_safety(mod, desktop_mode, cfg)
        if hardened:
            print("🛡️ 桌面流程已加固：%s" % ", ".join(hardened))
        extra = apply_post_claim_recheck(mod, cfg)
        if extra:
            print("🔁 已挂上领奖后复查：%s" % ", ".join(extra))
        web_fix = apply_web_report_fix(mod, cfg)
        if web_fix:
            print("🧩 已修正 web 类上报形状：%s" % ", ".join(web_fix))
        shape_fix = apply_report_shape_fix(mod, cfg)
        if shape_fix:
            print("🧩 已修正桌面事件上报形状：%s" % ", ".join(shape_fix))
        cat_fix = apply_black_cat_fix(mod, cfg)
        if cat_fix:
            print("🐱 已加固夜猫子任务：%s" % ", ".join(cat_fix))
        mini_fix = apply_mini_report_fix(mod, cfg)
        if mini_fix:
            print("🧩 已修正小程序上报形状：%s" % ", ".join(mini_fix))
        school_on = not (getattr(args, "no_school", False)
                         or not cfg.get("school_activity", True))
        school_fix = apply_school_activity_fix(mod, cfg) if school_on else []
        if school_fix:
            print("🏫 已修正开学季活动：%s" % ", ".join(school_fix))
        print("   上游脚本版本 sha256=%s\n" % upstream_sha())

        summaries, failures = run_accounts(mod, args, cfg, desktop_idx)

        # 🔴 全账号跑完的最后一道闸：服务端对埋点是延迟入账的，单账号内部那次清扫
        # 可能刚好卡在延迟窗口里。等所有账号跑完再统一扫一遍「已完成未领」。
        if not dry_run and cfg.get("final_claim_sweep", True):
            print("\n" + "─" * 66)
            print("🎁 ── 全账号兜底领奖（扫「已完成未领」）──")
            print("─" * 66)
            try:
                sweep = final_claim_sweep(mod, summaries)
                if sweep["claimed"]:
                    for idx, nick, got in sweep["detail"]:
                        print("   账号%d %s: 补领 %d 项 → %s" % (idx, nick, len(got), ", ".join(got)))
                    print("   合计补领 %d 项奖励" % sweep["claimed"])
                else:
                    print("   ✅ 无「已完成未领」")
            except Exception as exc:
                print("   兜底领奖异常（已忽略）：%s: %s" % (type(exc).__name__, str(exc)[:60]))

        # 🔴 开学季是**另一套任务 + 另一套转盘**（codebuddy.cn/portal/activity/school），
        #    跟成长中心的 final_claim_sweep 完全不通用，必须单独扫尾：
        #      · 上游只轮询 10 秒就 claim，服务端延迟入账 → 「已完成但没领」会漏（实测 6 个账号）
        #      · 转盘机会是发奖时才给的，而 school_lottery 跑在任务之前 → 跑那刻余额还是 0
        if (not dry_run and school_on
                and cfg.get("fix_school_activity", True)
                and cfg.get("school_final_sweep", True)):
            print("\n" + "─" * 66)
            print("🏫 ── 开学季兜底（补领「已完成未领」+ 抽转盘）──")
            print("─" * 66)
            try:
                ssweep = school_final_sweep(mod, summaries)
                for idx, nick, code in ssweep["detail"]:
                    print("   账号%d %s: 🎁 补领 %s" % (idx, nick, code))
                if ssweep["claimed"] or ssweep["drawn"]:
                    print("   合计补领 %d 项，%d 个账号抽了转盘"
                          % (ssweep["claimed"], ssweep["drawn"]))
                else:
                    print("   ✅ 无「已完成未领」，转盘余额为 0")
            except Exception as exc:
                print("   开学季兜底异常（已忽略）：%s: %s"
                      % (type(exc).__name__, str(exc)[:60]))

        print("\n" + "=" * 66)
        print(mod.build_summary(summaries))
        if failures:
            print("\n⚠️ %d 个账号执行异常（其余账号已正常跑完，可单独重跑）:" % len(failures))
            for idx, nick, msg in failures:
                print("   · 账号%d %s —— %s" % (idx, nick, msg))
        try:
            mod.send_notify_all("WorkBuddy 任务报告", mod.build_summary(summaries))
        except Exception as exc:
            print("（推送跳过：%s）" % exc)

        print("\n" + "=" * 66)
        print("耗时 %.1f 秒" % (time.time() - started))
        if dry_run:
            print("🔒 DRY-RUN 共拦截 %d 个写请求（未产生任何实际变化）" % len(BLOCKED_WRITES))
            seen = {}
            for verb, url in BLOCKED_WRITES:
                key = "%s %s" % (verb, url.split("?")[0])
                seen[key] = seen.get(key, 0) + 1
            for key, cnt in sorted(seen.items(), key=lambda x: -x[1])[:15]:
                print("     %4d×  %s" % (cnt, key))
            if len(seen) > 15:
                print("     … 另有 %d 种" % (len(seen) - 15))
        print("日志: %s" % logfile)
        print("=" * 66)

        return {"ok": True, "reason": "done", "dry_run": dry_run,
                "summaries": summaries, "failures": failures,
                "blocked": len(BLOCKED_WRITES), "logfile": str(logfile),
                "elapsed": time.time() - started}
    finally:
        # 🔴 顺序很重要：先还原 stdout，再收尾（否则收尾期间的输出会丢）
        sys.stdout = prev_stdout
        if restore_dry is not None:
            try:
                restore_dry()
            except Exception:
                pass
        clear_running_mark()
        try:
            if fh is not None:
                fh.close()
        except Exception:
            pass


def main():
    args = parse_args()
    res = run(args)
    return 0 if res.get("ok") else 2


def upstream_sha():
    import hashlib
    script = VENDOR / "workbuddy_daily.py"
    if not script.exists():
        return "?"
    return hashlib.sha256(script.read_bytes()).hexdigest()[:16]


if __name__ == "__main__":
    sys.exit(main())
