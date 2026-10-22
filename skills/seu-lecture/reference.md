# seu-lecture 参考

[SKILL.md](SKILL.md) 的补充：字段、事件、全部结果与错误码、可自定义设置。按需查阅。

## 讲座字段（`list` / `show` 的 `data`）

| 字段 | 含义 |
|---|---|
| `id` | 讲座 ID（`reserve` / `show` 用它） |
| `title`、`location`、`category` | 标题、地点、类别 |
| `start`、`end` | 举办起止时间（ISO 8601，+08:00）；`end` 可能为 `null` |
| `reserve_start`、`reserve_end` | 预约开放 / 截止时间 |
| `capacity`、`reserved`、`remaining` | 容量、已约、余量 |
| `state` | `open` 可预约 / `not_open` 未开放 / `full` 已满 / `closed` 已截止 / `unknown` |
| `schedule` | 带 `--schedule` / `--free` 时出现：`{status: free\|conflict\|unknown, reason, classes}` |

## `reserve --json` 事件

stdout 每行一个 JSON 事件：`schedule`（课表复查）、`prepared`、`keepalive`、`final_check`（开放前约 5 分钟：
讲座最新信息 + 预检接口 `appiontCheck.do` 的原始返回）、`retarget`（开放时间被改）、`clock`（时钟校准）、`armed`、
`captcha_ready`、`phase`、`attempt`、`submit_format`、`appoint_check`、`resubmit`、`verify_raw`、`watch`、`backoff`、
`warning`（含意外错误的 `traceback`），最后一行是 `result`（含 `outcome`）或 `error`。

每个 `attempt` 都带着诊断信息：`raw`（学校的原始回答：`http_status`、`content_type`、`location`、`retry_after`、
`body` 前 4000 字；请求没发出去时是 `error`）、`format`（提交编码）、`captcha`（识别结果、置信度、验证码图片 base64）。
提交后第一次核实时的 `verify_raw` 是“我的预约”的原始回答。加 `--detach` 时只输出一行 `detached`（`pid`、`unit`（systemd 临时服务名，无 systemd 时为 null）、`log`、`status`），
事件流写进 `log` 文件。

## `status --json`

`data.tasks`：每场讲座一个任务（新的在前，结束超过一周的自动清理）。`state`：`running`（看 `phase`、`target_time`）、
`finished`（结果在 `result`）、`error`（看 `error.code`）、`stale`（进程已退出但没写结果，用 `reservations` 核实）。
`status <id>` 只看这一场。只读本地文件，不访问学校。任务日志和状态在结束一周后自动清理。

## 结果与错误码

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
| `TASK_RUNNING` | 7 | 这场讲座已有预约任务在跑，用 `status --json` 查看 |
| `SCHEDULE_CONFLICT` / `SCHEDULE_UNKNOWN` | 10 | 把冲突课程或原因告诉用户，由用户决定 |
| `CONFIRMATION_REQUIRED` | 12 | 确认用户已同意后，才可加 `--yes` |
| `CONFIG_ERROR` | 2 | 检查参数 |

## 可自定义设置

改任何一项都要遵守 SKILL.md 硬性规则 5：用户明确要求才改，改前说明影响。“请求量”一栏是相对默认值的变化。

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
| `--detach` | 关 | 转到后台运行并立即返回 | 无；Agent 启动预约时总是加 |

通用参数：`--json`、`--config <文件>`（换配置文件）、`--verbose`（stderr 打印调试信息）。

### 配置文件 `config.toml`

位置：Windows `%APPDATA%\seu-lecture\config.toml`，Linux `~/.config/seu-lecture/config.toml`（或 `--config` / `SEU_LECTURE_CONFIG` 指定）。
命令行参数优先于配置文件。`[reserve]` 里写错键名会报 `CONFIG_ERROR`。

`[reserve]`（取值范围由程序校验，越界报 `CONFIG_ERROR`）：

| 键 | 默认 | 作用 |
|---|---|---|
| `offset_ms` | 10 | 同 `--offset-ms` |
| `clock` | `"auto"` | 同 `--clock` |
| `prefetch_captcha` | true | 开放前备好验证码（`--no-prefetch` 关闭） |
| `captcha_lead_ms` | 3000 | 开放前多久开始取验证码 |
| `captcha_tries` | 6 | 开放前最多取几张来找“有把握”的（1–20） |
| `clock_sync_lead_s` | 60 | 开放前多久校准时钟 |
| `session_check_lead_s` | 20 | 开放前多久最后检查一次登录状态 |
| `keepalive_s` | 300 | 等待期间保活间隔（≥30） |
| `max_attempts` | 8 | 同 `--max-attempts` |
| `max_resubmits` | 3 | 结果不明或回复不认识、且已确认没约上时，最多再交几次（0–5） |
| `min_submit_interval_ms` | 100 | 两次提交最小间隔（≥50） |
| `not_open_window_ms` | 10000 | 收到“未开始”后继续重试多久（不占提交次数；应对学校时钟偏慢） |
| `not_open_pause_ms` | 300 | “未开始”重试间隔（≥100） |
| `outage_window_s` | 30 | 请求没到达服务器（断网、取验证码失败）时继续重试多久，不占提交次数（0–120） |
| `final_check_lead_s` | 300 | 开放前多久最后检查一次讲座（开放时间是否改了）并记录预检接口的原始返回 |
| `rate_limit_backoff_s` | 5 | 被限流且服务器没给等待时间时，等多久（0–60） |
| `verify_attempts` | 5 | 核验“我的预约”最多查几次（1–10） |
| `verify_interval_ms` | 1000 | 核验间隔 |
| `watch_minutes` | 0 | 同 `--watch` |
| `watch_interval_s` | 2 | 监视时查余量的间隔（≥1）；调大可减少请求 |

其他小节：

| 键 | 默认 | 作用 |
|---|---|---|
| `[captcha] length` | 4 | 验证码位数，0 = 不限；学校改了位数时才动 |
| `[network] connect_timeout` / `read_timeout` | 5 / 10 秒 | 网络超时 |
| `[protocol] submit_format` | `"form"` | 提交编码：`form`（实测可用）、`both`、`paramJson`（实测被拒）；不要改 |
| `base_url` | 学校地址 | 只用于连 Mock 测试服务器，Agent 不要设置 |

### 环境变量

| 变量 | 作用 |
|---|---|
| `SEU_LECTURE_USERNAME` | 一卡通号（替代已保存的账号） |
| `SEU_LECTURE_PASSWORD` | 密码（CI / 容器用）；**Agent 不读取、不设置、不在对话中出现** |
| `SEU_LECTURE_CONFIG` | 配置文件路径 |
| `SEU_LECTURE_HOME` | 配置和状态目录（测试隔离用） |
| `SEU_LECTURE_BASE_URL` | 只用于 Mock 测试，Agent 不要设置 |
