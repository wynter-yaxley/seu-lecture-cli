# Agent 使用约束：seu-lecture

本文件写给代替用户调用 `seu-lecture` 的 AI Agent（Claude Code、Codex 等）。把它放进 Agent 的上下文即可。
人类用户请看 [README](README.md)。

`seu-lecture` 操作的是**用户本人的真实教务账号**。查询没有副作用；`reserve` 会向学校系统提交真实预约，
约上后若不去，可能被记为违约。下面的规则优先于“尽快帮用户约上”。

## 硬性规则

1. **只用 `--json`。** 解析 stdout（UTF-8 JSON），结合退出码判断结果；不要解析人类可读的表格，stderr 只当日志看。
2. **不碰凭据。**
   - 不在对话里索要、复述或记录密码；不读取系统凭据管理器、`SEU_LECTURE_PASSWORD`、Cookie 或状态目录里的会话数据。
   - 需要登录时（`AUTH_REQUIRED` / `AUTH_FAILED` / `SESSION_EXPIRED` 重试后仍失败），让用户自己在终端运行
     `seu-lecture auth login -u <学号>`，不要替用户输入密码。
   - 用户主动在对话里给出短信验证码时，可以执行 `auth login -u <学号> --sms-code <验证码>`。
3. **预约必须逐场获得用户明确同意。**
   - 只有用户针对**这一场讲座**说了“约”，才可以运行不带 `--dry-run` 的 `reserve`。“帮我看看有什么讲座”“挑个合适的”都不算同意。
   - 同意前先给用户看：ID、标题、举办时间、地点、预约开放时间、余量、课表检查结果。
   - 不替用户“多约几场保底”，不重复预约同一场，不为测试而预约。本工具没有取消命令，也不要尝试用其他方式取消。
4. **课表结果按原样对待。**
   - `schedule.status` 只有 `free` 才算无冲突。`unknown` 不是 `free`，要把 `reason` 告诉用户。
   - `--ignore-schedule` 只能在用户看过冲突或“无法判断”的原因后、明确要求时使用。
5. **不擅自改设置。** 默认值已按实测调好。下文[“可自定义设置”](#可自定义设置)里的每一项（命令行参数、配置文件、环境变量），
   只有用户明确要求改**那一项**时才能改；改之前先用一句话告诉用户影响，尤其是请求量会不会变大。
   不要为了“抢得更快”自行调整，也不要并行启动多个 `reserve`（会得到 `TASK_RUNNING`）或用循环反复调用 `reserve`。
6. **不高频轮询，避免账号被风控。** 学校系统会限流（HTTP 429），短时间大量请求也可能引起风控。查询命令按需调用即可；
   `status --json` 只读本地文件、不访问学校，但间隔也不短于 30 秒；不要反复调用 `list` / `show` / `reservations` 去“盯余量”。
   被限流（`RATE_LIMITED` / `rate_limited`）后不要立即重试，把情况告诉用户。
7. **如实报告。** 只有 `confirmed` 和 `already_reserved` 才算约上；`dry_run` 不是预约；`unconfirmed` 不是成功也不是失败。
   不要把演练、Mock 测试或“已提交”说成“已约上”。

## 标准流程

### 1. 检查登录

```bash
seu-lecture auth status --json
```

`data.session_valid` 为 `true`，或 `data.password_saved` 为 `true`，就可以继续；否则按规则 2 请用户登录。

### 2. 找候选讲座

```bash
seu-lecture list --free --json                   # 与课表确定不冲突的讲座
seu-lecture list --schedule --json               # 全部讲座 + 冲突说明
seu-lecture list --free --available -k 戏曲 --json
seu-lecture show <id> --schedule --json          # 单场详情；id 可用 ≥6 位唯一前缀
```

讲座对象的主要字段：

| 字段 | 含义 |
|---|---|
| `id` | 讲座 ID（`reserve` / `show` 用它） |
| `title`、`location`、`category` | 标题、地点、类别 |
| `start`、`end` | 举办起止时间（ISO 8601，+08:00）；`end` 可能为 `null` |
| `reserve_start`、`reserve_end` | 预约开放 / 截止时间 |
| `capacity`、`reserved`、`remaining` | 容量、已约、余量 |
| `state` | `open` 可预约 / `not_open` 未开放 / `full` 已满 / `closed` 已截止 / `unknown` |
| `schedule` | 带 `--schedule` / `--free` 时出现：`{status: free\|conflict\|unknown, reason, classes}` |

给用户列候选时，至少包含 `id` 前 6 位、标题、`start`–`end`、地点、`reserve_start`、`remaining`、`schedule.status`。

### 3. 获得同意后启动预约

`reserve` 是**常驻进程**：启动后先登录、复查课表，然后一直等到 `reserve_start`，到点提交，再去“我的预约”核验。
应在开放前约 5–10 分钟启动；若开放时间已过，它会立即提交。
满员捡漏默认**关闭**：开放时没约上就结束。只有用户明确要求捡漏时才加 `--watch <分钟>`，并先告诉用户：
监视期间约每 2 秒请求一次学校系统（30 分钟约 900 次）。开启后进程可能运行很久，`status` 里 `phase` 为 `watch` 表示正在监视。

可选：先演练一次，不提交，只确认登录、课表复查、时钟校准和验证码识别都正常。

```bash
seu-lecture reserve <id> --dry-run --json
```

正式预约（非交互环境必须加 `--yes`，否则返回 `CONFIRMATION_REQUIRED`，退出码 12）：

```bash
# Linux / macOS：放到后台，事件流写入文件
nohup seu-lecture reserve <id> --yes --json > reserve-<id>.jsonl 2>&1 &

# Windows PowerShell
Start-Process -WindowStyle Hidden seu-lecture -ArgumentList 'reserve','<id>','--yes','--json' `
  -RedirectStandardOutput reserve.jsonl -RedirectStandardError reserve.err
```

如果 Agent 的工具调用有超时限制，一定要放到后台，不要让前台调用一直等到开放时刻。

`--json` 下 stdout 每行一个事件，常见的有 `schedule`、`prepared`、`clock`、`armed`、`captcha_ready`、`attempt`、`watch`（余量变化）、`backoff`；
最后一行是 `{"event": "result", "outcome": ...}` 或错误对象。

### 4. 监控与核验

```bash
seu-lecture status --json          # 任务状态，间隔 ≥30 秒
seu-lecture reservations --json    # “我的预约”，最终以这里为准
```

`status` 的 `data.state`：`idle` 没有任务；`running` 进行中（看 `phase`、`target_time`）；
`finished` 已结束（结果在 `result`）；`error` 出错（看 `error.code`）；`stale` 进程已退出但没写结果，用 `reservations` 核实。

## 结果与应对

`reserve` 的 `outcome`：

| outcome | 退出码 | 含义 | Agent 应该 |
|---|---|---|---|
| `confirmed` | 0 | 已在“我的预约”里查到 | 告诉用户已约上 |
| `already_reserved` | 0 | 之前就约过 | 告诉用户，不要再约 |
| `dry_run` | 0 | 演练完成，未提交 | 说明这只是演练 |
| `full` / `closed` | 10 | 满员（开了 `--watch` 则是监视期内也没等到空位）/ 已截止 | 报告；是否捡漏由用户决定 |
| `conflict` | 10 | 学校判定与已约讲座时间冲突 | 报告；不要重试 |
| `not_open` | 10 | 重试窗口内一直未开放 | 核对 `reserve_start` 后问用户是否重新启动 |
| `rate_limited` | 10 | 被学校限流 | 报告；不要立即重试 |
| `unconfirmed` | 11 | 提交了但无法确认结果 | 稍后运行 `reservations --json` 核实；**不要再次 `reserve`** |
| `failed` | 1 | 已核实没约上 | 报告原因（`message`） |
| `aborted` | 130 | 被中断 | 用 `reservations --json` 核实 |

命令级错误（`ok: false` 时的 `error.code`）：

| error.code | 退出码 | Agent 应该 |
|---|---|---|
| `AUTH_REQUIRED` / `AUTH_FAILED` / `SESSION_EXPIRED` | 3 | 请用户自己运行 `auth login` |
| `SMS_REQUIRED` | 3 | 短信已发出；向用户要验证码，带 `--sms-code` 重试一次 |
| `NETWORK_ERROR` / `NETWORK_TIMEOUT` | 4 | 查询可稍后重试一次；提示用户检查校园网 / VPN |
| `RATE_LIMITED` | 4 | 停止调用，稍后再试 |
| `PROTOCOL_ERROR` / `CAPTCHA_ERROR` | 5 | 学校接口可能变了，报告给用户，不要反复重试 |
| `NOT_FOUND` | 6 | 检查 ID 或前缀 |
| `TASK_RUNNING` | 7 | 已有预约任务在跑，用 `status --json` 查看 |
| `SCHEDULE_CONFLICT` / `SCHEDULE_UNKNOWN` | 10 | 把冲突课程或原因告诉用户，由用户决定 |
| `CONFIRMATION_REQUIRED` | 12 | 确认用户已同意后，才可加 `--yes` |
| `CONFIG_ERROR` | 2 | 检查参数 |

## 可自定义设置

改任何一项都要遵守硬性规则 5：用户明确要求才改，改前说明影响。“请求量”一栏是相对默认值的变化。

### `reserve` 命令行参数

| 参数 | 默认 | 作用 | 请求量 / 风险 |
|---|---|---|---|
| `--yes` / `-y` | 无 | 确认提交（非交互必须） | 写操作：逐场获得同意后才加 |
| `--dry-run` | 关 | 全流程演练，不提交 | 仍会登录、校准时钟、取验证码 |
| `--at <ISO 时间>` | 讲座的 `reserve_start` | 指定开放时间（时间改了、或演练时用） | 无 |
| `--watch <分钟>` | 0（关闭） | 满员 / 未开放后继续监视余量并补交 | **大**：约每 2 秒 1 次请求 |
| `--ignore-schedule` | 关 | 跳过课表复查 | 可能约到与上课冲突的讲座 |
| `--offset-ms <毫秒>` | 10 | 最晚可能开放时刻之后再等多久发出 | 调小可能早到 → 浪费一轮 |
| `--clock auto\|local` | auto | 按讲座系统时钟校准 / 只用本机时钟 | local 会少 8 次校准请求，但时间不准 |
| `--max-attempts <1-20>` | 8 | 每轮最多提交次数 | 调大 → 提交更多 |
| `--no-prefetch` | 关 | 不在开放前备好验证码 | 开放后才取图，变慢 |

通用参数：`--json`、`--config <文件>`（换配置文件）、`--verbose`（stderr 打印调试信息）。

### 配置文件 `config.toml`

位置：Windows `%APPDATA%\seu-lecture\config.toml`，Linux `~/.config/seu-lecture/config.toml`（或 `--config` / `SEU_LECTURE_CONFIG` 指定）。
命令行参数优先于配置文件。`[reserve]` 里写错键名会报 `CONFIG_ERROR`。

`[reserve]`（取值范围由程序校验，越界报 `CONFIG_ERROR`）：

| 键 | 默认 | 作用 |
|---|---|---|
| `offset_ms` | 10 | 同 `--offset-ms` |
| `clock` | `"auto"` | 同 `--clock` |
| `compensate_latency` | false | 按往返时间提前发出；可能早到，不建议开启 |
| `prefetch_captcha` | true | 开放前备好验证码（`--no-prefetch` 关闭） |
| `captcha_lead_ms` | 3000 | 开放前多久开始取验证码 |
| `captcha_tries` | 6 | 开放前最多取几张来找“有把握”的（1–20） |
| `clock_sync_lead_s` | 60 | 开放前多久校准时钟 |
| `session_check_lead_s` | 20 | 开放前多久最后检查一次登录状态 |
| `keepalive_s` | 300 | 等待期间保活间隔（≥30） |
| `max_attempts` | 8 | 同 `--max-attempts` |
| `max_resubmits` | 1 | 结果不明且已确认没约上时，最多再交几次（0–3） |
| `min_submit_interval_ms` | 100 | 两次提交最小间隔（≥50） |
| `not_open_window_ms` | 3000 | 收到“未开始”后继续重试多久 |
| `rate_limit_backoff_s` | 5 | 被限流且服务器没给等待时间时，等多久（0–60） |
| `verify_attempts` | 3 | 核验“我的预约”最多查几次（1–10） |
| `verify_interval_ms` | 500 | 核验间隔 |
| `watch_minutes` | 0 | 同 `--watch` |
| `watch_interval_s` | 2 | 监视时查余量的间隔（≥1）；调大可减少请求 |

其他小节：

| 键 | 默认 | 作用 |
|---|---|---|
| `[captcha] length` | 4 | 验证码位数，0 = 不限；学校改了位数时才动 |
| `[network] connect_timeout` / `read_timeout` | 5 / 10 秒 | 网络超时 |
| `[protocol] submit_format` | `"form"` | 提交格式，`form` 或 `paramJson`；学校接口变了才动 |
| `[protocol] module` | `"hdyy_bk"` | 讲座应用模块名；学校接口变了才动 |
| `base_url` | 学校地址 | 只用于连 Mock 测试服务器，Agent 不要设置 |

### 环境变量

| 变量 | 作用 |
|---|---|
| `SEU_LECTURE_USERNAME` | 学号（替代已保存的账号） |
| `SEU_LECTURE_PASSWORD` | 密码（无凭据管理器时用）；**Agent 不读取、不设置、不在对话中出现** |
| `SEU_LECTURE_CONFIG` | 配置文件路径 |
| `SEU_LECTURE_HOME` | 配置和状态目录（测试隔离用） |
| `SEU_LECTURE_BASE_URL` | 只用于 Mock 测试，Agent 不要设置 |

## 能力边界

- 只支持东南大学**本科生人文素质讲座**；必须在校园网内或已连接学校 VPN 客户端，不支持 WebVPN。
- 没有取消预约、签到、自动发现开放后批量预约等功能。
- 课表冲突只看时间重叠，不考虑校区之间的通勤时间；需要时提醒用户。
- 截至 2026-10-21，真实预约提交尚未完成验收（见 README“实测记录与验收状态”）。在此之前，对 `failed`、`unconfirmed`
  这类结果尤其要用 `reservations --json` 复核，并提醒用户自己在网页上确认。
