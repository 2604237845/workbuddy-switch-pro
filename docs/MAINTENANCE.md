# 维护指南（MAINTENANCE）

> 写给「以后要改这个项目的人」——包括几个月后的你自己。
>
> 这个项目的特殊性：**任务清单是别人家的服务端决定的**，官方随时可能改活动、上新活动、下线活动。
> 所以维护性不是「代码写得漂亮」，而是**能不能在官方一变就快速跟上、并且不把已跑通的逻辑改坏**。
> 下面每条约束都是为了这件事。

---

## 1. 三层架构：为什么这么分

```
service/   常驻服务（定时触发 / 日志 / 面板）      ← 只管「什么时候跑」
   ↓ 加载
engine/    补丁层（把上游行为修正成实测正确的形状）  ← 只管「怎么跑对」
   ↓ monkey-patch，运行时不改文件
vendor/    上游原始脚本（任务清单 / 请求封装）       ← 只管「跑什么」
```

**依赖方向单向**：`vendor` 不知道上面两层存在；`engine` 不 import `service`。
好处是任何一层都能单独替换。**不要把三层混在一起**——一旦 `engine` 里开始直接写业务请求，
上游一更新你就得重新对齐，维护成本立刻翻倍。

---

## 2. 🔴 铁律（违反过的，都出过事故）

1. **`vendor/` 一个字节都不许改。**
   上游更新时是**整文件替换**，你改过的地方会被冲掉、还会产生冲突。
   要改行为，就在 `engine.py` 里**运行时打补丁**。
   `selftest.py` 与 `selftest_engine.py` 都有一项在盯这个（比对 sha）。

2. **补丁一律写成 `if hasattr(mod, "xxx")` 的形式。**
   官方下线某个功能、上游删掉某个函数时，补丁应该**静默跳过**而不是抛异常把整轮任务炸掉。

3. **改完必须跑三套自检**，三项都得是 `✅ 全部通过`：
   ```bash
   python tasks/engine/selftest.py           # 上游同步护栏
   python tasks/engine/selftest_engine.py    # 引擎接线（305 项）
   python tasks/service/selftest_hub.py      # 服务层（69 项）
   ```
   🔴 **改完 `engine.py` 必须 `selftest_engine.py`；改完 `server.py` 必须 `selftest_hub.py`。**
   这两套自检锁住了历史上所有踩过的坑，是唯一能防止「改 A 坏 B」的东西。

4. **改完 `engine.py` 或 `server.py` 必须重启服务**（不是可选）。
   引擎模块被 `EngineBridge.load()` **缓存在内存里**，不重启的话定时任务跑的还是旧代码。
   ```bash
   tasks/service/restart.bat
   tasks/service/start.bat   # 里面会执行 status 确认
   ```
   重启后必须看到 `✔ 代码是最新的`。**看不到就是没生效，别往下走。**

5. **判定成功只看「回读的真实进度」**，不看 HTTP 状态码、也不看自己日志里的 ✅。
   这个服务端对**所有**错误形状都回 `HTTP 200 {"code":0,"msg":"OK"}`——
   `200 OK` **完全不是成功证据**。唯一标准是回读任务列表的 `status` / `progress`。

6. **不要靠抓包定位问题。**
   桌面客户端会自解析代理、直连上游（对话走 WebSocket），抓包软件**抓不到**它。
   正确信息源是两个：客户端本地日志 `~/.workbuddy/logs/<日期>/*.log`
   （找 `[TelemetryDebug] report code=... payload={...}`，那是**完整报文原文**），
   以及 `app.asar` 里的前端源码。

---

## 3. 场景 A：官方改了某个任务的行为（最常见）

症状：某任务卡在 `accepted 0/1`，或进度一直不动。

处理顺序：

1. **先确认账号与链路**：`python tasks/engine/engine.py --list`。
2. **看真实报文**：`~/.workbuddy/logs/` 里找 `[TelemetryDebug] report`，
   拿到官方客户端**真实发出的**事件形状。
3. **比对上游发的形状**：多半是「信封 vs 裸数组」这类形状差异
   （本项目多数修复都属于这一类：形状对了，服务端才计分）。
4. **写补丁**：在 `engine.py` 加一个 `apply_xxx_fix(mod, cfg)`，
   用 `if hasattr` 包住，并在 `config/tasks.json` 里加一个独立开关（便于出问题时单独关掉）。
5. **单变量验证**：一次只改一个变量，改完**回读真实进度**确认。
   🔴 **不要一次改多个变量然后宣布「因为 X 所以好了」**——
   那样得到的只是「改完好了」，不是根因。要根因就设计单变量实验。
6. **补一条自检**：把新发现写进 `selftest_engine.py`，防止以后回退。

> 上游若也修了同一个问题：同步上游后，你的补丁应该变成多余。
> 这时**先跑自检 + 实测**，再决定是否删掉补丁（不要凭感觉删）。

---

## 4. 场景 B：官方**下线**了某个活动

这是「维护性」最关键的场景。当前设计已经能自动扛住大部分情况：

1. **补丁自动失效**：所有补丁都写成 `if hasattr(mod, "xxx")`，
   上游删掉对应函数后补丁静默跳过 → **不会报错，其余任务照跑**。
2. **服务端任务列表里没有了**：`prog()` 查不到该任务 → 进度判定自然跳过。
3. **上游同步会带下来变化**：`update.py` 同步上游后，任务清单自动更新。

需要**人工确认**的两处：

- `config/tasks.json` 里若为已下线任务留了**独立开关**（如 `fix_library_report`），
  可以保留不管（无害），也可以在确认上游已自带修复后删掉。
- **自检里若有针对该任务的断言**，可能因为函数消失而失败 → 那是**自检在提醒你**，
  去把对应断言更新掉，而不是把自检删掉。

---

## 5. 场景 C：官方**上新**了活动

1. **先看有没有被检测到**：`engine.py` 内置了「未覆盖任务检测」，
   会把服务端下发、但脚本不认识的任务**列出来**（不会静默忽略）。
2. **拿到真实报文**：从客户端本地日志抓该活动的事件形状。
3. **判断归属**：
   - 签到 / 旅行这类「上游已经在做」的 → 加进 `exclude_functions`，**不要重复做**。
   - 其余 → 新增一个 `t_xxx` 补丁。
4. **按场景 A 的第 4~6 步走**（补丁 + 开关 + 单变量验证 + 自检）。

---

## 6. 上游同步机制

`vendor/` 的两个文件来自 **[L0NE-6/WorkBuddy-Daily](https://github.com/L0NE-6/WorkBuddy-Daily)**，
由 `engine/update.py` 同步，目标写在 `config/tasks.json` 的 `auto_update.repo`。

同步带**安全护栏**（同样在 `tasks.json` 里，别关掉）：

- 体积区间：`min_bytes` / `max_bytes`
- 体积突变比例：`max_size_delta_ratio`
- **必需函数存在性**：`run_account` / `t_sign` / `t_travel` / `new_api` / `report` / `claim`
  （上游若重构导致函数改名，会**拒绝同步**而不是把引擎改坏）
- 域名白名单：`allowed_hosts`
- 语法必须能编译

护栏拦下时会告警。**看到告警先人工比对**，确认上游是真重构还是被投毒。

---

## 7. 补丁清单

`engine.py` 里 `bootstrap()` 按顺序应用。每个都是独立函数、独立开关：

| 补丁 | 作用 | 对应开关 |
|---|---|---|
| `apply_exclusions` | 把上游已做的任务（签到 / 旅行）替换成空操作，避免重复请求 | `exclude_functions` |
| `apply_desktop_safety` | 桌面类任务改用**纯 API 指纹上报**，绝不 `taskkill` 桌面端、不改写认证文件 | `desktop_mode` / `desktop_scope` |
| `apply_report_shape_fix` | 桌面事件上报：信封 → **裸数组** + 补 `timestamp/reportDelay` + **注入客户端身份信封** | `fix_report_shape` |
| `apply_black_cat_fix` | 夜猫子：上游会**每晚硬发 8 次真实对话**（控制流不退出）→ 改为有界重试 + 当夜到账即停 + 本夜去重 | `fix_black_cat` |
| `apply_web_report_fix` | 资料库（`Library_read`）：信封 → 裸数组 + 三个必需请求头 | `fix_library_report` |
| `apply_mini_report_fix` | 小程序对话：信封 → 裸数组 | `fix_mini_report` |
| `apply_school_activity_fix` | 开学季（含幸运大转盘） | `school_activity` |
| `apply_post_claim_recheck` | 领奖后复查 + 全量兜底领奖（服务端是延迟入账的，领奖必须补扫） | `post_claim_recheck` / `final_claim_sweep` |

> ⚠️ **不要盲目把某个任务的结论跨任务推广。**
> 例如「注入客户端身份信封」这一条对桌面类任务有效，但对走 `webchat` 路径的任务就**不需要**——
> 每个任务都要单独取证，`selftest_engine.py` 里有断言在钉这一点。

---

## 8. 排查工具

| 工具 | 用途 |
|---|---|
| `python engine.py --list` | 不联网，列账号与将执行的任务 |
| `python engine.py`（默认） | 只读演练，拦截所有非 GET |
| `probe_*.py` | 定向探针，每个文件头部写了背景与用法；`probe_all_tasks.py` 可盘点全部任务的真实状态 |
| `~/.workbuddy/logs/` | **客户端真实报文**（唯一可靠的一手信息源） |
| 网页面板 | 实时日志流，最直观 |

`probe_richmeow.py` 是**单变量阶梯实验**的范本：
同一账号、同一分钟内顺序试臂，**每臂只比上一臂多一个变量**，每臂后回读真实状态。
需要定位「到底哪个字段是关键」时照这个模式来。

> 📌 探针脚本里的 `WB_PROBE_*` 环境变量是**可选的**：任务本身不需要真实设备指纹
> （引擎发的 `machineId` 是按账号 uid 派生的），只有重跑 arm2/arm3 实验才需要填。

---

## 9. 发布 / 提交前检查（本仓库特有）

因为这是**公开仓库**，每次提交前确认没有夹带个人数据：

```bash
# 1) 开发机绝对路径
grep -rIn --exclude-dir=.git -E "C:\\\\Users\\\\[^\\\\]+|/Users/[^/]+/" .
# 2) 真实设备指纹 / token / 账号
grep -rIn --exclude-dir=.git -E "eyJhbGciOi[A-Za-z0-9_-]{20,}" .
# 3) 敏感文件是否被正确忽略
git check-ignore -v tasks/engine/vendor/WORKBUDDY_ACCESS_TOKEN.txt \
                    tasks/service/_private/server.log
# 4) 确认暂存区没有不该有的东西
git status --short
```

`.gitignore` 已覆盖：`accounts.json`、`tasks/*/logs/`、`tasks/service/_private/`、
`WORKBUDDY_ACCESS_TOKEN.txt`、`black_cat_nights.json`、`.venv/`、`__pycache__/`。

🔴 **`.bat` 文件必须是纯 ASCII + CRLF。**
cmd.exe 用 OEM 代码页，UTF-8 中文会让它**粘行、执行错乱**。
本项目改成英文提示就是为了这个（校验方法：数非 ASCII 字节 == 0，且 CRLF 行数 == LF 行数）。
