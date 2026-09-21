#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wb-task-hub 切换账号守望者

用户 2026-09-20 要求：**保留 desktop_scope=all（每次都全量跑），另外在账号被切换时，
自动再补跑一次「刚切换过去的那个账号」**。

原理：桌面端「此刻登录的是谁」唯一的真相在桌面端认证文件的 `account.uid` 里
（wb-switch 切换账号时写的就是它）。所以这个进程只做一件事：
    盯着 uid —— 一变就等它稳定 —— 然后把 engine 对那个账号单独跑一遍。

为什么值得单独补跑：
  - 刚切过去的账号拥有**真实桌面会话**，那一刻跑它成功率最高；
  - 时效性好，不必等下一轮全量。

安全约束（硬性的，别改）：
  - **绝不写桌面端认证文件**，只读；
  - **绝不切换账号** —— 守望者只观察，切换永远由你/wb-switch 决定；
  - 认不出 uid 是哪个账号 → 什么都不做（不猜一个账号去顶）；
  - 同一账号跑过之后有冷却（min_gap_minutes），防止来回抖动被反复触发；
  - 已有 engine 在跑时这一轮跳过，避免和全量运行抢同一个账号；
  - 单实例锁：已在跑就不再起第二个。

用法：
    python watch.py            # 前台跑（能看日志）
    python watch.py --once     # 只轮询一次就退出（自检/排错用）
    python watch.py --dry      # 只判定不真跑（看它会不会触发）
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import engine as E

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
STATE_PATH = LOG_DIR / "watch-state.json"
LOCK_PATH = LOG_DIR / "watch.lock"
ENGINE = ROOT / "engine.py"


# ---------------------------------------------------------------- 基础设施

def atomic_write_json(path, obj):
    """临时文件 + os.replace —— 被别的进程读到半个文件的成本太高。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_state():
    """读状态文件。

    读失败**不当作「没有状态」**（那样会把冷却记录清空、导致重复补跑）：
    先把它改名保留成 .corrupt-<时间>，再从头开始，并把这件事喊出来。
    """
    if not STATE_PATH.exists():
        return {"uid": None, "runs": {}}
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        keep = STATE_PATH.with_name("watch-state.corrupt-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(STATE_PATH, keep)
        except Exception:
            pass
        log("⚠️ 状态文件读不出来（%s: %s），已保留为 %s，从头开始"
            % (type(exc).__name__, exc, keep.name))
        return {"uid": None, "runs": {}}
    if not isinstance(data, dict):
        return {"uid": None, "runs": {}}
    data.setdefault("uid", None)
    if not isinstance(data.get("runs"), dict):
        data["runs"] = {}
    return data


def _decode(raw):
    """Windows 命令行工具的输出编码不定 —— 中文系统上 tasklist 是 GBK(936)，不是 UTF-8。

    实测踩过：直接 `subprocess.run(..., text=True)` 会在读取线程里 UnicodeDecodeError，
    而那个异常不影响 returncode，只会让 `.stdout` 变成 None → 后面一用就 TypeError。
    """
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("utf-8", "replace")


def pid_alive(pid):
    """用 tasklist 查 PID 是否还活着（Windows 上没有安全的 os.kill(pid,0)）。"""
    try:
        p = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                           capture_output=True, timeout=20)
    except Exception:
        return True  # 查不了就当他活着：宁可少跑一轮，也不要重复跑
    out = _decode((p.stdout or b"") + (p.stderr or b""))
    # PID 列本身不带千位分隔符，但保险起见先去掉逗号再匹配
    return str(pid) in out.replace(",", "")


def engine_running():
    """是否已有 engine 在跑（读它自己写的 PID 标记；残留会自动清掉）。"""
    if not E.RUN_MARKER.exists():
        return False
    try:
        pid = int(E.RUN_MARKER.read_text(encoding="utf-8").strip())
    except Exception:
        return False
    if pid_alive(pid):
        return True
    try:
        E.RUN_MARKER.unlink()
    except Exception:
        pass
    return False


def acquire_lock():
    """单实例锁：返回 True 表示拿到锁；False 表示已经有一个在跑。"""
    if LOCK_PATH.exists():
        try:
            other = int(LOCK_PATH.read_text(encoding="utf-8").strip())
        except Exception:
            other = None
        if other and other != os.getpid() and pid_alive(other):
            return False
        log("清理掉一个残留的锁（PID %s 已不在）" % other)
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock():
    try:
        if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_PATH.unlink()
    except Exception:
        pass


_logfile = None


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    if _logfile:
        try:
            _logfile.write(line + "\n")
            _logfile.flush()
        except Exception:
            pass


# ---------------------------------------------------------------- 判定（纯函数，可自检）

def evaluate(prev_uid, cur_uid, last_run_at, now, cfg):
    """一次轮询的决策。纯函数 —— 不碰文件、不跑进程，方便自检。

    返回 (action, detail)，action ∈：
      no-uid   读不到桌面端 uid
      observe  首次观测，只记录不跑（除非 run_on_start）
      run      该补跑这个账号
      cooldown 变了但这个账号刚跑过，跳过
      idle     没变化
    """
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


# ---------------------------------------------------------------- 动作

def run_account(idx, nick):
    """对单个账号跑一次真实执行（独立进程：异常隔离 + 独立日志）。"""
    cmd = [sys.executable, str(ENGINE), "--live", "--only", str(idx), "--no-update"]
    log("▶ 补跑账号%d %s —— 启动 %s" % (idx, nick, " ".join(cmd)))
    try:
        rc = subprocess.call(cmd, cwd=str(ROOT))
    except Exception as exc:
        log("❌ 补跑启动失败：%s: %s" % (type(exc).__name__, exc))
        return -1
    log("补跑结束，退出码 %d" % rc)
    return rc


def poll_once(state, cfg):
    """一次判定（uid 已稳定时调用）。"""
    uid, src = E.read_desktop_uid()
    now = time.time()
    action, detail = evaluate(state.get("uid"), uid, state["runs"].get(uid or "", 0), now, cfg)

    if action in ("idle", "no-uid"):
        if action == "no-uid":
            log("⚠️ %s（来源判定：%s）" % (detail, src))
        return state, uid, None, detail

    if action == "observe":
        log("👀 %s" % detail)
        state["uid"] = uid
        atomic_write_json(STATE_PATH, state)
        return state, uid, None, detail

    if action == "cooldown":
        log("⏳ %s" % detail)
        state["uid"] = uid
        atomic_write_json(STATE_PATH, state)
        return state, uid, None, detail

    # action == "run"
    accounts, _skipped = E.read_switch_accounts(cfg)
    idx, why = E.find_desktop_account(accounts)
    if not idx:
        log("⚠️ 桌面端切到了认不出的账号（%s），本次不跑 —— 不猜账号" % why)
        state["uid"] = uid
        atomic_write_json(STATE_PATH, state)
        return state, uid, None, "unknown-account"

    if engine_running():
        log("⏸️ %s，但已有 engine 在跑，这一轮让路（下轮再试）" % detail)
        return state, state.get("uid"), None, "engine-busy"

    log("🔄 %s → 补跑账号%d %s" % (detail, idx, why.split("（")[0]))
    state["uid"] = uid
    state["runs"][uid] = now
    atomic_write_json(STATE_PATH, state)
    return state, uid, idx, detail


def service_watcher_active(timeout=3.0):
    """wb-hub 常驻服务里已经内置了守望者 —— 它在跑的话本进程必须让路。

    🔴 为什么必须让路：两套守望者同时补跑同一个账号，会各跑一遍全量任务
    （engine 之间**没有互斥锁**，`engine.running` 只是"有任务在跑"的提示标记，
    `watch.py` 自己的 `watch.lock` 也只管得住自己这一类进程）。重复跑除了浪费时间，
    还可能触发服务端风控。

    判定失败的**唯一安全方向**是「当作服务版在跑 → 本进程退出」：
    宁可这里不跑（用户还有服务版兜着），也不要赌两个一起跑。
    确实需要独立跑（比如服务没起来、要单独调试）时用 `--force` 显式覆盖。
    """
    try:
        import urllib.request
        # 本机地址必须绕开系统代理，否则会被拦成 502
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with op.open("http://127.0.0.1:8793/api/watch", timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False          # 服务没起 / 接口不可用 → 允许独立跑
    d = data.get("data") if isinstance(data, dict) else None
    if not isinstance(d, dict):
        return False
    return bool(d.get("enabled")) and bool(d.get("alive"))


def main():
    global _logfile
    ap = argparse.ArgumentParser(description="WorkBuddy 账号切换守望者")
    ap.add_argument("--once", action="store_true", help="只轮询一次就退出")
    ap.add_argument("--dry", action="store_true", help="只判定，不真的跑 engine")
    ap.add_argument("--force", action="store_true",
                    help="即使 wb-hub 服务里的守望者在跑也照常启动（默认让路）")
    args = ap.parse_args()

    cfg_full = E.load_config()
    cfg = (cfg_full.get("watch") or {})
    if not cfg.get("enabled", True) and not args.once:
        print("watch.enabled = false（配置里关掉了），退出")
        return 0

    # 让路：服务版守望者在跑就别再起一个（2026-09-20 起守望者已内置进 wb-hub）
    if not args.once and not args.force and service_watcher_active():
        print("wb-hub 服务里已有守望者在运行（重启/停止它请到 http://127.0.0.1:8793）")
        print("确实要再单独跑一个，请加 --force")
        return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _logfile = open(LOG_DIR / ("watch-%s.log" % time.strftime("%Y%m%d")), "a", encoding="utf-8")

    if not args.once and not acquire_lock():
        log("已有守望者在跑，本进程退出")
        return 0

    poll = float(cfg.get("poll_seconds") or 5)
    debounce = float(cfg.get("debounce_seconds") or 30)
    cur_uid, src = E.read_desktop_uid()

    log("=" * 60)
    log("🛰️ 守望者启动（PID %d）｜轮询 %.0fs｜稳定 %.0fs｜冷却 %s 分钟%s"
        % (os.getpid(), poll, debounce, cfg.get("min_gap_minutes", 120),
           "｜DRY（不真跑）" if args.dry else ""))
    log("当前桌面端 uid=%s…（%s）" % ((cur_uid or "?")[:8], src))
    log("=" * 60)

    try:
        state = load_state()

        if args.once:
            state, _acted, idx, _detail = poll_once(state, cfg)
            if idx:
                if args.dry:
                    log("🧪 DRY：本应补跑账号%d，已跳过" % idx)
                else:
                    run_account(idx, "见上方日志")
            return 0

        # pending 机制：uid 一变先记下来，连续 debounce 秒不再变才动作 ——
        # 切换过程中认证文件可能被写好几次，直接触发会重复补跑。
        pending_uid, pending_since = None, 0.0
        while True:
            try:
                uid, _src = E.read_desktop_uid()
                now = time.time()
                if uid != (state.get("uid") or None):
                    if uid != pending_uid:
                        pending_uid, pending_since = uid, now
                        log("👀 检测到桌面账号变化 → %s…，等 %.0fs 稳定后再判定"
                            % ((uid or "?")[:8], debounce))
                    elif now - pending_since >= debounce:
                        state, _acted, idx, _detail = poll_once(state, cfg)
                        pending_uid = None
                        if idx:
                            if args.dry:
                                log("🧪 DRY：本应补跑账号%d，已跳过" % idx)
                            else:
                                run_account(idx, "见上方日志")
                else:
                    pending_uid = None
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # 守望者绝不能因为一次异常就死掉
                log("❌ 轮询异常（已忽略，继续）：%s: %s" % (type(exc).__name__, exc))
            time.sleep(poll)
    except KeyboardInterrupt:
        log("收到中断，守望者退出")
    finally:
        release_lock()
        if _logfile:
            _logfile.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
