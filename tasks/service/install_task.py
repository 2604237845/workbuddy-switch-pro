"""注册 / 卸载 Windows 计划任务，让 wb-hub 开机（登录）自动跑。

为什么必须用计划任务，而不是让 launch.py 自己 Popen：
  在终端或 WorkBuddy 会话里起的进程**会随该会话结束被回收**（实测：launch.py start
  成功、/api/health 也通了，但工具调用一结束，PID 就消失）。计划任务里的进程归
  「任务计划服务」管，不隶属于任何登录会话，才能真正长期驻留。

任务的运行方式：调用本目录的 start.bat（内部再走 launch.py start），
  好处是 start.bat 已经处理了「已在运行就直接返回」「端口被占就拒绝」等分支，
  计划任务不会被这些边界情况弄出重复实例。

用法：
  python install_task.py            注册（登录时自动启动）
  python install_task.py --boot     注册（开机即启动，不等人登录）
  python install_task.py --remove   卸载
  python install_task.py --status   查看
  python install_task.py --run      立刻拉起一次
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TASK_NAME = "WbTaskHubBoot"
BASE_DIR = Path(__file__).resolve().parent
START_BAT = BASE_DIR / "start.bat"

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def say(msg: str = "") -> None:
    print(msg, flush=True)


def run(args: list) -> subprocess.CompletedProcess:
    """跑 schtasks 并拿到原始输出（中文 Windows 下是 GBK）。"""
    return subprocess.run(args, capture_output=True)


def out_text(r: subprocess.CompletedProcess) -> str:
    raw = (r.stdout or b"") + (r.stderr or b"")
    try:
        return raw.decode("gbk", "replace")
    except Exception:  # noqa: BLE001
        return raw.decode("utf-8", "replace")


def do_install(boot: bool = False) -> int:
    if not START_BAT.exists():
        say("✘ 找不到 %s" % START_BAT)
        return 4

    trigger = "ONSTART" if boot else "ONLOGON"
    # /RL HIGHEST 让它以最高权限跑，避免个别机器上策略拦截；
    # /F 覆盖同名任务，重复注册不会报错。
    args = ["schtasks", "/Create",
            "/TN", TASK_NAME,
            "/TR", '"%s"' % START_BAT,
            "/SC", trigger,
            "/RL", "HIGHEST",
            "/F"]
    # 登录触发时给 1 分钟缓冲：登录瞬间网络/DNS 往往还没就绪，
    # 而 server.py 启动时要拉账号库、调度器也要联网，太早起来容易首轮失败。
    if not boot:
        args += ["/DELAY", "0001:00"]
    r = run(args)
    txt = out_text(r)
    if r.returncode != 0:
        say("✘ 注册失败（exit=%d）" % r.returncode)
        say(txt.strip())
        say("")
        say("  提示：以管理员身份重开一个终端再试（schtasks 建任务通常需要提权）。")
        return 4

    say("✔ 已注册计划任务：%s" % TASK_NAME)
    say("   触发方式：%s" % ("开机（不等人登录）" if boot else "用户登录时"))
    say("   执行：%s" % START_BAT)
    say("")
    say("  ⚠️ 立刻生效请执行：python install_task.py --run")
    say("     或下次登录/开机时自动启动。")
    return 0


def do_remove() -> int:
    r = run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
    txt = out_text(r)
    if r.returncode != 0:
        if "找不到" in txt or "cannot find" in txt.lower():
            say("○ 任务本来就不存在")
            return 1
        say("✘ 卸载失败：")
        say(txt.strip())
        return 4
    say("✔ 已卸载计划任务：%s（服务进程本身没停，要停请双击 stop.bat）" % TASK_NAME)
    return 0


def do_status() -> int:
    r = run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    txt = out_text(r)
    if r.returncode != 0:
        say("○ 未注册计划任务（%s 不存在）" % TASK_NAME)
        return 1

    # 中文 Windows 的字段名是中文，做个宽松的行内匹配，避免依赖列位置。
    keep = ("任务名", "状态", "下次运行时间", "上次运行时间", "上次结果",
            "要运行的任务", "下次运行", "上次运行", "结果",
            "TaskName", "Status", "Next Run", "Last Run", "Last Result",
            "Task To Run", "Schedule")
    for line in txt.splitlines():
        if any(k in line for k in keep):
            say(line.rstrip())
    return 0


def do_run() -> int:
    r = run(["schtasks", "/Run", "/TN", TASK_NAME])
    txt = out_text(r)
    if r.returncode != 0:
        say("✘ 触发失败（任务可能还没注册）：")
        say(txt.strip())
        return 4
    say("✔ 已触发计划任务，几秒后可用 status / 浏览器确认")
    return 0


def main() -> int:
    arg = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if arg in ("", "--install"):
        return do_install(boot=False)
    if arg == "--boot":
        return do_install(boot=True)
    if arg in ("--remove", "--uninstall"):
        return do_remove()
    if arg == "--status":
        return do_status()
    if arg == "--run":
        return do_run()
    say("未知参数：%s" % arg)
    say("用法：python install_task.py [--boot|--remove|--status|--run]")
    return 2


if __name__ == "__main__":
    sys.exit(main())
