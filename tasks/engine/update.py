#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""wb-task-hub 上游同步器

把 L0NE-6/WorkBuddy-Daily 的任务逻辑静默同步到本地 vendor/ 目录。

为什么需要护栏：
  上游脚本是带着你的 access token 去调腾讯接口的。如果它哪天被投毒、或者行为突变
  （比如开始往陌生域名传数据），静默替换就等于把风险直接放行。所以每次替换前都要过一遍
  静态检查：体积、域名白名单、必须存在的函数、能否编译、有没有危险调用。
  检查通过 → 静默替换（备份旧版）；检查不过 → 保留旧版并把告警写出来等人处理。

一个前提：引擎侧对上游只做运行时 monkey-patch，不改文件。所以这里的替换永远是
「整文件覆盖」，不需要 merge，也就不会因为我们的改动而产生冲突。
"""

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import urllib3

urllib3.disable_warnings()

ROOT = Path(__file__).resolve().parent
VENDOR = ROOT / "vendor"
CONFIG_PATH = ROOT / "config" / "tasks.json"
STATE_PATH = ROOT / "config" / "upstream.json"
BACKUP_DIR = ROOT / "backups"
LOG_DIR = ROOT / "logs"
ALARM_PATH = LOG_DIR / "update-alarm.txt"

GITHUB_API = "https://api.github.com"

# 即使在允许域名之外也绝不放行的危险调用（上游本来就不用这些）
DANGEROUS_PATTERNS = [
    (r"\bsubprocess\b", "调用外部进程"),
    (r"\bos\.system\s*\(", "执行 shell 命令"),
    (r"\bos\.popen\s*\(", "执行 shell 命令"),
    (r"\beval\s*\(", "动态求值"),
    (r"\bexec\s*\(", "动态执行代码"),
    (r"\b__import__\s*\(", "动态导入"),
    (r"\bpickle\b", "反序列化"),
    (r"\bbase64\.b64decode\s*\([^)]*\)\s*\)\s*\.decode", "疑似隐藏载荷"),
]


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def read_state():
    if STATE_PATH.exists():
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"files": {}, "last_check": 0, "history": []}


def write_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_PATH)  # 原子替换，避免读到半截文件


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_file(path):
    return sha256_bytes(path.read_bytes()) if path.exists() else ""


def fetch_remote(owner_repo, branch, name):
    """取上游单个文件内容。走 API + base64 —— 本机 raw.githubusercontent.com 不通（200 但 0 字节）。"""
    import requests

    url = "%s/repos/%s/contents/%s?ref=%s" % (GITHUB_API, owner_repo, name, branch)
    s = requests.Session()
    s.trust_env = False
    r = s.get(url, timeout=40, headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        raise RuntimeError("取 %s 失败: HTTP %s" % (name, r.status_code))
    payload = r.json()
    if payload.get("encoding") == "base64" and payload.get("content"):
        return base64.b64decode(payload["content"])
    raise RuntimeError("取 %s 返回内容异常" % name)


def extract_hosts(text):
    """抽出脚本里所有 URL 的 host，用于域名白名单校验。"""
    hosts = set()
    for raw in re.findall(r"https?://([A-Za-z0-9._\-]+)", text):
        hosts.add(raw.lower().strip("."))
    return hosts


def host_allowed(host, allowed):
    return any(host == a or host.endswith("." + a) for a in allowed)


def check_new_file(name, data, old_bytes, guards):
    """静态安全检查。返回 (是否通过, 问题列表)。

    域名与危险调用采用「增量」判定：只拦相对旧版**新出现**的东西。
    原因：上游本来就用 subprocess 重启桌面端、用 127.0.0.1 连 CDP、用推送域名发通知。
    若拿这份历史遗留去全量校验，上游每次更新都会被 100% 拦下，静默同步直接失效。
    增量判定保留了真正的防护语义 —— 新域名/新危险调用 = 新行为 = 需要人看一眼。
    """
    problems = []
    text = data.decode("utf-8", errors="replace")
    old_text = old_bytes.decode("utf-8", errors="replace") if old_bytes else ""

    size = len(data)
    if size < guards.get("min_bytes", 0):
        problems.append("体积异常小：%d 字节 < 下限 %d" % (size, guards.get("min_bytes", 0)))
    if size > guards.get("max_bytes", 1 << 60):
        problems.append("体积异常大：%d 字节 > 上限 %d" % (size, guards.get("max_bytes", 0)))

    if old_bytes:
        ratio = abs(size - len(old_bytes)) / float(len(old_bytes))
        limit = guards.get("max_size_delta_ratio", 1.0)
        if ratio > limit:
            problems.append("体积相对旧版变化 %.0f%%（上限 %.0f%%），可能是大改或被替换"
                            % (ratio * 100, limit * 100))

    try:
        compile(text, name, "exec")
    except SyntaxError as e:
        problems.append("语法无法编译：%s" % e)

    allowed = [h.lower() for h in (guards.get("allowed_hosts") or [])]
    new_hosts = extract_hosts(text) - extract_hosts(old_text)
    if allowed and new_hosts:
        bad = sorted(h for h in new_hosts if not host_allowed(h, allowed))
        if bad:
            problems.append("新增白名单外域名（疑似新增外传通道）：%s" % ", ".join(bad[:8]))

    for fn in guards.get("require_functions") or []:
        if not re.search(r"^def\s+%s\s*\(" % re.escape(fn), text, re.M):
            problems.append("缺少必需函数 %s()（上游可能重构，引擎的注入会失准）" % fn)

    old_labels = {label for pat, label in DANGEROUS_PATTERNS if re.search(pat, old_text)}
    new_labels = {label for pat, label in DANGEROUS_PATTERNS if re.search(pat, text)}
    added = sorted(new_labels - old_labels)
    if added:
        problems.append("新增危险调用类型：%s" % "、".join(added))

    return (not problems), problems


def backup_current(name):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src = VENDOR / name
    if not src.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = BACKUP_DIR / ("%s.%s.bak" % (name, stamp))
    shutil.copy2(src, dst)
    return dst


def write_atomically(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def raise_alarm(message, details=None):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(ALARM_PATH, "w", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
        if details:
            for d in details:
                f.write("  - %s\n" % d)
        f.write("\n把上游新版本放行需要人工确认：确认无误后删除本文件或用 --force 重跑。\n")
    print("🚨 上游更新被拦下，已写告警：%s" % ALARM_PATH)
    print("   %s" % message)
    for d in details or []:
        print("     - %s" % d)


def clear_alarm():
    if ALARM_PATH.exists():
        ALARM_PATH.unlink()


def sync(force=False, verbose=True):
    """执行一次同步。返回 dict：{'status': ..., 'changed': [...]}"""
    cfg = load_config()
    upd = cfg.get("auto_update") or {}
    if not upd.get("enabled", True) and not force:
        return {"status": "disabled", "changed": []}

    repo = upd.get("repo")
    branch = upd.get("branch", "main")
    files = upd.get("files") or []
    guards = upd.get("guards") or {}
    interval = float(upd.get("check_interval_hours", 12)) * 3600

    state = read_state()
    now = time.time()
    if not force and state.get("last_check") and (now - state["last_check"]) < interval:
        return {"status": "skipped", "changed": [], "reason": "距上次检查不足 %.0f 小时"
                % (interval / 3600)}

    changed, blocked, checked = [], [], []
    for name in files:
        target = VENDOR / name
        old_bytes = target.read_bytes() if target.exists() else None
        try:
            data = fetch_remote(repo, branch, name)
        except Exception as e:
            if verbose:
                print("⚠️  %s 拉取失败：%s" % (name, e))
            state["last_check"] = now
            state["last_result"] = "fetch-failed"
            write_state(state)
            return {"status": "fetch-failed", "changed": [], "error": str(e)}

        new_sha = sha256_bytes(data)
        old_sha = sha256_bytes(old_bytes) if old_bytes else ""
        checked.append({"name": name, "sha256": new_sha, "size": len(data)})

        if new_sha == old_sha:
            continue

        ok, problems = check_new_file(name, data, old_bytes, guards)
        if not ok:
            blocked.append({"name": name, "problems": problems})
            continue

        bak = backup_current(name)
        write_atomically(target, data)
        changed.append({"name": name, "old_sha": old_sha[:16], "new_sha": new_sha[:16],
                        "size": len(data), "backup": str(bak) if bak else None})
        if verbose:
            print("🔄 已同步 %s（%s → %s，备份 %s）"
                  % (name, old_sha[:12] or "无", new_sha[:12], bak.name if bak else "无"))

    state["last_check"] = now
    state["files"] = {c["name"]: c for c in checked}
    if blocked:
        state["last_result"] = "blocked"
        state["history"] = (state.get("history") or [])[-19:] + [{
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "result": "blocked",
            "detail": "; ".join("%s: %s" % (b["name"], " / ".join(b["problems"])) for b in blocked),
        }]
        write_state(state)
        details = []
        for b in blocked:
            details.extend("%s → %s" % (b["name"], p) for p in b["problems"])
        raise_alarm("上游 %s 有新版本，但未通过安全检查，已保留旧版" % repo, details)
        return {"status": "blocked", "changed": changed, "blocked": blocked}

    if changed:
        clear_alarm()
        state["last_result"] = "updated"
        state["history"] = (state.get("history") or [])[-19:] + [{
            "at": time.strftime("%Y-%m-%d %H:%M:%S"), "result": "updated",
            "detail": "; ".join("%s %s→%s" % (c["name"], c["old_sha"], c["new_sha"]) for c in changed),
        }]
    else:
        state["last_result"] = "up-to-date"
    write_state(state)

    if verbose:
        if changed:
            print("✅ 静默同步完成：%d 个文件已更新" % len(changed))
        else:
            print("✅ 上游已是最新（%s/%s）" % (repo, branch))
    return {"status": "updated" if changed else "up-to-date", "changed": changed}


def main():
    ap = argparse.ArgumentParser(description="同步上游 WorkBuddy-Daily 任务逻辑")
    ap.add_argument("--force", action="store_true", help="忽略检查间隔与 sha 一致，强制重拉")
    ap.add_argument("--check", action="store_true", help="只看状态，不写文件")
    ap.add_argument("--status", action="store_true", help="打印本地记录")
    args = ap.parse_args()

    if args.status:
        st = read_state()
        print(json.dumps(st, ensure_ascii=False, indent=2))
        return 0

    if args.check:
        cfg = load_config()
        upd = cfg["auto_update"]
        for name in upd["files"]:
            local = VENDOR / name
            print("%-24s 本地 %s" % (name, sha256_file(local)[:16] or "缺失"))
        st = read_state()
        print("上次检查: %s" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.get("last_check", 0)))
                              if st.get("last_check") else "从未"))
        print("上次结果: %s" % st.get("last_result", "-"))
        if ALARM_PATH.exists():
            print("\n🚨 存在未处理的告警：%s" % ALARM_PATH)
        return 0

    result = sync(force=args.force)
    if result["status"] == "blocked":
        return 3
    if result["status"] == "fetch-failed":
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
