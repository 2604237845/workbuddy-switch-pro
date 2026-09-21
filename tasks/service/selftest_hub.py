# -*- coding: utf-8 -*-
"""wb-hub 自检 —— 锁住 2026-09-20 代码审计中修掉的坑，防止再犯。

跑法：
    python selftest_hub.py

⚠️ 只用**静态检查 + 纯函数调用**，不起服务、不联网、不碰用户账号数据。
   涉及「事件循环不能被阻塞」这类约束，断言的是**源码形状**（是否走了 to_thread），
   因为真跑一条阻塞路由需要起服务 + 真触发任务，代价太大且会污染真实数据。
"""

import re
import sys
import pathlib

BASE = pathlib.Path(__file__).resolve().parent
SERVER = (BASE / "server.py").read_text(encoding="utf-8")
LAUNCH = (BASE / "launch.py").read_text(encoding="utf-8")
INDEX = (BASE / "static" / "index.html").read_text(encoding="utf-8")

passed = 0
failed = []


def check(cond, label):
    global passed
    if cond:
        passed += 1
        print("  ✅ %s" % label)
    else:
        failed.append(label)
        print("  ❌ %s" % label)


def _strip_docstring(fn_src: str) -> str:
    """去掉函数体开头的 docstring。

    很多断言会被 docstring 里**引用的旧错误写法**误伤（我就在这栽过：
    注释里为了说明「原来错在哪」把旧那行抄了一遍，结果断言命中了注释）。

    传进来的通常是整段 `def ...:` + 函数体，所以要：
      1) 先在**第一个冒号换行处**切掉签名，只留函数体；
      2) 再把函数体开头的 docstring 去掉。
    """
    # 切掉签名：从 `def name(...)` 到第一个 `:` + 换行
    m = re.search(r"^[^:]*:(?:[^\n]*\n)?", fn_src, re.S)
    body = fn_src[m.end():] if m else fn_src
    # 去 docstring
    d = re.match(r'\s*(?:"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')\s*', body)
    return body[d.end():] if d else body


def section(title):
    print("\n" + "=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------- 1. 语法
def test_syntax():
    section("第 1 组 · 全文件语法（py_compile 已在外层跑，这里查明显的结构问题）")
    check(SERVER.count("def ") > 40, "server.py 函数数量正常（>40）")
    check("bootstrap()" in SERVER, "server.py 有 bootstrap 调用")
    check('if __name__ == "__main__":' in SERVER, "server.py 有 __main__ 块")
    js = re.search(r"<script[\s\S]*?</script>", INDEX)
    check(js is not None, "index.html 能抽出 script 块")


# ---------------------------------------------------------------- 2. clear_fired_today
def test_clear_fired_today():
    section("第 2 组 · clear_fired_today 方向（2026-09-20 修：原来实现是反的）")
    m = re.search(r"def clear_fired_today\(self\)[\s\S]*?(?=\n    def |\nclass )", SERVER)
    check(m is not None, "能找到 clear_fired_today")
    body = _strip_docstring(m.group(0)) if m else ""
    # 正确实现：删「今天」的记录（startswith(today) → del）
    check("startswith(today)" in body, "判断里出现 startswith(today)")
    check(not re.search(r"if\s+not\s+self\._fired\[k\]\.startswith\(today\)", body),
          "不再是 `if not ...startswith(today)` 的反向写法")
    check(re.search(r"if\s+self\._fired\[k\]\.startswith\(today\)\s*:\s*\n\s*del",
                    body) is not None,
          "是 `if startswith(today): del` 的正向写法")


# ---------------------------------------------------------------- 3. 日志级别
def test_stdout_levels():
    section("第 3 组 · LogHub 往 stdout 输出（2026-09-20 修：原来看不到任何启动日志）")
    check("_STDOUT_SKIP_LEVELS" in SERVER, "有 stdout 级别黑名单常量")
    check('"engine"' in SERVER.split("_STDOUT_SKIP_LEVELS")[1][:120],
          "黑名单里含 engine（逐行业务输出压掉）")
    m = re.search(r"def emit\(self, level: str, msg: str\)[\s\S]*?(?=\n    def )", SERVER)
    body = m.group(0) if m else ""
    check("print(" in body, "emit 里有 print")
    check("flush=True" in body, "print 带 flush（pythonw 重定向不会丢）")
    check("_STDOUT_SKIP_LEVELS" in body, "print 受黑名单控制")
    # 黑名单必须是「跳过」语义：level not in skip → 打印
    check(re.search(r"if\s+level\s+not\s+in\s+self\._STDOUT_SKIP_LEVELS", body)
          is not None, "语义正确：level 不在黑名单里才打印")


# ---------------------------------------------------------------- 4. 事件循环
def test_no_blocking():
    section("第 4 组 · async 路由不得同步阻塞（2026-09-20 修 3 处）")
    # watch/check
    m = re.search(r'@app\.post\("/api/watch/check"\)[\s\S]*?(?=\n@app\.)', SERVER)
    b = m.group(0) if m else ""
    check("asyncio.to_thread" in b, "watch/check 走了 to_thread")
    check("WATCH.check_now()" not in b.replace("to_thread(WATCH.check_now)", ""),
          "watch/check 不再裸调 check_now()")

    # switch
    m = re.search(r'@app\.post\("/api/switch/\{idx\}"\)[\s\S]*?(?=\n@app\.)', SERVER)
    b = m.group(0) if m else ""
    check("asyncio.to_thread" in b, "switch 走了 to_thread")
    # 正确形状：阻塞调用被包成一个内部函数 _do_switch，再交给 to_thread
    check(re.search(r"def _do_switch\(\)[\s\S]*?opener\.open", b) is not None,
          "阻塞的 opener.open 被包进 _do_switch 内部函数")
    check(re.search(r"await\s+asyncio\.to_thread\(_do_switch\)", b) is not None,
          "用 await asyncio.to_thread(_do_switch) 执行")

    # watch 状态类路由
    for route in ("/api/watch", "/api/watch/config"):
        m = re.search(r'@app\.(?:get|post)\("%s"\)[\s\S]*?(?=\n@app\.)'
                      % re.escape(route), SERVER)
        b = m.group(0) if m else ""
        check("asyncio.to_thread(WATCH.status)" in b,
              "%s 的 WATCH.status 走了 to_thread" % route)

    # 通用的裸阻塞调用扫描
    naked = []
    for m in re.finditer(r"^async def (\w+)\([\s\S]*?(?=\n@app\.|\n@|\nclass |\Z)",
                         SERVER, re.M):
        name, body = m.group(1), m.group(0)
        if re.search(r"WATCH\.status\(\)", body) and "to_thread" not in body:
            naked.append(name + "→WATCH.status")
        if re.search(r"check_now\(\)", body) and "to_thread" not in body:
            naked.append(name + "→check_now")
    check(not naked, "没有 async 路由裸调阻塞函数（若有：%s）" % naked)


# ---------------------------------------------------------------- 5. nextPoll
def test_next_poll():
    section("第 5 组 · nextPoll 倒计时（2026-09-20 修：原来永远返回当前时间）")
    m = re.search(r"def next_poll_at\(self\)[\s\S]*?(?=\n    def )", SERVER)
    body = _strip_docstring(m.group(0)) if m else ""
    check(body != "", "能找到 next_poll_at")
    check("_last_tick_at" in body, "用了 _last_tick_at")
    check("now_cst().strftime" not in body,
          "不再直接返回当前时间（那种写法页面倒计时永远是 0）")
    check("remain" in body, "算的是「还剩几秒」")
    check("_last_tick_at: Optional[float] = None" in SERVER, "初始化了 _last_tick_at")
    check(SERVER.count("self._last_tick_at = time.time()") == 1,
          "只在 _tick 里刷新一次 _last_tick_at")
    check("self._last_tick_at = None" in SERVER, "start() 里会清空 _last_tick_at")


# ---------------------------------------------------------------- 6. 前端中文
def test_frontend_cn():
    section("第 6 组 · 前端状态中文化 + 无英文残留（2026-09-20）")
    check("const STATUS_CN" in INDEX, "有 STATUS_CN 映射表")
    check("function statusCn" in INDEX, "有 statusCn 兜底函数")
    check("function statusCls" in INDEX, "有 statusCls 上色兜底")
    check("未知状态" in INDEX, "未知状态有中文兜底（不再把英文贴到页面）")
    # 关键：不能再用 `STATUS_CN[x.status] || x.status` 的裸兜底
    check(not re.search(r"STATUS_CN\[[^\]]+\]\s*\|\|\s*\w+\.status\b", INDEX),
          "没有 `STATUS_CN[..] || x.status` 这种会漏英文的写法")
    # 已中文化的英文词不得复活（正文/注释里出现英文词是正常的，这里只查 UI 文案）
    for bad, why in (("只读演练（dry-run）", "dry-run 已中文化"),
                     ("日志通过 SSE 实时推送", "SSE 已中文化")):
        check(bad not in INDEX, why)


# ---------------------------------------------------------------- 7. 上游隔离
def test_upstream_intact():
    section("第 7 组 · 上游 vendor 隔离（中文化只在 wb-hub 自己的层）")
    # 中文化的实现（STATUS_CN / zh_status）在 tasks/engine/engine.py —— engine 层，
    # 不是桥接层、更不是 vendor 上游。这里核对它确实在 engine.py 里。
    # ⚠️ 必须用相对路径：写死绝对路径会让别人跑不起来（本项原本就写死了开发机路径）。
    engine = BASE.parent / "engine" / "engine.py"
    check(engine.exists(), "找到 engine.py（中文化实现所在）")
    if engine.exists():
        esrc = engine.read_text(encoding="utf-8")
        check("STATUS_CN" in esrc, "engine.py 里有 STATUS_CN 映射表")
        check("def zh_status" in esrc, "engine.py 里有 zh_status 函数")
        check("re.compile" in esrc, "用词边界正则做替换（不误伤代号）")
    # server.py（wb-hub 自己的层）不应该重复实现一份中文映射
    check("def zh_status" not in SERVER,
          "server.py 不重复实现（只有一份，在 engine.py）")


# ---------------------------------------------------------------- 8. launch 提示
def test_launch_hint():
    section("第 8 组 · launch.py 启动失败的指引（2026-09-20 加）")
    check("WbTaskHubBoot" in LAUNCH, "失败提示里给了计划任务办法")
    check("回收子进程" in LAUNCH or "回收" in LAUNCH,
          "说明了「会话结束回收子进程」这个真实陷阱")
    check("start.bat" in LAUNCH, "提示了双击 start.bat")


# ------------------------------------------------- 9. 代码指纹 + 启动副作用
def raw_fn(src, name):
    """取出某顶层函数的**完整源码**（含签名/docstring），供隔离执行 —— 比只查文本形状强。"""
    m = re.search(r"^def %s\([\s\S]*?(?=\n\S)" % re.escape(name), src, re.M)
    return m.group(0) if m else ""


def test_fingerprint_and_bootstrap():
    section("第 9 组 · 代码指纹与启动副作用（2026-09-20 加）")

    def body_of(src):
        return _strip_docstring(raw_fn(src, "code_fingerprint"))

    for name, src in (("server.py", SERVER), ("launch.py", LAUNCH)):
        b = body_of(src)
        check(b != "", "%s 里能取到 code_fingerprint 函数体" % name)
        # 引擎被 EngineBridge.load() 缓存在 self._mod → 改引擎必须重启，漏掉它就会出现
        # 「显示代码最新、其实跑旧引擎」
        check("engine.py" in b, "%s 的指纹包含 engine.py" % name)
        check("index.html" in b, "%s 的指纹包含 index.html" % name)
        check('b"?"' in b, "%s 缺失文件时填同一个占位符 b\"?\"（两边才可能相等）" % name)
        # vendor 每轮 load_upstream() 重读、不缓存，算进去只会造成无谓重启
        check("vendor" not in b, "%s 刻意不把 vendor 算进指纹" % name)

    # 两个实现必须真的算出同一个值。launch.py 直接 import（无模块级副作用）；
    # server.py 不 import（会建 FastAPI app），改成把它的 code_fingerprint 源码
    # 拎到隔离命名空间里 exec —— 零副作用，但确实是真跑一遍函数。
    import ast
    import hashlib
    import importlib.util
    spec = importlib.util.spec_from_file_location("_wbhub_launch_fp", BASE / "launch.py")
    lmod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lmod)

    ns = {"ROOT": BASE, "STATIC_DIR": BASE / "static", "HUB_DIR": BASE.parent / "engine"}
    exec(compile(raw_fn(SERVER, "code_fingerprint"), "<server.code_fingerprint>", "exec"), ns)
    got_launch = lmod.code_fingerprint()
    got_server = ns["code_fingerprint"]()
    check(got_server == got_launch,
          "server.py 与 launch.py 两个实现算出同一个值（%s）" % got_launch)

    def manual(files):
        h = hashlib.sha1()
        for p in files:
            try:
                h.update(p.read_bytes())
            except OSError:
                h.update(b"?")
        return h.hexdigest()[:12]

    three = (BASE / "server.py", BASE / "static" / "index.html",
             BASE.parent / "engine" / "engine.py")
    check(got_launch == manual(three), "指纹 == 手算（server.py + index.html + engine.py）")
    check(got_launch != manual(three[:2]), "含 engine.py 后指纹确实变了（不是摆设）")
    check((BASE.parent / "engine" / "engine.py").exists(), "engine.py 路径解得对")

    # 🔴 `bootstrap()` 必须在 `__main__` 守卫里：放模块级的话，任何 `import server`
    # 都会起一个调度线程 → 将来有脚本 import 它就会「第二个调度器同到点触发任务」。
    tree = ast.parse(SERVER)
    mod_calls = [getattr(n.value.func, "id", getattr(n.value.func, "attr", "?"))
                 for n in tree.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    check("bootstrap" not in mod_calls,
          "server.py 不在模块级调 bootstrap()（否则一 import 就起调度线程）")
    main_src = re.search(r'if __name__ == "__main__":[\s\S]*', SERVER)
    check(main_src is not None and "bootstrap()" in main_src.group(0),
          "bootstrap() 在 __main__ 守卫内（`pythonw server.py` 行为不变）")

    # 🔴 指纹必须**启动时快照**。health 里现算的话，实例报的永远是「此刻磁盘内容」，
    # 与 launch.py 现算必然相等 → code_is_newer() 恒 False → 「✔ 代码是最新的」变成废话。
    mh = re.search(r'@app\.get\("/api/health"\)[\s\S]*?(?=\n@app\.|\n@)', SERVER)
    hb = mh.group(0) if mh else ""
    check(hb != "", "能取到 /api/health 路由体")
    check("CODE_FINGERPRINT_AT_START" in hb, "health 报的是「启动时快照」")
    check(re.search(r'"code":\s*code_fingerprint\(\)', hb) is None,
          "health 不再现算 code_fingerprint()")
    check(re.search(r"^CODE_FINGERPRINT_AT_START\s*=\s*code_fingerprint\(\)",
                    SERVER, re.M) is not None,
          "模块级确实建立了启动快照常量")

    # 🔴 服务名是**跨文件硬编码**的耦合：launch.py 用 APP_TAG 判断「8793 上跑的是不是我」
    # （probe_health 里 `d.get("service") == APP_TAG`），而那个值来自 server.py 的 SERVICE_NAME。
    # 只改一边 → launch.py 认不出自己启动的服务 → 误判「端口被别的程序占着」。
    m = re.search(r'^SERVICE_NAME\s*=\s*"([^"]+)"', SERVER, re.M)
    sname = m.group(1) if m else None
    m2 = re.search(r'^APP_TAG\s*=\s*"([^"]+)"', LAUNCH, re.M)
    atag = m2.group(1) if m2 else None
    check(sname is not None and atag is not None, "两边都能取到服务名常量")
    if sname and atag:
        check(sname == atag,
              "server.py SERVICE_NAME 与 launch.py APP_TAG 一致（%s）—— 只改一边就认不出自己" % sname)

    # 引擎目录必须与 server.py 的 HUB_DIR 指向同一个地方，否则服务加载不到引擎
    mh2 = re.search(r'^HUB_DIR\s*=\s*ROOT\.parent\s*/\s*"([^"]+)"', SERVER, re.M)
    hdir = mh2.group(1) if mh2 else None
    check(hdir == "engine", "server.py 的 HUB_DIR 指向 tasks/engine（实际：%s）" % hdir)
    check((BASE.parent / "engine").is_dir(), "tasks/engine 目录存在")


def main():
    for fn in (test_syntax, test_clear_fired_today, test_stdout_levels,
               test_no_blocking, test_next_poll, test_frontend_cn,
               test_upstream_intact, test_launch_hint,
               test_fingerprint_and_bootstrap):
        fn()

    print("\n" + "=" * 70)
    if failed:
        print("❌ %d 项未通过：" % len(failed))
        for f in failed:
            print("   - " + f)
        print("（通过 %d 项）" % passed)
        return 1
    print("✅ 全部通过（%d 项）—— wb-hub 审计修复均未回退" % passed)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    sys.exit(main())
