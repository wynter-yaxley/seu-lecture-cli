---
name: seu-lecture
description: >
  东南大学（SEU）本科生人文素质讲座：查讲座、对照课表筛出不冲突的场次、在预约开放时刻自动预约并在
  “我的预约”里核实。通过 seu-lecture CLI 操作用户本人的真实账号。Use when the user asks about SEU
  lectures: "人文讲座", "讲座预约", "约讲座", "东大讲座", "seu-lecture".
---

# seu-lecture

`seu-lecture` 操作的是**用户本人的真实教务账号**。查询没有副作用；`reserve` 会向学校提交真实预约，约上不去可能记违约。
下面的规则优先于“尽快帮用户约上”。字段、事件、全部错误码和可调设置见 [reference.md](reference.md)。

未安装时（`seu-lecture --version` 失败）：`uv tool install git+https://github.com/wynter-yaxley/seu-lecture-cli`。
需要校园网或学校 VPN 客户端。

## 硬性规则

1. **只用 `--json`**，按 stdout 的 JSON 和退出码判断结果；stderr 只当日志。
2. **不碰凭据**：不索要、复述、记录密码；不读凭据管理器、`credentials.json`、`SEU_LECTURE_PASSWORD`、Cookie。
   需要登录时让用户自己在终端运行 `seu-lecture auth login -u <学号>`（WSL / 服务器等没有凭据管理器的机器加
   `--insecure-storage`）。用户主动给出短信验证码时，可执行 `auth login -u <学号> --sms-code <验证码>`。
3. **预约逐场获得明确同意**：用户针对**这一场**说了“约”，才能运行不带 `--dry-run` 的 `reserve`。
   同意前先展示 ID 前 6 位、标题、举办时间、地点、开放时间、余量、课表检查结果。不替用户“保底多约”，不重复约，
   不为测试而约。本工具没有取消功能，也不要用其他方式取消。
4. **课表结果照实对待**：只有 `schedule.status == "free"` 才算不冲突；`unknown` 要把 `reason` 告诉用户。
   `--ignore-schedule` 只在用户看过原因、明确要求时使用。
5. **不擅自改设置**：参数、配置文件、环境变量都用默认值；只有用户明确要求改某一项时才改，改前说明影响（尤其是请求量）。
   满员捡漏 `--watch <分钟>` 默认关闭，开启后约每 2 秒请求一次，只在用户要求时加。
6. **不高频请求，避免风控**：不要反复调用 `list` / `show` / `reservations` 去盯余量；`status` 间隔 ≥ 30 秒；
   被限流（`RATE_LIMITED` / `rate_limited`）就停下来告诉用户。不要用循环反复调用 `reserve`。
7. **如实报告**：只有 `confirmed` / `already_reserved` 算约上；`dry_run` 不是预约；`unconfirmed` 既不是成功也不是失败。

## 流程

1. **登录状态**：`seu-lecture auth status --json`。`session_valid` 或 `password_saved` 为 `true` 即可继续，否则按规则 2。
2. **找讲座**：`seu-lecture list --free --json`（与课表确定不冲突的）；全部加冲突说明用 `list --schedule --json`；
   单场 `show <id> --schedule --json`（id 可用 ≥6 位前缀）；课表 `schedule --json`。
3. **启动预约**（用户同意后，每场一条，可以同时挂多场）：
   ```bash
   seu-lecture reserve <id> --yes --detach --json
   ```
   它先登录、复查课表，然后转到后台并立即返回一行 `{"event": "detached", "pid", "unit", "log", "status"}`；
   后台任务等到开放时刻提交并核实，**不依赖 Agent 继续运行**（有 systemd 的 Linux 上是独立的临时用户服务 `unit`，
   Agent 或其服务重启也不受影响，结束后自动消失）。推荐在开放前 5–30 分钟启动；电脑不能休眠、WSL 不能关闭；
   开放已过则立即提交。
   想先演练就把 `--yes` 换成 `--dry-run`（不提交）。若这条命令直接返回了 `error`（如课表冲突、未登录），按错误码处理。
4. **查进度**：`seu-lecture status --json`（只读本地文件）。`data.tasks[]` 每场一项：`running`（看 `phase`、`target_time`）、
   `finished`（看 `result.outcome`）、`error`（看 `error.code`）、`stale`（进程意外退出）。
5. **核实**：结束后用 `seu-lecture reservations --json` 确认，再告诉用户。

## 常见结果

| 结果 | 应对 |
|---|---|
| `confirmed` / `already_reserved` | 告诉用户已约上 |
| `full` / `closed` / `conflict` | 报告；不自动重试（捡漏由用户决定） |
| `not_open` | 核对 `reserve_start`，问用户是否重新启动 |
| `unconfirmed` / `aborted` / `stale` | 用 `reservations --json` 核实；**不要再次 `reserve`** |
| `failed` | 报告 `message` |
| `AUTH_REQUIRED` / `AUTH_FAILED` / `SESSION_EXPIRED` | 请用户自己 `auth login` |
| `SMS_REQUIRED` | 短信已发出，向用户要验证码后带 `--sms-code` 重试一次 |
| `SCHEDULE_CONFLICT` / `SCHEDULE_UNKNOWN` | 把冲突课程或原因告诉用户 |
| `TASK_RUNNING` | 这场已有任务在跑，用 `status` 查看 |
| `NETWORK_*` / `RATE_LIMITED` / `PROTOCOL_ERROR` | 稍后至多重试一次；仍失败就报告（可能不在校园网或接口变了） |

## 能力边界

- 只支持东南大学本科生人文素质讲座；不支持 WebVPN；没有取消、签到功能。
- 课表冲突只看时间重叠，不考虑校区间通勤；需要时提醒用户。
- 真实预约已验证跑通（2026-10-22）。对 `failed` / `unconfirmed` 仍要用 `reservations --json` 复核，并提醒用户必要时在网页上确认。
- 提醒用户：开放前不要在网页上打开预约验证码；约上了要按时到场签到、签退，不去可能记违约。
