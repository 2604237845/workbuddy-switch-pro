"""HAR 抓包分析工具 —— 从真实抓包里反推任务判据接口/报文形状。

用法：
    python probe_har.py [--har <抓包文件>] <命令> [参数]

    --har 省略时用 `WB_HAR` 环境变量，再省略则用 DEFAULT_HAR（上一次的抓包）。

    python probe_har.py list [host关键字]        # 列出请求（方法 + 状态 + 长度 + 路径）
    python probe_har.py chrono [host关键字]      # 按**真实时间**排序列出（HAR 常是倒序）
    python probe_har.py time <序号或关键字>       # 看某条请求前后相邻的条目（定位因果最有用）
    python probe_har.py body <序号或关键字>       # 打印某条请求的完整请求头 + 请求体
    python probe_har.py resp <序号或关键字>       # 打印某条请求的响应体
    python probe_har.py grep <关键字>            # 在「路径 + 请求体 + 响应体」里全文搜
    python probe_har.py expert [关键字]          # 专家相关专用视图（默认关键字 expert）

设计理由：HAR 里几百条噪声很大（CDN/埋点/图片），先按域名+路径筛，再按关键字精确定位。
"""
import json
import os
import sys
from pathlib import Path

DEFAULT_HAR = Path(r"D:/WX/xwechat_files/wxid_nfpq6um33gk622_ca3d/msg/file/2026-09/15ee0d17378c3a3bc7705fea611c6de0.har")
HAR = DEFAULT_HAR          # 可被 `--har <path>` 或环境变量 WB_HAR 覆盖

SKIP_HOSTS = ("download.codebuddy.cn", "openplatform-cdn", "dscache", "servicewechat.com",
              "support.weixin.qq.com", "launchwxacode", "search.wxqcloud")


def load():
    return json.loads(HAR.read_text(encoding="utf-8"))["log"]["entries"]


def _body(entry, which="request"):
    """取 request/response 的 body 文本（优先 text，其次 base64 解码）。"""
    import base64
    post = entry.get(which) or {} if which == "response" else entry.get("request") or {}
    if which == "response":
        post = entry.get("response") or {}
    content = (post.get("postData") or {}) if which == "request" else (post.get("content") or {})
    txt = content.get("text")
    if txt is None:
        return ""
    if content.get("encoding") == "base64":
        try:
            return base64.b64decode(txt).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return "<base64 解码失败>"
    return txt


def _hdrs(entry):
    hs = (entry.get("request") or {}).get("headers") or []
    return {h.get("name", ""): h.get("value", "") for h in hs}


def _url(entry):
    return (entry.get("request") or {}).get("url", "")


def _short(entry):
    """去掉 scheme 与 query 的短 URL（对畸形/空 URL 也要安全）。"""
    u = _url(entry)
    if not u:
        return "(无 URL)"
    body = u.split("://", 1)[1] if "://" in u else u
    return body.split("?")[0]


def _host(entry):
    """取主机名（对无 scheme / 畸形 URL 安全）。"""
    body = _short(entry)
    return body.split("/")[0]


def _rows(entries):
    """按真实时间排序，并带上原始序号（HAR 数组常常是逆序的）。"""
    return sorted(enumerate(entries, 1), key=lambda p: p[1].get("startedDateTime", ""))


def cmd_chrono(entries, filt=""):
    print("%-13s %-5s %-6s %-4s %-8s %s" % ("时间", "#", "方法", "码", "请求体", "URL"))
    print("-" * 120)
    for i, e in _rows(entries):
        u = _url(e)
        if filt and filt not in u:
            continue
        if not filt and any(s in _host(e) for s in SKIP_HOSTS):
            continue
        n = len(((e.get("request") or {}).get("postData") or {}).get("text") or "")
        code = (e.get("response") or {}).get("status", "")
        print("%-13s %-5d %-6s %-4s %-8s %s"
              % (e.get("startedDateTime", "")[11:23], i,
                 (e.get("request") or {}).get("method", ""), code, n or "-", _short(e)))


def cmd_time(entries, key):
    """看某条请求的真实时间 + 前后相邻条目（定位因果最有用）。"""
    rows = _rows(entries)
    idx = None
    for j, (i, e) in enumerate(rows):
        hit = (key.isdigit() and int(key) == i) or (not key.isdigit()
                                                    and key.lower() in _url(e).lower())
        if hit:
            idx = j
            break
    if idx is None:
        print("未找到 %r" % key)
        return
    for j in range(max(0, idx - 8), min(len(rows), idx + 9)):
        i, e = rows[j]
        mark = "   ← ★" if j == idx else ""
        print("  %s  #%-4d %-6s %s%s" % (e.get("startedDateTime", "")[11:23], i,
                                         (e.get("request") or {}).get("method", ""),
                                         _short(e), mark))


def cmd_list(entries, filt=""):
    print("%-4s %-6s %-4s %-8s %s" % ("#", "方法", "码", "请求体", "URL"))
    print("-" * 110)
    for i, e in enumerate(entries, 1):
        u = _url(e)
        if filt and filt not in u:
            continue
        if not filt and any(s in _host(e) for s in SKIP_HOSTS):
            continue
        req = e.get("request") or {}
        try:
            n = len((req.get("postData") or {}).get("text") or "")
        except Exception:  # noqa: BLE001
            n = 0
        code = (e.get("response") or {}).get("status", "")
        print("%-4d %-6s %-4s %-8s %s" % (i, req.get("method", ""), code, n or "-", _short(e)))


def _match(entries, key):
    """key 是序号（1 起）或子串。"""
    if key.isdigit() and 1 <= int(key) <= len(entries):
        return [entries[int(key) - 1]]
    out = []
    for e in entries:
        blob = _url(e) + _body(e, "request")
        if key.lower() in blob.lower():
            out.append(e)
    return out


def cmd_body(entries, key):
    for e in _match(entries, key):
        print("=" * 100)
        print("%s %s" % ((e.get("request") or {}).get("method"), _url(e)))
        print("--- 请求头 ---")
        for k, v in _hdrs(e).items():
            print("  %s: %s" % (k, v[:200]))
        print("--- 请求体 ---")
        print(_body(e, "request")[:4000] or "(无)")
        print()


def cmd_resp(entries, key):
    for e in _match(entries, key):
        print("=" * 100)
        print("%s %s → HTTP %s" % ((e.get("request") or {}).get("method"), _url(e),
                                  (e.get("response") or {}).get("status")))
        print(_body(e, "response")[:3000] or "(无)")


def cmd_grep(entries, kw):
    kw_l = kw.lower()
    hits = 0
    for i, e in enumerate(entries, 1):
        parts = []
        if kw_l in _url(e).lower():
            parts.append("URL")
        if kw_l in _body(e, "request").lower():
            parts.append("请求体")
        if kw_l in _body(e, "response").lower():
            parts.append("响应体")
        if parts:
            hits += 1
            print("  #%-4d [%s] %s" % (i, "+".join(parts), _short(e)))
    print("  （命中 %d 条）" % hits)


def cmd_expert(entries, kw="expert"):
    """专家相关专用视图：先按 URL 命中，再按请求体命中。"""
    kw = kw or "expert"
    print("=== URL 含 %r ===" % kw)
    for i, e in enumerate(entries, 1):
        if kw.lower() in _url(e).lower():
            print("  #%-4d %-6s %s" % (i, (e.get("request") or {}).get("method"), _short(e)))
    print()
    print("=== 请求体含 %r ===" % kw)
    for i, e in enumerate(entries, 1):
        if kw.lower() in _body(e, "request").lower():
            u = _short(e)
            if any(s in u for s in SKIP_HOSTS):
                continue
            print("  #%-4d %-6s %s" % (i, (e.get("request") or {}).get("method"), u))


def main():
    global HAR
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    argv = list(sys.argv[1:])
    if "--har" in argv:                      # --har <path> 优先
        i = argv.index("--har")
        if i + 1 >= len(argv):
            print("--har 后面要跟抓包文件路径")
            return 2
        HAR = Path(argv[i + 1])
        del argv[i:i + 2]
    elif os.environ.get("WB_HAR"):           # 其次环境变量
        HAR = Path(os.environ["WB_HAR"])
    if not HAR.exists():
        print("抓包文件不存在：%s" % HAR)
        return 2
    entries = load()
    cmd = argv[0] if argv else "list"
    arg = argv[1] if len(argv) > 1 else ""
    if cmd == "list":
        cmd_list(entries, arg)
    elif cmd == "chrono":
        cmd_chrono(entries, arg)
    elif cmd == "time":
        cmd_time(entries, arg)
    elif cmd == "body":
        cmd_body(entries, arg)
    elif cmd == "resp":
        cmd_resp(entries, arg)
    elif cmd == "grep":
        cmd_grep(entries, arg)
    elif cmd == "expert":
        cmd_expert(entries, arg)
    else:
        print("未知命令 %s（可用：list / chrono / time / body / resp / grep / expert）" % cmd)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
