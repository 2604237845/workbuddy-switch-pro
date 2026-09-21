#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wb-hub 服务：把 wb-task-hub 的任务引擎挂成常驻服务

为什么要做成服务（用户 2026-09-20 明确要求）：
    「这些任务的所有定时任务都不要用 workbuddy 的定时任务，因为我可能退出 workbuddy
      导致没办法运行，我希望能够以后台服务的形式定时运行，包括每晚 23:30 把「夜猫子」收掉的任务。」

所以定时逻辑**完全由本服务自己的调度线程负责**，不依赖 WorkBuddy 客户端在线、
也不依赖 WorkBuddy 的自动化功能。只要这台机器开着、服务在跑，任务就会按时执行。

设计：
  - FastAPI + 单页前端
  - 内置 AutoEngine 风格的后台线程：
      · 定时调度线程：按「每日计划表」到点触发一次运行（含 23:30 收夜猫子）
      · 运行互斥：同一时刻只允许一轮任务在跑（手动/定时都要抢同一把锁）
      · 实时日志：环形缓冲 + 订阅者回调 → SSE 推给前端
  - 配置持久化在 config/schedule.json（原子写）
  - 账号与任务配置沿用 tasks/engine/config/tasks.json

安全约束（继承引擎的，别改）：
  - 不改 workbuddy-switch 的账号库（只读）
  - 不刷新 refresh token
  - 绝不 taskkill WorkBuddy
  - 默认只读（dry-run），要真实执行必须在页面上显式开「真实执行」开关
"""

import asyncio
import json
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import urllib3
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

urllib3.disable_warnings()

# ---------------------------------------------------------------- 路径与常量

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
HUB_DIR = ROOT.parent / "engine"            # 任务引擎所在目录（tasks/engine）
CONFIG_DIR = ROOT / "config"
SCHEDULE_PATH = CONFIG_DIR / "schedule.json"
LOG_DIR = ROOT / "logs"

# 本服务与引擎同 venv；引擎通过 import 直接调用（不走子进程，日志才能实时拿到）
sys.path.insert(0, str(HUB_DIR))

CST = timezone(timedelta(hours=8))

HOST = "127.0.0.1"      # 默认只绑本机。要开局域网自行改，注意本服务无鉴权。
PORT = 8793

SERVICE_NAME = "wb-hub"
SERVICE_VERSION = "1.0.0"

urllib3.disable_warnings()


# ---------------------------------------------------------------- 工具

def now_cst() -> datetime:
    return datetime.now(CST)


def atomic_write_json(path: Path, obj: Any) -> None:
    """临时文件 + os.replace —— 可能被并发读的持久化文件必须原子写。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def code_fingerprint() -> str:
    """服务代码指纹（server.py + 前端 + 引擎），供 launch.py 判断「实例要不要重启」。

    🔴 必须包含 `wb-task-hub/engine.py`：`EngineBridge.load()` 会把引擎模块**缓存**在
    `self._mod`，改了引擎不重启的话，定时任务跑的还是旧代码（2026-09-20 踩过，
    当时只能靠人工确认「新代码到底加载了没有」）。
    ⚠️ **不要**把 `wb-task-hub/vendor/workbuddy_daily.py` 放进来：`load_upstream()`
    每轮重读、**不缓存**，改它本来就不需要重启，放进来只会造成无谓重启。
    文件缺失时填固定占位符，保证与 launch.py 里同名函数**结果一致**。
    """
    import hashlib

    h = hashlib.sha1()
    for p in (ROOT / "server.py", STATIC_DIR / "index.html", HUB_DIR / "engine.py"):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:12]


# 🔴 **必须在进程启动时快照，不能在每次请求时现算**。
# 现算的话，实例报出去的永远是「此刻磁盘上的内容」，而 launch.py 也是现算 → 两边必然相等
# → `code_is_newer()` 恒为 False → 「✔ 代码是最新的」变成一句废话：
# 改完 server.py（甚至改完引擎）它照样说「最新」，只能靠人工去确认（2026-09-20 就是栽在这）。
# 只在启动时算一次，这个值才代表「本进程加载的是哪一版代码」，之后磁盘再变就能被比出来。
CODE_FINGERPRINT_AT_START = code_fingerprint()


# ---------------------------------------------------------------- 配置

DEFAULT_SCHEDULE = {
    "_说明": "wb-hub 自己的定时计划（不依赖 WorkBuddy 的自动化功能）。改完即时生效，无需重启。",
    "enabled": True,
    "live": True,                 # true=真实执行；false=只读演练（dry-run）
    "no_update": True,            # 定时跑时跳过上游静默同步（避免定时任务被网络拖住）
    # 切换账号守望者：盯着桌面端「此刻登录的是谁」，一变就对刚切过去的账号单独补跑。
    # 刚切过去的账号有**真实桌面会话**，那一刻跑它成功率最高。
    "watch": {
        "enabled": True,
        "poll_seconds": 5,
        "debounce_seconds": 30,     # uid 变化后连续这么久不再变才动手（切换会反复写文件）
        "min_gap_minutes": 120,     # 同账号两次补跑最小间隔，防来回抖动
        "run_on_start": False,      # 服务启动时若当前账号没跑过，是否立刻补一次
    },
    "jobs": [
        {
            "id": "night_owl",
            "name": "夜猫子（23:30 收）",
            "enabled": True,
            "at": "23:30",
            "desc": "「夜猫子」任务只在 23:00–08:00(CST) 计数，所以安排在 23:30 全量跑一轮。",
            "only": None,
            "days": [0, 1, 2, 3, 4, 5, 6],   # 0=周一 … 6=周日
        },
        {
            "id": "daily_full",
            "name": "每日全量（08:30）",
            "enabled": True,
            "at": "08:30",
            "desc": "每天上午把成长中心 / 盲盒 / 抽奖 / 开学季等全部任务跑一遍。",
            "only": None,
            "days": [0, 1, 2, 3, 4, 5, 6],
        },
    ],
}


def load_schedule() -> Dict[str, Any]:
    """读调度配置；坏文件不当作「没有配置」，而是保留副本后回退默认。"""
    if not SCHEDULE_PATH.exists():
        return json.loads(json.dumps(DEFAULT_SCHEDULE))
    try:
        data = json.loads(SCHEDULE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        keep = SCHEDULE_PATH.with_name("schedule.corrupt-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(SCHEDULE_PATH, keep)
        except Exception:
            pass
        ENGINE.log("⚠️", "调度配置读不出来（%s），已保留为 %s，回退默认配置"
                   % (type(exc).__name__, keep.name))
        return json.loads(json.dumps(DEFAULT_SCHEDULE))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_SCHEDULE))
    data.setdefault("enabled", True)
    data.setdefault("live", True)
    data.setdefault("no_update", True)
    if not isinstance(data.get("watch"), dict):
        data["watch"] = json.loads(json.dumps(DEFAULT_SCHEDULE["watch"]))
    if not isinstance(data.get("jobs"), list):
        data["jobs"] = json.loads(json.dumps(DEFAULT_SCHEDULE["jobs"]))
    return data


def save_schedule(patch: Dict[str, Any]) -> Dict[str, Any]:
    """合并式保存（只覆盖传进来的键），返回新配置。

    ⚠️ `watch` 是**子字典**，必须逐键合并而不是整段替换 ——
    否则页面点一次守望者开关（只发 {"watch":{"enabled":true}}）就会把
    poll_seconds / min_gap_minutes 等参数全冲掉。
    """
    cur = load_schedule()
    for k, v in (patch or {}).items():
        if k.startswith("_"):
            continue
        if k == "watch" and isinstance(v, dict):
            w = cur.get("watch")
            if not isinstance(w, dict):
                w = json.loads(json.dumps(DEFAULT_SCHEDULE["watch"]))
            for wk, wv in v.items():
                if not str(wk).startswith("_"):
                    w[wk] = wv
            cur["watch"] = w
            continue
        cur[k] = v
    cur.pop("_说明", None)
    atomic_write_json(SCHEDULE_PATH, cur)
    return load_schedule()


# ---------------------------------------------------------------- 日志中枢

class LogHub:
    """环形日志缓冲 + 订阅者广播。

    引擎通过 observer 回调把每一行塞进来，SSE 端点据此实时推送。
    前端用「拿到的最大 seq」做增量拉取，断线重连不会重复也不会漏。
    """

    def __init__(self, maxlen: int = 2000):
        self._lock = threading.Lock()
        self._seq = 0
        self._buf = deque(maxlen=maxlen)
        self._subs: List[Any] = []
        self._sub_lock = threading.Lock()

    # stdout 只被 launch.py 重定向到 _private/server.log；前台直接跑时就是控制台。
    # 用**黑名单**而不是白名单：将来新增的级别默认可见（可观测性优先），
    # 只把「引擎逐行业务输出」压掉，避免一轮几百行刷屏。
    _STDOUT_SKIP_LEVELS = ("engine",)

    def emit(self, level: str, msg: str) -> None:
        with self._lock:
            self._seq += 1
            item = {
                "seq": self._seq,
                "ts": now_cst().strftime("%H:%M:%S"),
                "level": level,
                "msg": msg,
            }
            self._buf.append(item)
        for q in list(self._subs):
            try:
                q.put_nowait(item)
            except Exception:
                pass
        # 🔴 必须往 stdout 写一行：pythonw 的 stdout 被 launch.py 重定向到 server.log，
        # 而前台 `python server.py` 就是控制台。不写的话启动过程**一行输出都没有**，
        # 2026-09-20 就因此把「正常启动」误判成「静默退出」排查了半天。
        if level not in self._STDOUT_SKIP_LEVELS:
            try:
                print("[%s][%s] %s" % (item["ts"], level, msg), flush=True)
            except Exception:  # noqa: BLE001
                pass

    def since(self, seq: int = 0) -> Dict[str, Any]:
        with self._lock:
            return {
                "seq": self._seq,
                "items": [x for x in self._buf if x["seq"] > seq],
            }

    def subscribe(self):
        q: "asyncio.Queue" = asyncio.Queue(maxsize=1000)
        with self._sub_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._sub_lock:
            try:
                self._subs.remove(q)
            except ValueError:
                pass

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()


LOG = LogHub()


# ---------------------------------------------------------------- 引擎桥接

class EngineBridge:
    """把 wb-task-hub 的 engine 模块包一层，供服务调用。

    刻意**懒加载**：engine 在 import 时会读配置、算路径；服务启动时若引擎文件缺失，
    不应该让整个服务起不来（页面还能看状态、给提示）。
    """

    def __init__(self):
        self._mod = None
        self._up = None          # 上游 vendor 模块（new_api / BASE / task_cn 在这里）
        self._err = ""

    def load(self):
        """加载引擎模块（幂等）。失败时抛异常，由调用方决定怎么提示。"""
        if self._mod is not None:
            return self._mod
        if not (HUB_DIR / "engine.py").exists():
            raise RuntimeError("找不到引擎：%s" % (HUB_DIR / "engine.py"))
        import importlib.util

        spec = importlib.util.spec_from_file_location("wb_task_hub_engine", HUB_DIR / "engine.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["wb_task_hub_engine"] = mod
        spec.loader.exec_module(mod)
        self._mod = mod
        return mod

    def available(self) -> bool:
        try:
            self.load()
            return True
        except Exception as exc:
            self._err = "%s: %s" % (type(exc).__name__, exc)
            return False

    @property
    def error(self) -> str:
        return self._err

    def accounts(self) -> List[Dict[str, Any]]:
        """读账号库（只读）。读不出来时抛异常，**不返回空列表**。"""
        mod = self.load()
        cfg = mod.load_config()
        accounts, skipped = mod.read_switch_accounts(cfg)
        return accounts, skipped

    def upstream(self):
        """加载上游模块（vendor/workbuddy_daily.py），幂等。

        ⚠️ 这里要的是**上游**模块，不是引擎模块：`new_api()` / `BASE` / `task_cn()`
        都是上游定义的（引擎没有这些）。之前误写成 `mod.new_api` 导致页面整列报
        `AttributeError: module 'wb_task_hub_engine' has no attribute 'new_api'`。

        上游在**模块级**就会读 sys.argv 并 load_accounts()，所以临时固定 argv，
        免得把服务自己的启动参数喂进去。
        """
        if self._up is not None:
            return self._up
        mod = self.load()
        saved = sys.argv
        try:
            sys.argv = ["workbuddy_daily"]
            self._up = mod.load_upstream()
        finally:
            sys.argv = saved
        return self._up

    def account_status(self, idx: int, tok: str) -> Dict[str, Any]:
        """查单个账号的任务完成情况（只读 GET）。"""
        up = self.upstream()
        s = up.new_api(tok)
        r = s.get(up.BASE + "/v2/activity/growth/tasks", timeout=25, verify=False).json()
        tasks = (r.get("data") or {}).get("tasks") or []
        prof = {}
        try:
            prof = (s.get(up.BASE + "/v2/activity/growth/profile",
                          timeout=25, verify=False).json().get("data") or {})
        except Exception:
            pass
        energy = None
        try:
            e = s.get(up.BASE + "/v2/activity/growth/energy", timeout=25, verify=False).json()
            energy = (e.get("data") or {}).get("balance")
        except Exception:
            pass

        claimed = sum(1 for t in tasks if t.get("accept_status") == "claimed")
        completed = sum(1 for t in tasks if t.get("accept_status") == "completed")
        accepted = sum(1 for t in tasks if t.get("accept_status") == "accepted")
        not_acc = sum(1 for t in tasks if t.get("accept_status") == "not_accepted")
        in_prog = sum(1 for t in tasks if t.get("accept_status") == "in_progress")

        detail = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            pr = t.get("progress") or {}
            code = t.get("task_code", "")
            detail.append({
                "code": code,
                "name": t.get("title") or up.task_cn(code),
                "status": t.get("accept_status", ""),
                "current": pr.get("current"),
                "target": pr.get("target"),
                "is_new": bool(t.get("is_new")),
            })

        return {
            "idx": idx,
            "total": len(tasks),
            "claimed": claimed,
            "completed": completed,      # ← 已完成但没领！这个数应当一直是 0
            "accepted": accepted,
            "notAccepted": not_acc,
            "inProgress": in_prog,
            "level": prof.get("level"),
            "energy": energy,
            "isNewCount": sum(1 for t in tasks if isinstance(t, dict) and t.get("is_new")),
            "tasks": detail,
        }


BRIDGE = EngineBridge()


# ---------------------------------------------------------------- 运行调度器

class Runner:
    """任务运行器：串行化执行 + 实时日志 + 定时调度。

    - 手动触发与定时触发抢同一把锁 → 同一时刻只有一轮在跑
    - 定时线程每 20 秒醒一次，检查有没有到点的 job（错过超过 grace 秒就不补跑，
      避免服务停了一晚上后开机瞬间补跑一堆）
    """

    TICK_SECONDS = 20
    # 到点后多久内还允许补跑（服务短暂重启不丢任务）；超过就跳过，等下一个周期
    GRACE_SECONDS = 600

    def __init__(self):
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()      # 保证同一时刻只有一轮任务
        self._scheduler: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.running = False
        self.current = ""                      # 当前在跑什么（描述）
        self.started_at: Optional[float] = None
        self.last_result: Optional[Dict[str, Any]] = None
        self.last_error = ""
        self.history: deque = deque(maxlen=50)  # 最近运行记录
        self._fired: Dict[str, str] = {}        # job_id -> "YYYY-MM-DD HH:MM"（去重）
        self.total_runs = 0
        self.auto_runs = 0
        self.manual_runs = 0

    # ---- 状态 ----
    def status(self) -> Dict[str, Any]:
        with self._lock:
            sched = load_schedule()
            return {
                "running": self.running,
                "current": self.current,
                "startedAt": self.started_at,
                "elapsed": (time.time() - self.started_at) if self.started_at else None,
                "lastResult": self.last_result,
                "lastError": self.last_error,
                "totalRuns": self.total_runs,
                "autoRuns": self.auto_runs,
                "manualRuns": self.manual_runs,
                "history": list(self.history),
                "schedule": sched,
                "nextRuns": self.next_runs(sched),
            }

    def next_runs(self, sched: Optional[Dict[str, Any]] = None, count: int = 6) -> List[Dict[str, Any]]:
        """算出接下来 N 个将要触发的计划点（只算未来 7 天内）。"""
        sched = sched or load_schedule()
        if not sched.get("enabled", True):
            return []
        out = []
        base = now_cst()
        for job in sched.get("jobs", []):
            if not job.get("enabled", True):
                continue
            hhmm = (job.get("at") or "").strip()
            if len(hhmm) != 5 or ":" not in hhmm:
                continue
            try:
                hh, mm = int(hhmm[:2]), int(hhmm[3:])
            except ValueError:
                continue
            days = job.get("days") or [0, 1, 2, 3, 4, 5, 6]
            for delta in range(0, 8):
                d = (base + timedelta(days=delta))
                if d.weekday() not in days:
                    continue
                cand = d.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if cand <= base:
                    continue
                out.append({
                    "id": job.get("id"),
                    "name": job.get("name") or job.get("id"),
                    "at": cand.strftime("%Y-%m-%d %H:%M"),
                    "inSeconds": int((cand - base).total_seconds()),
                    "only": job.get("only"),
                })
                break
        out.sort(key=lambda x: x["inSeconds"])
        return out[:count]

    # ---- 执行 ----
    def run_now(self, only: Optional[int] = None, live: Optional[bool] = None,
                by_auto: bool = False, reason: str = "", no_update: Optional[bool] = None) -> Dict[str, Any]:
        """跑一轮。返回结果 dict；已有任务在跑时立刻返回 busy。"""
        if not self._run_lock.acquire(blocking=False):
            return {"ok": False, "reason": "busy", "message": "已有一轮任务正在运行"}

        sched = load_schedule()
        if live is None:
            live = bool(sched.get("live", True))
        if no_update is None:
            no_update = bool(sched.get("no_update", True))

        label = reason or ("定时触发" if by_auto else "手动触发")
        if only:
            label += "（仅账号%d）" % only

        with self._lock:
            self.running = True
            self.current = label
            self.started_at = time.time()
            self.last_error = ""
            self.total_runs += 1
            if by_auto:
                self.auto_runs += 1
            else:
                self.manual_runs += 1

        LOG.emit("run", "▶ 开始运行 —— %s（%s）" % (label, "真实执行" if live else "只读演练"))
        LOG.emit("run", "⏰ 启动时间 %s" % now_cst().strftime("%Y-%m-%d %H:%M:%S"))

        result: Dict[str, Any] = {}
        try:
            mod = BRIDGE.load()
            args = mod.normalize_args(
                live=bool(live),
                only=only,
                no_update=bool(no_update),
            )
            args.live = bool(live)
            args.dry_run = not bool(live)

            def on_line(line: str) -> None:
                # 每行都灌进日志中枢 → SSE 实时推给前端
                LOG.emit("engine", line.rstrip())

            res = mod.run(args, observer=on_line, dry_run_override=not bool(live))
            result = res or {}
            with self._lock:
                self.last_result = {
                    "ok": result.get("ok"),
                    "dryRun": result.get("dry_run"),
                    "blocked": result.get("blocked"),
                    "logfile": result.get("logfile"),
                    "elapsed": result.get("elapsed"),
                    "done": sum(int(s.get("done") or 0) for s in (result.get("summaries") or [])),
                    "total": sum(int(s.get("total") or 0) for s in (result.get("summaries") or [])),
                    "accounts": len(result.get("summaries") or []),
                    "failures": len(result.get("failures") or []),
                }
            LOG.emit("ok", "✅ 运行结束（耗时 %.1f 秒）" % (result.get("elapsed") or 0))
        except Exception as exc:
            import traceback
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            LOG.emit("err", "❌ 运行异常：%s" % self.last_error)
            for ln in traceback.format_exc().splitlines():
                LOG.emit("err", "   " + ln)
            result = {"ok": False, "reason": "exception", "message": self.last_error}
        finally:
            elapsed = time.time() - (self.started_at or time.time())
            with self._lock:
                self.running = False
                self.current = ""
                self.started_at = None
                self.history.appendleft({
                    "at": now_cst().strftime("%Y-%m-%d %H:%M"),
                    "label": label,
                    "live": bool(live),
                    "ok": bool(result.get("ok")),
                    "elapsed": round(elapsed, 1),
                    "done": (self.last_result or {}).get("done"),
                    "total": (self.last_result or {}).get("total"),
                    "error": self.last_error,
                })
            self._run_lock.release()
        result["label"] = label
        return result

    # ---- 调度线程 ----
    def start_scheduler(self) -> None:
        with self._lock:
            if self._scheduler and self._scheduler.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(target=self._loop, name="wb-hub-scheduler", daemon=True)
            self._scheduler = t
            t.start()
        LOG.emit("sys", "🕐 调度线程已启动（每 %d 秒检查一次计划）" % self.TICK_SECONDS)

    def stop_scheduler(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                LOG.emit("err", "调度线程异常（已忽略，继续下一轮）：%s: %s"
                         % (type(exc).__name__, exc))
            # 分片 sleep：停止事件来了能很快退出，不用等满一个 tick
            for _ in range(self.TICK_SECONDS):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def _tick(self) -> None:
        sched = load_schedule()
        if not sched.get("enabled", True):
            return
        now = now_cst()
        for job in sched.get("jobs", []):
            if not job.get("enabled", True):
                continue
            hhmm = (job.get("at") or "").strip()
            if len(hhmm) != 5 or ":" not in hhmm:
                continue
            try:
                hh, mm = int(hhmm[:2]), int(hhmm[3:])
            except ValueError:
                continue
            if now.weekday() not in (job.get("days") or [0, 1, 2, 3, 4, 5, 6]):
                continue
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            delta = (now - target).total_seconds()
            # 到点窗口内才触发（错过太久不补跑，免得开机瞬间补一堆）
            if not (0 <= delta <= self.GRACE_SECONDS):
                continue
            key = "%s %s" % (now.strftime("%Y-%m-%d"), hhmm)
            jid = job.get("id") or hhmm
            if self._fired.get(jid) == key:
                continue
            self._fired[jid] = key
            LOG.emit("sys", "⏰ 计划到点：%s（%s）" % (job.get("name") or jid, hhmm))
            # 在独立线程里跑，别把调度 tick 卡住
            threading.Thread(
                target=self.run_now,
                kwargs={"only": job.get("only"), "by_auto": True,
                        "reason": "定时：%s" % (job.get("name") or jid)},
                name="wb-hub-job-%s" % jid,
                daemon=True,
            ).start()

    def clear_fired_today(self) -> None:
        """清掉「今天已经触发过」的记录，让今天的计划可以再跑一次。

        用途（docstring 里那句）：页面上放个「重跑今天定时」按钮时调它。

        🔴 原来的实现是反的（2026-09-20 审计）：
            `if not self._fired[k].startswith(today): del ...`
        这是在删**非今天**的记录，等于把历史全清掉、偏偏把今天留着 ——
        调用它会**让今天的任务再也不会触发**，与函数名和注释完全相反。
        现在只看注释承诺的语义：删今天，留历史。
        """
        today = now_cst().strftime("%Y-%m-%d")
        for k in list(self._fired):
            if self._fired[k].startswith(today):
                del self._fired[k]

    def fired_today(self) -> List[str]:
        today = now_cst().strftime("%Y-%m-%d")
        return [k for k, v in self._fired.items() if v.startswith(today)]


ENGINE = Runner()


# ---------------------------------------------------------------- 切换账号守望者

class Watcher:
    """盯着桌面端「此刻登录的是谁」—— uid 一变，就对刚切过去的账号单独补跑一轮。

    为什么值得单独补跑：刚切过去的账号拥有**真实桌面会话**，那一刻跑它成功率最高。
    这是原本 wb-task-hub/watch.py 的能力，现在搬进服务里常驻，不再需要单独双击 bat。

    安全约束（硬性的，别改）：
      · **绝不写桌面端认证文件**，只读；
      · **绝不切换账号** —— 只观察，切换永远由用户/wb-switch 决定；
      · 认不出 uid 是哪个账号 → 什么都不做（**不猜一个账号去顶**）；
      · 同一账号跑过之后有冷却（min_gap_minutes），防来回抖动被反复触发；
      · 已有任务在跑时这一轮跳过，避免和全量运行抢同一个账号；
      · 单实例：本服务内只有一个守望线程。
    """

    TICK_SECONDS = 5

    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.enabled = True
        self.uid: Optional[str] = None            # 上一次观测到的 uid
        self.last_action = "idle"
        self.last_detail = ""
        self.last_seen: Optional[float] = None    # 上次看到某 uid 的时间
        self.runs: Dict[str, float] = {}          # uid -> 上次补跑时间
        self.false_alarms = 0                     # 轮询异常计数（守望者绝不能因一次异常死掉）
        self._debounce_until: Optional[float] = None   # uid 变化后的稳定等待截止
        self._pending_uid: Optional[str] = None
        self._last_tick_at: Optional[float] = None     # 上次轮询时刻（给 nextPoll 倒计时用）

    # ---- 配置 ----
    @staticmethod
    def cfg() -> Dict[str, Any]:
        sched = load_schedule()
        w = sched.get("watch") or {}
        return {
            "enabled": bool(w.get("enabled", True)),
            "poll_seconds": float(w.get("poll_seconds") or 5),
            "debounce_seconds": float(w.get("debounce_seconds") or 30),
            "min_gap_minutes": float(w.get("min_gap_minutes") if w.get("min_gap_minutes") is not None else 120),
            "run_on_start": bool(w.get("run_on_start", False)),
        }

    # ---- 状态 ----
    def status(self) -> Dict[str, Any]:
        with self._lock:
            c = self.cfg()
            uid_now, src = "", ""
            try:
                mod = BRIDGE.load()
                uid_now, src = mod.read_desktop_uid()
            except Exception as exc:  # noqa: BLE001
                src = "%s: %s" % (type(exc).__name__, exc)
            return {
                "enabled": bool(c["enabled"] and self.enabled),
                "alive": self.is_alive(),
                "uid": self.uid,
                "desktopUid": uid_now,
                "desktopUidShort": (uid_now or "")[:8],
                "uidSource": src,
                "lastAction": self.last_action,
                "lastDetail": self.last_detail,
                "lastSeen": self.last_seen,
                "runsCount": len(self.runs),
                "falseAlarms": self.false_alarms,
                "cfg": c,
                "nextPoll": self.next_poll_at(),
            }

    def next_poll_at(self) -> Optional[str]:
        """下一次轮询的预计时间。

        ⚠️ 轮询线程是「醒来就干活」，没有精确的下一次唤醒时刻可报，所以这里报的是
        **距下次轮询还剩几秒**（用配置的 poll_seconds 减去离上次 tick 的时间）。
        原来直接返回当前时间（`now_cst().strftime(...)`）——那永远是「此刻」，
        页面拿去做倒计时只会一直显示 0，属于误导，已改掉。
        """
        with self._lock:
            last = self._last_tick_at
            interval = max(1.0, float(self.cfg().get("poll_seconds") or 5))
        if not last:
            return None
        remain = interval - (time.time() - last)
        return "%.0f" % max(0.0, remain)

    def _say(self, detail: str, action: str = "idle", level: str = "sys") -> None:
        with self._lock:
            self.last_action = action
            self.last_detail = detail
        if detail:
            LOG.emit(level, "🛰️ " + detail)

    # ---- 判定（照搬 watch.py 的纯函数语义，方便逐条对照） ----
    @staticmethod
    def evaluate(prev_uid, cur_uid, last_run_at, now, cfg) -> tuple:
        if not cur_uid:
            return "no-uid", "读不到桌面端 uid"
        if prev_uid is None:
            if cfg.get("run_on_start"):
                return "run", "启动时当前账号（run_on_start=true）"
            return "observe", "首次观测，只记录不跑"
        if prev_uid == cur_uid:
            return "idle", ""
        gap = float(cfg.get("min_gap_minutes") or 0) * 60
        if gap > 0 and last_run_at and (now - last_run_at) < gap:
            wait = int((gap - (now - last_run_at)) / 60) + 1
            return "cooldown", "该账号刚跑过（约 %d 分钟前），冷却还剩 %d 分钟" % (
                int((now - last_run_at) / 60), wait)
        return "run", "桌面端已切换到新账号"

    # ---- 轮询 ----
    def _tick(self) -> None:
        c = self.cfg()
        if not c["enabled"]:
            return
        with self._lock:
            self._last_tick_at = time.time()
        mod = BRIDGE.load()
        uid, src = mod.read_desktop_uid()
        now = time.time()
        prev = self.uid

        # uid 变了先等稳定：切换过程中认证文件可能被写好几次，直接触发会重复补跑
        if uid != (prev or None):
            if uid != self._pending_uid:
                self._pending_uid = uid
                self._debounce_until = now + c["debounce_seconds"]
                LOG.emit("sys", "👀 检测到桌面账号变化 → %s…，等 %.0f 秒稳定后再判定"
                         % ((uid or "?")[:8], c["debounce_seconds"]))
                return
            if self._debounce_until and now < self._debounce_until:
                return
        else:
            self._pending_uid = None
            self._debounce_until = None

        action, detail = self.evaluate(prev, uid, self.runs.get(uid or "", 0), now, c)

        if action == "idle":
            with self._lock:
                self.uid = uid or None
                self.last_seen = now
                self.last_action = "idle"
            return
        if action == "no-uid":
            self.false_alarms += 0          # 读不到不算异常，是正常可预期的状态
            self._say("⚠️ %s（来源：%s）" % (detail, src), "no-uid")
            return
        if action in ("observe", "cooldown"):
            with self._lock:
                self.uid = uid or None
                self.last_seen = now
            self._say(("👀 " if action == "observe" else "⏳ ") + detail,
                      action, "warn" if action == "cooldown" else "sys")
            return

        # action == "run"
        try:
            accounts, _ = mod.read_switch_accounts(mod.load_config())
            idx, why = mod.find_desktop_account(accounts)
        except Exception as exc:  # noqa: BLE001
            self._say("⚠️ 判定账号失败（已忽略）：%s: %s" % (type(exc).__name__, exc), "run", "err")
            return

        if not idx:
            with self._lock:
                self.uid = uid or None
            self._say("⚠️ 桌面端切到了认不出的账号（%s），本次不跑 —— 不猜账号" % why,
                      "unknown-account", "warn")
            return

        if ENGINE.running:
            self._say("⏸️ %s，但已有任务在跑，这一轮让路（下轮再试）" % detail, "engine-busy", "warn")
            self._pending_uid = None
            self._debounce_until = None
            return

        with self._lock:
            self.uid = uid or None
            self.runs[uid or ""] = now
        self._say("🔄 %s → 补跑账号%d %s" % (detail, idx, why.split("（")[0]), "run", "ok")

        # 在独立线程里跑，别把守望线程卡住
        threading.Thread(
            target=ENGINE.run_now,
            kwargs={"only": idx, "by_auto": False,
                    "reason": "守望者：切换到账号%d" % idx},
            name="wb-hub-watch-run",
            daemon=True,
        ).start()
        self._pending_uid = None
        self._debounce_until = None

    # ---- 线程管理 ----
    def start(self) -> None:
        with self._lock:
            self.enabled = True
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._last_tick_at = None      # 重新起线程 → 清掉上一轮的 tick 时刻
            t = threading.Thread(target=self._loop, name="wb-hub-watcher", daemon=True)
            self._thread = t
            t.start()
        c = self.cfg()
        LOG.emit("sys", "🛰️ 守望者已启动（轮询 %.0fs｜稳定 %.0fs｜冷却 %s 分钟）"
                 % (c["poll_seconds"], c["debounce_seconds"], c["min_gap_minutes"]))

    def stop(self) -> None:
        with self._lock:
            self.enabled = False
            self._stop.set()
        LOG.emit("sys", "🛰️ 守望者已停止（切账号不再自动补跑）")

    def is_alive(self) -> bool:
        t = self._thread
        return bool(t and t.is_alive() and not self._stop.is_set())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:  # noqa: BLE001
                # 守望者绝不能因为一次异常就死掉
                self.false_alarms += 1
                LOG.emit("err", "守望轮询异常（已忽略，继续）：%s: %s" % (type(exc).__name__, exc))
            c = self.cfg()
            step = max(1.0, float(c["poll_seconds"]))
            for _ in range(int(step)):
                if self._stop.is_set():
                    break
                time.sleep(1)

    def check_now(self) -> Dict[str, Any]:
        """手动立刻判定一次（页面上的「立即检查」）。"""
        try:
            self._tick()
            return {"ok": True, **self.status()}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}


WATCH = Watcher()


# ---------------------------------------------------------------- FastAPI

app = FastAPI(title="wb-hub", version=SERVICE_VERSION)


def ok(data: Any = None, **extra: Any) -> JSONResponse:
    payload = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return JSONResponse(payload)


def fail(msg: str, code: int = 500, **extra: Any) -> JSONResponse:
    payload = {"ok": False, "error": msg}
    payload.update(extra)
    return JSONResponse(payload, status_code=code)


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    import traceback
    traceback.print_exc()
    return fail("%s: %s" % (type(exc).__name__, exc), 500)


@app.get("/api/health")
async def health():
    return ok({
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        # 报「启动时快照」而不是现算：现算的话它永远等于 launch.py 算的值，比不出新旧。
        "code": CODE_FINGERPRINT_AT_START,
        "pid": os.getpid(),
        "time": now_cst().strftime("%Y-%m-%d %H:%M:%S"),
        "engineAvailable": BRIDGE.available(),
        "engineError": BRIDGE.error,
    })


@app.get("/api/status")
async def status():
    return ok(ENGINE.status())


@app.get("/api/accounts")
async def accounts():
    """账号列表 + 每个账号的任务完成情况（只读 GET，逐个查完再返回）。

    include=1 时才去查任务明细（要逐个联网，慢）；否则只给账号清单。
    """
    try:
        accs, skipped = BRIDGE.accounts()
    except Exception as exc:
        return fail("读账号库失败：%s: %s" % (type(exc).__name__, exc))

    out = []
    for i, a in enumerate(accs, 1):
        exp = a.get("exp") or 0
        days_left = round((exp - time.time()) / 86400, 1) if exp else None
        out.append({
            "idx": i,
            "note": a.get("note"),
            "nick": a.get("nick"),
            "domain": a.get("domain"),
            "variant": a.get("variant"),
            # uid 给前端匹配「桌面端此刻登录的是哪个账号」用，**要完整的**（截断就没法比）
            "uid": a.get("uid") or "",
            "uidShort": (a.get("uid") or "")[:12],
            "daysLeft": days_left,
            "expired": (days_left is not None and days_left <= 0),
        })
    return ok({"accounts": out, "skipped": skipped, "count": len(out)})


@app.get("/api/accounts/{idx}/status")
async def account_status(idx: int):
    """单个账号的任务明细。"""
    try:
        accs, _ = BRIDGE.accounts()
    except Exception as exc:
        return fail("读账号库失败：%s" % type(exc).__name__)
    if not 1 <= idx <= len(accs):
        return fail("账号序号 %d 超出范围（共 %d 个）" % (idx, len(accs)), 400)
    a = accs[idx - 1]
    try:
        detail = await asyncio.to_thread(BRIDGE.account_status, idx, a["at"])
    except Exception as exc:
        return fail("查询账号%d 失败：%s: %s" % (idx, type(exc).__name__, exc))
    detail["note"] = a.get("note")
    detail["nick"] = a.get("nick")
    detail["domain"] = a.get("domain")
    return ok(detail)


@app.post("/api/switch/{idx}")
async def switch_account(idx: int):
    """切换到指定账号 —— 调 workbuddy-switch 自己的 57890 接口，不自己实现切换逻辑。"""
    import urllib.request

    try:
        accs, _ = BRIDGE.accounts()
    except Exception as exc:
        return fail("读账号库失败：%s" % type(exc).__name__)
    if not 1 <= idx <= len(accs):
        return fail("账号序号 %d 超出范围" % idx, 400)

    # wb-switch 的 /api/switch 需要账号 id（uuid），不是序号
    raw = json.loads((Path(os.path.expanduser("~")) / ".wb-switch" / "accounts.json")
                     .read_text(encoding="utf-8"))
    uid = accs[idx - 1].get("uid")
    target = next((x for x in raw if isinstance(x, dict)
                   and (x.get("uid") or "").strip() == uid), None)
    if target is None:
        return fail("在账号库里找不到账号%d 对应的条目" % idx, 404)

    body = json.dumps({"id": target.get("id")}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:57890/api/switch", data=body, method="POST",
        headers={"Content-Type": "application/json"})

    # 🔴 必须丢进线程跑：wb-switch 切账号是慢动作（要重启客户端），
    # timeout 给到 60 秒；直接在 async 路由里同步调用会把事件循环卡死 60 秒，
    # SSE 日志流断推、页面所有请求排队 → 看着像服务挂了。
    def _do_switch() -> str:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=60) as resp:
            return resp.read().decode("utf-8", "replace")

    try:
        text = await asyncio.to_thread(_do_switch)
        LOG.emit("sys", "🔄 已请求 wb-switch 切到账号%d %s" % (idx, accs[idx - 1].get("nick")))
        return ok({"switched": idx, "upstream": text[:400]})
    except Exception as exc:
        return fail("调用 wb-switch 切换接口失败：%s: %s" % (type(exc).__name__, exc), 502)


@app.post("/api/run")
async def run_now(request: Request):
    """手动触发一轮。body: {only?, live?}"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    only = body.get("only")
    if only in ("", None):
        only = None
    else:
        try:
            only = int(only)
        except (TypeError, ValueError):
            return fail("only 必须是整数", 400)
        if only < 1:
            return fail("only 必须 >= 1", 400)

    live = body.get("live")
    if live is not None:
        live = bool(live)

    # 真实执行是危险动作，要求显式确认（前端会弹确认框）
    if live is True and not body.get("confirm"):
        return fail("真实执行需要 confirm=true（前端应弹确认框）", 400)

    if ENGINE.running:
        return fail("已有一轮任务正在运行，请等它结束", 409)

    # 不在这里拼「（仅账号N）」—— run_now 会按 only 自己补上，重复拼会变成「（仅账号1）（仅账号1）」
    threading.Thread(
        target=ENGINE.run_now,
        kwargs={"only": only, "live": live, "by_auto": False, "reason": "手动触发"},
        name="wb-hub-manual", daemon=True,
    ).start()
    return ok({"started": True, "only": only, "live": live})


@app.get("/api/logs")
async def logs(since: int = 0, limit: int = 500):
    """增量拉日志（前端轮询兜底；实时走 /api/logs/stream）。"""
    data = LOG.since(since)
    items = data["items"]
    if limit and len(items) > limit:
        items = items[-limit:]
    return ok({"seq": data["seq"], "items": items})


@app.get("/api/logs/stream")
async def logs_stream(request: Request, since: int = 0):
    """SSE 实时日志流。"""
    queue = LOG.subscribe()

    async def gen():
        try:
            # 先把历史补上，避免刚打开页面看不到已有输出
            hist = LOG.since(since)
            for item in hist["items"][-200:]:
                yield "data: %s\n\n" % json.dumps(item, ensure_ascii=False)
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield "data: %s\n\n" % json.dumps(item, ensure_ascii=False)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"      # 心跳，防中间层掐连接
        except asyncio.CancelledError:
            pass
        finally:
            LOG.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })


@app.get("/api/schedule")
async def get_schedule():
    return ok({"schedule": load_schedule(), "nextRuns": ENGINE.next_runs(),
               "firedToday": ENGINE.fired_today()})


@app.post("/api/schedule")
async def post_schedule(request: Request):
    """保存调度配置。body 里给哪些键就改哪些键（合并式）。"""
    try:
        body = await request.json()
    except Exception:
        return fail("请求体不是合法 JSON", 400)
    try:
        cur = save_schedule(body)
    except Exception as exc:
        return fail("保存失败：%s: %s" % (type(exc).__name__, exc))
    LOG.emit("sys", "⚙️ 调度配置已更新（enabled=%s live=%s jobs=%d）"
             % (cur.get("enabled"), cur.get("live"), len(cur.get("jobs") or [])))
    return ok({"schedule": cur, "nextRuns": ENGINE.next_runs(cur)})


@app.post("/api/schedule/reset")
async def reset_schedule():
    atomic_write_json(SCHEDULE_PATH, json.loads(json.dumps(DEFAULT_SCHEDULE)))
    LOG.emit("sys", "♻️ 调度配置已恢复默认")
    return ok({"schedule": load_schedule(), "nextRuns": ENGINE.next_runs()})


@app.post("/api/logs/clear")
async def clear_logs():
    LOG.clear()
    return ok()


@app.get("/api/watch")
async def watch_status():
    # status() 里要读桌面端 uid（磁盘 I/O），而且页面会周期轮询 —— 丢线程别卡事件循环。
    return ok(await asyncio.to_thread(WATCH.status))


@app.post("/api/watch")
async def watch_toggle(request: Request):
    """开关守望者。body: {enabled: bool}"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    want = body.get("enabled")
    if want is None:
        return fail("请给 enabled（true/false）", 400)
    if bool(want):
        WATCH.start()
    else:
        WATCH.stop()
    # 同步写进配置，重启后保持（合并式保存，不动 jobs）
    save_schedule({"watch": {"enabled": bool(want)}})
    return ok(await asyncio.to_thread(WATCH.status))


@app.post("/api/watch/check")
async def watch_check():
    """立刻判定一次（不等轮询）。

    🔴 必须丢进线程跑：check_now → _tick 里有磁盘 I/O（读桌面端 uid），
    并且在判定为「该补跑」时会**直接触发一轮任务**（可能跑几分钟）。
    直接在 async 路由里同步调用会把整个事件循环卡死 —— SSE 日志流断推、
    其他请求全部排队，页面看着就像死了。对比 account_status 也是这么处理的。
    """
    return ok(await asyncio.to_thread(WATCH.check_now))


@app.post("/api/watch/config")
async def watch_config(request: Request):
    """改守望者参数（poll/debounce/冷却/run_on_start）。"""
    try:
        body = await request.json()
    except Exception:
        return fail("请求体不是合法 JSON", 400)
    allowed = ("enabled", "poll_seconds", "debounce_seconds", "min_gap_minutes", "run_on_start")
    patch = {k: body[k] for k in allowed if k in body}
    if not patch:
        return fail("没有可改的字段（可用：%s）" % ", ".join(allowed), 400)
    save_schedule({"watch": patch})
    LOG.emit("sys", "⚙️ 守望者参数已更新：%s" % json.dumps(patch, ensure_ascii=False))
    return ok(await asyncio.to_thread(WATCH.status))


@app.get("/")
async def index():
    f = STATIC_DIR / "index.html"
    if not f.exists():
        return fail("前端文件缺失：%s" % f, 404)
    return FileResponse(f, media_type="text/html")


# ---------------------------------------------------------------- 启动

def bootstrap() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not SCHEDULE_PATH.exists():
        atomic_write_json(SCHEDULE_PATH, json.loads(json.dumps(DEFAULT_SCHEDULE)))

    LOG.emit("sys", "🚀 %s v%s 启动（pid=%d）" % (SERVICE_NAME, SERVICE_VERSION, os.getpid()))
    LOG.emit("sys", "📂 引擎目录：%s" % HUB_DIR)
    if BRIDGE.available():
        LOG.emit("sys", "✅ 引擎加载成功（上游 sha256=%s）" % BRIDGE.load().upstream_sha())
    else:
        LOG.emit("err", "❌ 引擎加载失败：%s" % BRIDGE.error)
    LOG.emit("sys", "🕐 定时调度**由本服务自己负责**，不依赖 WorkBuddy 客户端")

    sched = load_schedule()
    for job in sched.get("jobs", []):
        LOG.emit("sys", "   计划：%s @ %s（%s）"
                 % (job.get("name"), job.get("at"),
                    "启用" if job.get("enabled", True) else "停用"))
    ENGINE.start_scheduler()

    # 守望者：切账号自动补跑。配置里关掉就不起。
    if (sched.get("watch") or {}).get("enabled", True):
        WATCH.start()
    else:
        LOG.emit("sys", "🛰️ 守望者未启动（配置里 watch.enabled = false）")


if __name__ == "__main__":
    # 🔴 bootstrap() **必须留在这个守卫里**。放到模块级的话，任何 `import server`
    # 都会顺带起一个调度线程 —— 将来若有脚本/工具 import 它，就会出现
    # 「第二个调度器同时到点触发任务」的真实风险（2026-09-20 被发现并修掉）。
    # launch.py 是用 `pythonw server.py` 拉起的 → `__main__` 成立，行为完全不变。
    bootstrap()

    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
