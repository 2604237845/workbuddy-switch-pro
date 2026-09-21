# 任务中心（`tasks/`）

把 WorkBuddy 的**每日成长任务**自动做完，并提供常驻服务 + 网页面板 + 定时调度。

本目录是本仓库新增的部分；账号管理由上游 [workbuddy-switch](../README.md) 负责。

---

## 1. 它做什么

上游已经做了「切号 / 每日签到 / 猫猫旅行 / 积分监控」，**本任务中心只做剩下的部分**，两边互补不重复：

| 类别 | 具体任务 |
|---|---|
| 成长中心 | 设计画布 · 探索灵感 · 专家团 · 主题 · 资料库 · 模板 · GLM-5.2 对话 · 夜猫子 |
| 互动玩法 | 抽奖 · 盲盒 · 连签兑换 · 补签卡 · 礼包补偿 · 徽章 |
| 活动 | 开学季（含幸运大转盘）· 校园日 |
| 小程序 | 小程序对话任务 |
| 其他 | 桌面端对话（RichMeow）· 专家使用 · 热门技能 · 自动领奖 · 未覆盖任务检测 |

> 每日签到（`t_sign`）与猫猫旅行（`t_travel`）被**显式排除**，因为上游已经在做。
> 排除清单在 `engine/config/tasks.json` 的 `exclude_functions`，想改随时改。

---

## 2. 前置条件

- **Python 3.10+**（用了 `from __future__ import annotations` 与新语法）
- **至少一个已登录的 WorkBuddy 账号**，且账号库文件存在：
  `~/.wb-switch/accounts.json`（Windows：`C:\Users\<你>\.wb-switch\accounts.json`）
  - 这个文件由**上游的 App 或 webui** 生成。先把上游跑起来、登录账号，再回来用本任务中心。
- 依赖只有 4 个：`requests` / `urllib3` / `fastapi` / `uvicorn`

---

## 3. 快速开始

### 3.1 装依赖（推荐虚拟环境）

```bash
cd workbuddy-switch-pro
python -m venv .venv
.venv\Scripts\pip install -r tasks\requirements.txt      # Windows
# source .venv/bin/activate && pip install -r tasks/requirements.txt   # macOS/Linux
```

> 脚本会**自动找解释器**，优先级：环境变量 `WB_TASKS_PYTHON` → 仓库根的 `.venv` → `PATH` 里的 `python`。
> 所以放在 `.venv` 里最省事，不用改任何文件。

### 3.2 用法 A：命令行跑一轮（最简单）

| 脚本 | 作用 |
|---|---|
| `tasks/engine/check.bat` | **只列账号与将要执行的任务，不联网**（先跑这个确认认得账号） |
| `tasks/engine/dry-run.bat` | **只读演练**：拦截所有非 GET 请求，不产生任何实际变化 |
| `tasks/engine/run.bat` | **真实执行**：需要手输 `YES` 确认 |
| `tasks/engine/watch.bat` | 账号切换守望者（**已内置进服务**，这是备胎） |

等价的手工命令：

```bash
python tasks/engine/engine.py --list        # 不联网，只列
python tasks/engine/engine.py              # 只读演练（默认）
python tasks/engine/engine.py --live       # 真实执行
```

### 3.3 用法 B：常驻服务 + 网页面板（推荐）

```bash
tasks/service/start.bat      # 启动（后台、无窗口）
# 浏览器打开 http://127.0.0.1:8793
tasks/service/stop.bat       # 停止
tasks/service/restart.bat    # 重启
```

面板能力：实时日志、手动跑一轮、账号勾选、定时计划开关、守望者开关。

**服务自带定时调度**（不需要 WorkBuddy 自己的自动化功能）：

- `night_owl` **23:30** —— 夜猫子任务只在 23:00–08:00 计数，所以安排在窗口内跑一轮
- `daily_full` **08:30** —— 每日全量跑一遍

计划文件是 `tasks/service/config/schedule.json`，**首次启动自动生成**，之后可在网页改。

> Windows 想开机自启：`python tasks/service/install_task.py` 注册计划任务（需管理员）。

---

## 4. 目录结构

```
tasks/
├── engine/                     # ① 任务引擎（补丁层）
│   ├── engine.py               #    核心：加载上游 + 运行时打补丁（唯一的“业务大脑”）
│   ├── vendor/                 #    上游原样文件（🔴 一个字节都不要改）
│   │   ├── workbuddy_daily.py
│   │   └── workbuddy_login.py
│   ├── config/
│   │   ├── tasks.json          #    全部开关与排除清单（改完下次运行即生效，无需重启）
│   │   └── upstream.json       #    上游文件的 sha 记录（同步护栏的基线）
│   ├── update.py               #    上游同步器（带安全护栏）
│   ├── watch.py                #    独立守望者（服务不在时的备胎）
│   ├── selftest.py             #    上游同步护栏自检
│   ├── selftest_engine.py      #    引擎自检（305 项）
│   ├── probe_*.py              #    诊断探针（排查任务为什么不计数时用）
│   └── *.bat                   #    check / dry-run / run / watch
└── service/                    # ② 常驻服务 + 网页面板
    ├── server.py               #    FastAPI：REST API + 调度器 + 守望者
    ├── launch.py               #    启动/停止/状态（找 PID、验身份、比代码指纹）
    ├── static/index.html       #    网页面板（单文件）
    ├── selftest_hub.py         #    服务自检（69 项）
    └── config/schedule.json    #    定时计划（首次启动生成）
```

三层职责（**这是本项目最重要的一张图，改动前务必理解**）：

```
service/  常驻服务：定时触发、日志、面板            ← 只管“什么时候跑”
   ↓ 加载
engine/   补丁层：把上游行为修正成实测正确的形状      ← 只管“怎么跑对”
   ↓ 运行时 monkey-patch（不改文件）
vendor/   上游原始脚本：任务清单、请求封装           ← 只管“跑什么”
```

依赖方向是**单向**的：`engine` 不 import `service`，`vendor` 不知道上面两层存在。
所以任何一层都可以单独替换。**维护方法见 [../docs/MAINTENANCE.md](../docs/MAINTENANCE.md)。**

---

## 5. 配置

全部在 `engine/config/tasks.json`，带中文注释。最常用的几项：

| 键 | 说明 |
|---|---|
| `accounts_source` | 账号库路径，默认 `~/.wb-switch/accounts.json`（**只读**） |
| `exclude_domains` | 按域名过滤账号（默认排除国际版 `workbuddy.ai` / `codebuddy.ai`） |
| `exclude_functions` | **排除哪些任务**（默认排除上游已做的 `t_sign` / `t_travel`） |
| `desktop_mode` | `fingerprint`（默认，纯 API 上报，**绝不碰桌面端进程**）/ `off` / `full`（禁用） |
| `desktop_scope` | `all`（默认，每个账号都建立桌面会话）/ `current`（只做当前登录账号） |
| `write_gap` | 写请求间隔秒数，太低容易触发频控 |
| `dry_run_default` | **默认 `true` = 只读**。改成 `false` 才会默认真实执行 |
| `auto_update` | 上游同步开关 + 安全护栏（体积、必需函数、域名白名单） |
| `fix_*` / `skill_real_chat` / `final_claim_sweep` | 各项修复的独立开关，出问题时可以逐个关掉定位 |

改完**不用重启**任何东西，下次运行即生效。

---

## 6. 安全与隐私（重要）

- **只读账号库**：只从 `~/.wb-switch/accounts.json` 读 access token，
  **绝不刷新 token**（上游的 refresh token 是单链轮换的，两边同刷会互相踢下线）。
- **不需要也不使用真实设备指纹**：
  引擎发的 `machineId` 是**按账号 uid 派生**的（`md5("machine:<uid>")`，见 `vendor` 的 `derive_id`），
  与你的真实机器无关 —— 所以**换台电脑跑照样有效，无需任何设备配置**。
  （`probe_richmeow.py` 里那几个 `WB_PROBE_*` 只是诊断实验用的可选项，留空不影响任务。）
- **默认只读**：`dry_run_default: true` 时所有非 GET 请求被拦截，跑一轮不会产生任何变化。
  真实执行必须显式 `--live` 或输 `YES`。
- **不碰桌面端进程**：默认 `desktop_mode=fingerprint`，走纯 API 上报建立桌面会话，
  **不会** 去 `taskkill` WorkBuddy、也不会改写桌面端认证文件。
- 本仓库**不含任何凭据**：token、账号、日志、`_private/` 均已 gitignore。

---

## 7. 自检（改代码后必跑）

```bash
python tasks/engine/selftest.py           # 上游同步护栏（8 项）
python tasks/engine/selftest_engine.py    # 引擎接线与安全约束（305 项）
python tasks/service/selftest_hub.py      # 服务层（69 项）
```

全部应输出 `✅ 全部通过`。这三套自检是**长期维护的安全网**：
它们锁住了大量「曾经踩过的坑」，改代码后跑一遍能立刻发现回退。

改完 `engine.py` 记得**重启服务**（引擎模块会被缓存），否则定时任务跑的还是旧代码。

---

## 8. 常见问题

**Q：跑完显示完成 15/19，剩下的是没做吗？**
有 3 项是**人工硬门槛**，脚本做不了：连接器授权（腾讯轻量云专家）、公益捐款、微信扫码关注公众号。
另有 1 项「夜猫子」有时间窗（23:00–08:00），白天跑会跳过。所以 15/19 是正常上限。

**Q：任务一直卡在 `accepted 0/1` 不计数？**
先用 `check.bat` 确认账号认得，再跑 `dry-run.bat` 看链路。仍不行就用 `probe_*.py` 定向排查
（每个探针文件头部都写了背景与用法）。**注意：不要靠抓包**——桌面客户端会自解析代理直连，
抓包抓不到，请改看客户端本地日志 `~/.workbuddy/logs/`。

**Q：能不能只跑某几个任务？**
可以，在 `tasks.json` 的 `exclude_functions` 里列出来即可（把不想要的排掉）。

**Q：会不会重复请求 / 和上游打架？**
不会。账号库只读、签到与旅行已排除，且定时计划与上游的自动化互不依赖。

**Q：`launch.py status` 说「跑的是旧代码」？**
说明改了代码没重启。执行 `tasks/service/restart.bat`，再 `status` 确认变成「✔ 代码是最新的」。
