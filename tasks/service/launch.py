"""
wb-hub（WorkBuddy 任务后台托管服务）—— 启动/停止器

为什么需要它：
  直接跑 ``python server.py`` 会一直挂着一个控制台窗口，而且**随终端一起死**。
  本脚本用 ``pythonw.exe`` 把服务以「完全脱离、无窗口」的方式拉起来，控制台随即退出。
  这样即使关掉 WorkBuddy 客户端、关掉所有终端，服务与它的定时任务仍在跑。

设计要点（针对已知事故逐条加固）：
  · 启动前先探测端口。若已被**本服务**占用 → 直接提示已在运行；
    若被**别的程序**占用 → 拒绝启动并说明，绝不硬抢端口（撞端口事故的教训）。
  · 启动后访问 /api/health 确认服务真的起来了且身份正确，失败则打印日志尾部。
  · 记录 PID 到 _private/server.pid 供 stop 精确终止。
    🔴 PID 文件是「运行中实例的状态文件」，不是缓存 —— 任何情况下都不许顺手删。
  · 停止时**只** taskkill 单个 PID，且 kill 前确认它确实是 python 进程。
    🔴 严禁 ``taskkill /IM pythonw.exe /F`` 这类按映像名杀 ——
      会把同机上其他同样用 pythonw 起的不相关服务**一起杀掉**，而且它们未必会自动重启。

用法：
  python launch.py start    启动（无窗口）
  python launch.py stop     停止
  python launch.py restart  重启
  python launch.py status   查看状态
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = BASE_DIR / "_private"
PID_FILE = PRIVATE_DIR / "server.pid"
LOG_FILE = PRIVATE_DIR / "server.log"
def find_pythonw() -> Path:
    """定位 pythonw.exe（无窗口解释器）。

    刻意**不写死路径** —— 别人的 Python 装在哪、有没有用虚拟环境，我们无从得知。
    Windows 上用 pythonw.exe 是为了「无控制台窗口」；找不到时退化成 python，
    功能一样，只是会挂一个窗口。
    优先级：环境变量 WB_HUB_PYTHONW > 仓库内 .venv > 当前解释器同目录 > PATH。
    """
    cands = []
    env = os.environ.get("WB_HUB_PYTHONW")
    if env:
        cands.append(Path(env))
    repo = BASE_DIR.parent.parent            # tasks/service → 仓库根
    cands += [repo / ".venv" / "Scripts" / "pythonw.exe",
              repo / ".venv" / "bin" / "pythonw",
              repo / ".venv" / "Scripts" / "python.exe"]
    for exe in ("pythonw.exe", "pythonw", "python.exe", "python"):
        try:
            cands.append(Path(sys.executable).with_name(exe))
        except Exception:  # noqa: BLE001
            pass
    for name in ("pythonw", "pythonw.exe", "python3", "python"):
        w = shutil.which(name)
        if w:
            cands.append(Path(w))
    for c in cands:
        try:
            if c and c.exists():
                return c
        except OSError:
            continue
    return Path("pythonw")                   # 交给 PATH 兜底


PYW = find_pythonw()

PORT_FALLBACK = 8793
APP_TAG = "wb-hub"

# 让中文在 chcp 65001 的控制台里正常输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def read_int_const(name: str, default: int) -> int:
    """从 server.py 里读出常量，避免两处写死不一致。"""
    try:
        for line in (BASE_DIR / "server.py").read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            left = s.split("=", 1)[0].strip()
            if left == name:
                return int(s.split("=", 1)[1].split("#")[0].strip())
    except Exception:  # noqa: BLE001
        pass
    return default


def read_bind_host() -> str:
    """从 server.py 里读出 HOST。

    注意别误取 HOST_XXX 之类的名字 —— 只看左边完全没有下划线的那一行。
    """
    try:
        for line in (BASE_DIR / "server.py").read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            left = s.split("=", 1)[0].strip()
            if left == "HOST":
                return s.split("=", 1)[1].split("#")[0].strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return "127.0.0.1"


PORT = read_int_const("PORT", PORT_FALLBACK)
BIND_HOST = read_bind_host()
HEALTH_URL = "http://127.0.0.1:%d/api/health" % PORT


def lan_url() -> str:
    """监听所有网卡时，返回局域网访问地址；否则返回空串。"""
    if BIND_HOST != "0.0.0.0":
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))     # 只为选路，不发包
            ip = s.getsockname()[0]
    except Exception:  # noqa: BLE001
        return ""
    return "http://%s:%d" % (ip, PORT)


# ---------------------------------------------------------------- 探测

def port_open(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def probe_health(timeout: float = 2.0) -> dict:
    """访问 /api/health。返回 {'ok':bool,'ours':bool,'raw':str,'data':dict}。"""
    req = urllib.request.Request(HEALTH_URL, headers={"Accept": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return {"ok": False, "ours": False, "raw": "HTTP %s" % e.code, "data": {}}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "ours": False, "raw": str(e), "data": {}}
    try:
        j = json.loads(body)
    except ValueError:
        return {"ok": False, "ours": False, "raw": body[:80], "data": {}}
    # 兼容两种形态：{"ok":true,"data":{...}} 或扁平 {"ok":true,"service":...}
    d = j.get("data") if isinstance(j.get("data"), dict) else j
    ours = (d.get("service") == APP_TAG) or (d.get("app") == APP_TAG)
    return {"ok": bool(j.get("ok", True)) and ours, "ours": ours, "data": d,
            "raw": json.dumps(d, ensure_ascii=False)}


def _decode_cmd(raw: bytes) -> str:
    """中文 Windows 下 netstat / tasklist 的输出是本地代码页(GBK)，按 GBK 解。"""
    return (raw or b"").decode("gbk", "replace")


def health_pid(health: dict) -> Optional[int]:
    """服务自己在 /api/health 里报的 pid。"""
    try:
        pid = int((health.get("data") or {}).get("pid") or 0)
    except (TypeError, ValueError):
        return None
    return pid or None


def pid_by_port(port: int) -> Optional[int]:
    """从 netstat 反查监听该端口的 PID。"""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    for line in _decode_cmd(r.stdout).splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        if parts[1].rsplit(":", 1)[-1] != str(port):
            continue
        if parts[4].isdigit():
            return int(parts[4])
    return None


def process_is_python(pid: int) -> bool:
    """确认这个 PID 是 python / pythonw，兜住「万一是别的程序」的情况。"""
    try:
        r = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=20)
    except Exception:  # noqa: BLE001
        return False
    out = _decode_cmd(r.stdout).lower()
    return "python" in out


def resolve_service_pid(health: dict) -> tuple:
    """拿到正在运行的本服务的 PID —— PID 文件丢了也不会抓瞎。

    优先级：PID 文件 → 服务自报(/api/health) → 端口反查(netstat)。
    若 PID 文件与 health 报的不一致，以 health 为准（文件多半是过期的，
    而 PID 是会被系统复用的，照着过期文件 kill 有可能误杀别的进程）。

    返回 (pid, 来源说明)；查不到时 pid 为 None，文案里说明原因。
    """
    file_pid: Optional[int] = None
    if PID_FILE.exists():
        try:
            file_pid = int(PID_FILE.read_text(encoding="ascii").strip()) or None
        except Exception:  # noqa: BLE001
            file_pid = None

    live_pid = health_pid(health)

    if file_pid and live_pid and file_pid != live_pid:
        return live_pid, "服务自报（PID 文件里是过期的 %d）" % file_pid
    if file_pid:
        return file_pid, "PID 文件"
    if live_pid:
        return live_pid, "服务自报（/api/health）"

    by_port = pid_by_port(PORT)
    if by_port:
        return by_port, "端口反查（netstat）"
    return None, "PID 文件缺失，health 与 netstat 都没给出 PID"


def code_fingerprint() -> str:
    """server.py + static/index.html + 引擎 engine.py 的内容指纹（与 server.py 里同名函数一致）。

    必须用内容而不是 mtime：这个工作区里文件改写后 **mtime 可能被保留成旧值**
    （实测内容已变、mtime 还停在几十分钟前），靠 mtime 判断就会漏掉"代码已更新"，
    于是报「服务已在运行」却继续跑旧代码。

    🔴 第三个文件 `wb-task-hub/engine.py` 不能省：引擎模块被 `EngineBridge.load()`
    **缓存**住，改引擎必须重启服务才生效，漏掉它就又会出现「显示代码最新、其实跑旧引擎」。
    ⚠️ vendor/workbuddy_daily.py 是每轮重读不缓存的，**故意不放进来**，免得无谓重启。
    顺序与占位符都要和 server.py 里那个实现**逐字对齐**，否则两边指纹永远不相等。
    """
    h = hashlib.sha1()
    for p in (BASE_DIR / "server.py", BASE_DIR / "static" / "index.html",
              BASE_DIR.parent / "engine" / "engine.py"):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(b"?")
    return h.hexdigest()[:12]


def code_is_newer(health: dict) -> bool:
    """端口上跑着的实例，用的代码与磁盘上的不一致 → 需要重启载入新代码。

    比的是**内容指纹**，不是 mtime（原因见 code_fingerprint）。

    老实例（health 里还没有 code 字段）没法判断新旧 → 直接当它旧、重启一次；
    重启后新实例会带上指纹，往后就是精确比对，不会再多重启。
    """
    running = str((health.get("data") or {}).get("code") or "")
    if not running:
        return True
    return running != code_fingerprint()


def log_tail(n: int = 12) -> str:
    if not LOG_FILE.exists():
        return "（暂无日志）"
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return "（日志读取失败）"
    return "\n".join(lines[-n:]) or "（日志为空）"


def say(msg: str = "") -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- 动作

def do_status(quiet: bool = False) -> int:
    p = probe_health()
    if p["ours"]:
        if not quiet:
            pid, how = resolve_service_pid(p)
            say("● 运行中   http://127.0.0.1:%d" % PORT)
            lan = lan_url()
            if lan:
                say("  局域网     %s" % lan)
            say("  %s" % p["raw"])
            if pid:
                say("  PID %d（来自%s）" % (pid, how))
            if code_is_newer(p):
                say("  ⚠ server.py 比服务新，跑的是旧代码 —— 执行 restart 生效")
            else:
                say("  ✔ 代码是最新的")
        return 0
    if port_open(PORT):
        if not quiet:
            pid = pid_by_port(PORT)
            say("✘ 端口 %d 被**其他程序**占用（PID %s），本服务未运行。"
                % (PORT, pid or "未知"))
        return 3
    if not quiet:
        say("○ 未运行")
    return 1


def rotate_log_if_big(max_bytes: int = 1_000_000) -> str:
    """日志超过上限就先把它挪成 server.log.1（只留一代）。

    这个文件只 append 不轮转的话会一直涨；一次轮转保留上一代，
    足够事后排查，也不会无限占地方。

    返回一句说明（没轮转就返回空串）。
    """
    try:
        size = LOG_FILE.stat().st_size
    except OSError:
        return ""
    if size <= max_bytes:
        return ""
    old = LOG_FILE.with_name(LOG_FILE.name + ".1")
    try:
        if old.exists():
            old.unlink()
        LOG_FILE.rename(old)          # 用 rename 而不是先删后建，尽量不丢内容
    except OSError as e:
        return "（日志轮转失败：%s）" % e
    return "（日志已超过 %dKB，旧内容转到 %s）" % (max_bytes // 1000, old.name)


def do_start() -> int:
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)

    running = probe_health(timeout=1.5)
    if running["ours"]:
        if code_is_newer(running):
            say("检测到代码已更新，自动重启以加载新版本 ...")
            do_stop()
            time.sleep(0.8)
        else:
            say("服务已在运行  →  http://127.0.0.1:%d" % PORT)
            return 0

    if port_open(PORT):
        say("✘ 启动已取消：端口 %d 已被其他程序占用。" % PORT)
        say("")
        say("  这次不硬抢端口，避免把别人的服务顶掉。请先确认占用者：")
        say("    netstat -ano | findstr :%d" % PORT)
        say("  然后改 server.py 里的 PORT，或停掉占用该端口的程序。")
        return 3

    if not PYW.exists():
        say("✘ 找不到可用的 Python 解释器（pythonw / python）：%s" % PYW)
        say("  解决：设置环境变量 WB_HUB_PYTHONW 指向 pythonw.exe，")
        say("  或在仓库根目录建虚拟环境：python -m venv .venv")
        return 4

    rotated = rotate_log_if_big()
    log_f = open(LOG_FILE, "a", encoding="utf-8")
    log_f.write("\n===== start %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    log_f.flush()
    if rotated:
        say(rotated)

    # pythonw.exe 的 stdout 不是终端，Python 会退回系统 ANSI 代码页（中文 Windows 是 GBK）
    # 写中文，而日志文件是按 UTF-8 读的 → 启动那行会变成乱码。
    # 显式指定 PYTHONIOENCODING 就能对齐。
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000
    try:
        subprocess.Popen(
            [str(PYW), "server.py"],
            cwd=str(BASE_DIR),
            stdin=subprocess.DEVNULL, stdout=log_f, stderr=log_f,
            creationflags=DETACHED_PROCESS | CREATE_NO_WINDOW,
            close_fds=True, env=env,
        )
    except Exception as e:  # noqa: BLE001
        say("✘ 拉起进程失败：%s" % e)
        return 4

    say("正在启动 ...")
    deadline = time.time() + 20
    while time.time() < deadline:
        time.sleep(0.6)
        h = probe_health(timeout=1.2)
        if h["ours"]:
            # 服务起来了，把 PID 落盘供 stop 精确终止。
            # ⚠️ 这个文件是运行中实例的状态文件，不是缓存 —— 不许随手删。
            pid = health_pid(h) or pid_by_port(PORT)
            if pid:
                try:
                    PID_FILE.write_text(str(pid), encoding="ascii")
                except Exception:  # noqa: BLE001
                    pass
            say("✔ 启动成功（窗口已隐藏）  →  http://127.0.0.1:%d" % PORT)
            lan = lan_url()
            if lan:
                say("  局域网也能访问：%s" % lan)
            say("  停止服务请双击 stop.bat")
            return 0

    say("✘ 启动失败：服务未在 20 秒内响应。")
    say("")
    say("--- 日志尾部 ---")
    say(log_tail())
    say("")
    # 🔴 2026-09-20 实测：从「工具/脚本会话」里拉起 pythonw，子进程会随会话结束被回收
    #   （DETACHED_PROCESS 也拦不住），表现为「启动日志全打出来了、health 也响应过、
    #   然后进程凭空消失、只留 8793 的 TIME_WAIT」。这不是代码问题，是执行环境限制。
    #   要长期常驻，必须让进程归「任务计划服务」管 —— 用计划任务拉起即可。
    say("如果日志里能看到完整的启动输出（🚀 ... 调度线程已启动 ...），")
    say("那多半不是代码问题，而是**当前这个执行环境会在退出时回收子进程**：")
    say("  从命令行/脚本会话里拉起的服务活不过会话结束。")
    say("  两条可靠的常驻办法（任选其一）：")
    say("    1) 双击 start.bat（在真实桌面会话里跑，不受影响）")
    say("    2) 走计划任务：schtasks /Run /TN WbTaskHubBoot")
    return 5


def do_stop() -> int:
    health = probe_health(timeout=1.2)

    if health["ours"]:
        pid, how = resolve_service_pid(health)
        if pid is None:
            say("✘ 服务在运行，但拿不到它的 PID，为安全起见不做终止。")
            say("  %s" % how)
            say("  手动停止：netstat -ano | findstr :%d  再 taskkill /PID <pid> /F" % PORT)
            return 4
        if not process_is_python(pid):
            say("✘ PID %d（%s）不是 python 进程，拒绝终止以免误杀。" % (pid, how))
            say("  请自行确认：tasklist /FI \"PID eq %d\"" % pid)
            return 4

        # 🔴 只 kill 这一个 PID。禁止 taskkill /IM pythonw.exe /F
        # （会连带杀掉同机上其他 pythonw 起的无关服务，而它们未必会自动重启）。
        # taskkill 在中文 Windows 上输出 GBK，别去捕获它（会撞 UTF-8 解码）。
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stopped = False
        for _ in range(20):
            time.sleep(0.3)
            if not probe_health(timeout=1.0)["ours"]:
                stopped = True
                break
        if not stopped:
            say("✘ 已发出终止指令，但服务仍在响应（PID %d，来自%s）。" % (pid, how))
            return 4
        say("✔ 已停止（PID %d，来自%s）" % (pid, how))
        try:
            PID_FILE.unlink()
        except Exception:  # noqa: BLE001
            pass
        return 0

    if port_open(PORT):
        pid = pid_by_port(PORT)
        who = "PID %d" % pid if pid else "进程未知"
        say("✘ 端口 %d 被其他程序占用（%s），不是本服务，未做任何终止操作。" % (PORT, who))
        return 3

    say("○ 服务本来就没在运行")
    # 只在确认端口真的空着时才清 PID 文件（自愈残留）。
    if PID_FILE.exists():
        try:
            PID_FILE.unlink()
        except Exception:  # noqa: BLE001
            pass
    return 1


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "start").lower()
    if cmd == "start":
        return do_start()
    if cmd == "stop":
        return do_stop()
    if cmd == "status":
        return do_status()
    if cmd == "restart":
        do_stop()
        time.sleep(0.8)
        return do_start()
    say("未知命令：%s（可用：start / stop / restart / status）" % cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main())
