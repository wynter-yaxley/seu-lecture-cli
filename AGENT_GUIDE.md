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
5. **不改参数去“抢得更快”。** 默认参数已按实测调好；不调 `--offset-ms`、`--max-attempts`、`--clock`、`--no-prefetch`、`--watch`，
   也不改配置文件，除非用户要求。
   不要并行启动多个 `reserve`（会得到 `TASK_RUNNING`），不要用循环反复调用 `reserve`。
6. **不高频轮询。** 学校系统会限流（HTTP 429）。查询命令按需调用即可；监控任务用 `status --json`，间隔不短于 30 秒。
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
开放时没约上（满员 / 未开放）时，它默认继续监视余量最多 60 分钟，有人退约就补交，所以进程可能运行很久；
`status` 里 `phase` 为 `watch` 表示正在监视。用户不想等时可加 `--watch 0`。

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
| `full` / `closed` | 10 | 满员（监视期内也没等到空位）/ 已截止 | 报告；不要重试 |
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

## 能力边界

- 只支持东南大学**本科生人文素质讲座**；必须在校园网内或已连接学校 VPN 客户端，不支持 WebVPN。
- 没有取消预约、签到、自动发现开放后批量预约等功能。
- 课表冲突只看时间重叠，不考虑校区之间的通勤时间；需要时提醒用户。
- 截至 2026-10-21，真实预约提交尚未完成验收（见 README“实测记录与验收状态”）。在此之前，对 `failed`、`unconfirmed`
  这类结果尤其要用 `reservations --json` 复核，并提醒用户自己在网页上确认。
