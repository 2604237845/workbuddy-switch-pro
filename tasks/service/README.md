# 常驻服务 + 网页面板（`tasks/service`）

把 `tasks/engine` 的任务引擎包成一个**常驻后台服务**，自带定时调度与实时日志面板。

> 任务中心的总体说明（装什么、怎么用、有哪些任务）在 **[../README.md](../README.md)**；
> 本文件只讲服务层这一块的细节。

**核心目的**：让定时任务**不再依赖 WorkBuddy 客户端**。你完全可以退出 WorkBuddy、
关掉所有终端，服务和它的计划任务照常运行 —— 包括每晚 23:30 收「夜猫子」。

---

## 快速开始

```
双击 start.bat      启动（无窗口后台运行）
双击 stop.bat       停止
双击 restart.bat    重启
```

启动后浏览器打开 **http://127.0.0.1:8793**

首次部署（让服务开机/登录自动跑）：

```
python install_task.py          注册计划任务（用户登录时自动启动）
python install_task.py --run    立刻拉起一次
python install_task.py --status 查看任务状态
python install_task.py --remove 卸载计划任务
```

---

## 页面能做什么

| 卡片 | 内容 |
|---|---|
| **运行状态** | 当前是否在跑、跑的是哪一轮、上次结果（完成 N/19）、累计次数 |
| **定时计划** | 计划列表（夜猫子 23:30 / 每日全量 08:30）、下次运行倒计时；可改时间、开关单条、改完即时生效 |
| **切换账号守望者** | 盯桌面端登录的账号，一切换就自动给新账号补跑一轮。可开关、改参数、手动「立即检查一次」 |
| **账号与任务** | 7 个账号的完成进度、**待领数量（>0 会标红，是异常信号）**、等级/能量；当前桌面端账号带「桌面端」标记；可展开看任务明细；可单独「跑这个」账号 |
| **实时日志** | SSE 流式输出，边跑边看；支持筛选、清空 |

页面底部有「立即跑一轮」按钮，真实执行会弹确认框（真实执行是危险动作，需显式确认）。

页面为浅色主题。

---

## 定时任务怎么定的

配置在 `config/schedule.json`，**改完即时生效，不用重启**：

```json
{
  "enabled": true,       // 总开关
  "live": true,          // true=真实执行；false=只读演练(dry-run)
  "no_update": true,     // 定时跑时跳过上游静默同步，免得被网络拖住
  "jobs": [
    { "id": "night_owl",  "at": "23:30", "only": null, "days": [0,1,2,3,4,5,6] },
    { "id": "daily_full", "at": "08:30", "only": null, "days": [0,1,2,3,4,5,6] }
  ]
}
```

- **夜猫子**只在北京时间 23:00–次日 08:00 计数，所以安排在 23:30 全量跑一轮。
- `days` 里 `0`=周一 … `6`=周日。
- 调度器每 20 秒检查一次；**错过超过 10 分钟就不补跑**（`GRACE_SECONDS`），
  避免开机瞬间把攒下的任务一次全补上、把账号打爆。
- 已触发记录按「任务 id → 当天时间点」去重，同一时间点不会重复触发。

**调度完全由本服务自己实现**，跟 WorkBuddy 的自动化功能无关。原来那条
WorkBuddy 定时任务（23:30 夜猫子）已删除 —— 两条同时跑会打同一批账号，容易触发风控。

---

## 切换账号守望者

桌面端（WorkBuddy 客户端）**切到哪个账号，就自动给那个账号补跑一轮**，不用手点。

- 只盯桌面端认证文件的 `account.uid`，**只读，绝不替你切账号**；认不出 uid 就什么都不做。
- uid 变化后先等 `debounce_seconds` 稳定再动手 —— 切换过程中认证文件可能被写好几次，
  直接触发会重复补跑。
- 同一账号两次补跑最小间隔 `min_gap_minutes`；全量轮次正在跑时自动让路。
- 轮询异常被兜住并计数（`falseAlarms`），守望者不会因为一次网络抖动就死掉。

参数在 `config/schedule.json` 的 `watch` 段，页面也能改：

```json
"watch": {
  "enabled": true,
  "poll_seconds": 5,
  "debounce_seconds": 30,
  "min_gap_minutes": 120,
  "run_on_start": false
}
```

接口：`GET /api/watch`（状态）、`POST /api/watch`（开关）、`POST /api/watch/check`（立即检查一次）、
`POST /api/watch/config`（改参数）。

> **守望者已内置进本服务**，`tasks/engine/watch.py` 只是服务不在时的备胎：
> 它启动时会探 `/api/watch`，如果**服务版正在跑就自己退出**，避免两个守望者同时补跑同一个账号
> （引擎之间没有互斥锁，重复跑除了浪费还可能触发风控）。确实要单独跑请加 `--force`。

---

## 架构

```
tasks/service/                ← 服务层（本目录）
├─ server.py                  FastAPI：API + 调度器 + 守望者 + 日志中心
├─ static/index.html          单页前端（浅色，SSE 实时日志）
├─ launch.py                  启动/停止器（无窗口、PID 自愈、端口自检、代码指纹比对新旧）
├─ install_task.py            注册/卸载 Windows 计划任务
├─ start.bat / stop.bat / restart.bat   （纯 ASCII + CRLF，禁止写中文）
├─ selftest_hub.py            服务层自测（9 组 / 69 项）
├─ config/schedule.json       调度 + 守望者配置（首次启动按默认值自动生成）
├─ logs/                      运行日志
└─ _private/                  状态目录（server.pid / server.log）

tasks/engine/                 ← 引擎层（被服务直接 import 调用）
├─ engine.py                  任务引擎（run() 可被服务层调用）
├─ watch.py                   守望者备胎（默认让路给服务版）
├─ vendor/workbuddy_daily.py  上游脚本，原样保留
├─ update.py                  上游静默同步 + 安全护栏
└─ selftest_engine.py         引擎自测（305 项）
```

服务**直接 import** 引擎（不走子进程），所以日志能实时流到页面。
`engine.run(args, observer=..., dry_run_override=..., log_sink=...)` 返回结构化结果。

### 端口

| 端口 | 归属 |
|---|---|
| **8793** | **本服务（任务中心）** —— 只绑 `127.0.0.1`，**无鉴权，仅限本机访问**，不要暴露到公网 |

---

## 关键设计（都是踩过坑之后的结论）

### 1. 为什么要用计划任务，不能靠 launch.py 自己 Popen

在终端/会话里起的进程**会随该会话结束被回收**。实测：`launch.py start` 成功、
`/api/health` 也通了，但会话一结束 PID 就消失。计划任务里的进程归「任务计划服务」管，
不隶属任何登录会话，才能真正长期驻留。

### 2. launch.py 的三重保护

- **启动前探端口**：被本服务占用 → 提示已在运行；被**别的程序**占用 → 拒绝启动，绝不硬抢。
- **启动后验身份**：访问 `/api/health` 确认服务名对得上且拿到了 PID，失败打印日志尾部。
- **代码指纹比对**：比 `server.py` + `static/index.html` 的 **sha1 内容指纹**（不是 mtime！
  这个工作区里文件改写后 mtime 可能保持旧值）。指纹不一致说明在跑旧代码 → 自动重启。

### 3. 停止只按 PID，且先验身份

`stop` 会先确认 PID 确实是 python 进程，再 `taskkill /PID <pid> /F`。
**严禁 `taskkill /IM pythonw.exe /F` 这类按映像名杀** —— 会把同机上其他用 pythonw 起的无关服务一起杀掉。

PID 文件在 `_private/server.pid`，**是运行中实例的状态文件，不是缓存，不许随手删**。
真丢了也能自愈：PID 文件 → `/api/health` 自报 pid → `netstat` 端口反查，依次兜底；
且 PID 文件与 health 不一致时**以 health 为准**（PID 会被系统复用，防误杀）。

### 4. `save_schedule` 必须逐键深合并

页面点一次守望者开关，只发 `{"watch":{"enabled":true}}`。第一版实现 `cur[k] = v` 是**整段替换**，
把 `poll_seconds` / `min_gap_minutes` 等参数**全冲掉了**（配置被悄悄改成只有 `enabled`）。
现在对 `watch` 子字典逐键合并，`selftest_engine.py` 有回归断言。

### 5. 引擎服务化时的三个「一次性运行不会暴露」的坑

审计发现并修复（`selftest_engine.py` 第 18 组是回归测试）：

1. **stdout 劫持必须成对还原** —— 常驻进程里永久替换 `sys.stdout` 会吞掉 uvicorn 日志。
2. **dry-run 补丁必须可逆** —— 否则跑过一次 dry-run 之后，所有真实运行都被静默拦成假响应
   （最隐蔽也最危险的坏法）；同时每次调用清空 `BLOCKED_WRITES`，防跨运行累积。
3. **`emit=None` 要能兜住** —— 否则「已点亮、正要领奖」的关键路径会 `TypeError` 直接丢奖励。

### 6. 2026-09-20 全项目审计修掉的 5 个真 bug

`selftest_hub.py`（9 组 / 69 项）是这些修复的回归测试，**改完 server.py 必须跑它**。

1. **`LogHub.emit` 从不写 stdout** —— 启动过程**一行输出都没有**，`:8793` 起没起来、
   卡在哪一步全靠猜（我就因此把「正常启动」误判成「静默退出」，排查了半天）。
   现在按**黑名单**输出（只压掉高频的 `engine` 逐行业务日志），启动日志一秒内全部可见。
2. **`clear_fired_today()` 实现与注释完全相反** —— 原写法
   `if not self._fired[k].startswith(today): del ...` 删的是**非今天**的记录，
   调用它会**让今天的任务再也不会触发**。
3. **3 处 `async` 路由同步阻塞事件循环** —— `watch/check`（可能触发一整轮任务，跑几分钟）、
   `switch`（`timeout=60` 的 HTTP）、`watch`/`watch/config`（读桌面端 uid 的磁盘 I/O）
   全部改成 `await asyncio.to_thread(...)`。修前并发打两个请求会**串行**；
   修后实测两个路由并发响应都在 **10ms 内**。
4. **`next_poll_at()` 永远返回当前时间** —— 页面拿它做倒计时只会一直显示 0，属于误导。
   现在返回「距下次轮询还剩几秒」（基于 `_last_tick_at`）。
5. **`stop.bat` / `restart.bat` 的 `if errorlevel 2 pause` 漏了 code 1** ——
   `do_stop()` 在「本来就没在运行」时返回 1，属于**正常提示**，但窗口会一闪而过，
   看起来像「点了没反应」。三个 bat 统一改成 `if errorlevel 1 pause`。

⚠️ 顺带记一条**血泪教训**：改 `.bat` 时**绝对不要把中文写进注释**。
我这次往三个 bat 里塞了中文说明，立刻产生 90~253 个非 ASCII 字节 ——
cmd 用 OEM 代码页解析 bat，UTF-8 中文会导致**粘行、命令错乱**。
`.bat` 必须**纯 ASCII + CRLF**（`selftest_hub.py` 有对应断言思路，人工核对也别偷懒）。

---

## 安全边界（不要碰）

- 本服务**只读** `~/.wb-switch/accounts.json` 的 AT，**绝不刷新 token** ——
  refresh token 是单链轮换的，两边同时刷会互相踢掉，会让 wb-switch 的保活失效。
- 引擎自身**不含任何杀进程能力**（AST 级检查强制保证），桌面类任务走纯 API 指纹上报，
  不碰 WorkBuddy 客户端进程。
- 本服务无鉴权，**默认只绑 `127.0.0.1`**。要开局域网自行改 `server.py` 的 `HOST`，
  改完记得重启，并想清楚同网段的人都能操作这些账号。
- **只操作自己的 PID**，不要按进程名批量杀（`taskkill /IM pythonw.exe /F` 会误伤同机其他服务）；
  本机的其他服务一律不要动。

---

## 排查

```
python launch.py status              看服务状态、PID 来源、代码是否最新
type _private\server.log             看服务日志（启动异常主要看这里）
type logs\run-*.log                  看某一轮任务的完整输出
python selftest_hub.py               服务层自测（9 组 / 69 项，改完 server.py 必跑）
python selftest_engine.py            引擎自测（305 项）
python install_task.py --status      看计划任务状态
curl http://127.0.0.1:8793/api/watch 看守望者状态
```

服务起不来时按顺序查：① 端口 8793 是否被别的程序占了（`netstat -ano | findstr :8793`）；
② `_private/server.log` 有没有报错；③ 引擎能不能单独跑通（`python engine.py --list`）。

**⚠️ 最容易误判的一种情况**：日志里能看到完整启动输出（`🚀 ... 调度线程已启动 ...`、
health 也响应过）然后进程凭空消失，只剩 8793 的 `TIME_WAIT` ——
这**不是代码问题**，是「当前这个执行环境会在会话结束时回收子进程」。
要常驻只有两条路：**双击 `start.bat`**，或 **`schtasks /Run /TN WbTaskHubBoot`**。
`launch.py` 启动超时时会自动打印这段提示。
