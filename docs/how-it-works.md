# 工作原理与技术细节

给想了解内部机制、或要二次开发的人看。日常使用请看 [README](../README.md)。

## 课表冲突怎么判断

数据全部来自官方：

| 需要的信息 | 来源 |
|---|---|
| 当前学期、开学日期、总周数、每天上下午晚上的节数 | 课表应用 `dqxnxq.do`、`cxjcs.do` |
| 某个日期是第几教学周 | `dqzc.do`（学校自己换算） |
| 那一周实际要上的课（含单双周） | `xskcb.do` 按周查询 |
| 调课 / 停课 / 补课 | `xsdkkc.do` |
| 每一节的起止时间 | 教务处[《上课时间安排表》](https://jwc.seu.edu.cn/_upload/article/files/c3/32/c3b5a51f41ddab0e8f2e66069bf2/0ba0d8c2-fa9e-4e0e-8b4b-333d815d15b8.pdf)（2024-2025 学年秋季起实施，13 节：08:00 起，晚上 19:00–21:25） |

课表应用只公布“上午 5 节、下午 5 节、晚上 3 节”，不公布具体钟点。工具每次都用这三个数核对教务处的表，**对不上就把时间标为未知**，不硬套。

结果只有三种：

- `free`：讲座起止时间完整，而且当天没有任何课与它重叠（首尾相接不算重叠）；
- `conflict`：列出重叠的课程、时间和地点；
- `unknown`：讲座缺少结束时间、日期不在本学期教学周内、节次时间无法换算、课表查询失败……并给出原因。

`reserve` 在正式预约前会**再查一次**：只有 `free` 才会继续；`conflict` 和 `unknown` 分别以 `SCHEDULE_CONFLICT` 和 `SCHEDULE_UNKNOWN`（退出码 10）拒绝。
你确实想约时可以加 `--ignore-schedule`。不做通勤时间推算。

## 预约任务的过程

1. **准备**：登录，确认讲座存在、尚未预约，加载验证码识别模型。
2. **等待**：低频保活；开放前约 1 分钟用讲座系统返回的“系统当前时间”（`xtdqsj`）校准本机时钟（实测误差约 ±50 ms）。
   在误差区间的**最晚端**再加 10 ms 余量时发出，不做网络提前量——请求不会早于开放到达（早到要白白多走一轮），代价是最多晚约 2 倍误差加一次单程网络时间（实测合计约 0.1 秒内）。
3. **备好验证码**：开放前 3 秒开始取验证码并识别，识别不够有把握就换一张（每张取图 + 识别约 50 ms），最多 6 张。
4. **到点提交**：关键路径上只有一个请求；验证码错就立刻换一张再交，两次提交至少间隔 100 ms，每轮最多 `--max-attempts` 次。
   冗余：“未开始”在 10 秒内重试（应对学校时钟偏慢）、请求没到达服务器（断网、取验证码失败）在 30 秒内重试，都不占提交次数；
   回复不认识或提交格式被拒（且已确认没约上）时换一种提交编码、再交，最多 3 次（预检接口 `appiontCheck.do` 只做诊断记录：本科生模块里它返回 404）；
   开放前约 5 分钟会重新读取讲座，开放时间被改就跟着改；任务开始后即使程序出现意外错误，也会先核实“我的预约”再给出结果。
   每次提交都在日志里记下学校的原始回答、提交编码和验证码，任务失败也能据此排查。
5. **满员捡漏（`--watch` 开启时）**：这一轮以“满员 / 未开放 / 验证码一直不对”结束时，进入监视：每 2 秒查一次余量，有空位立即补交（每次最多 3 次提交），
   直到约上、预约截止、讲座开始或 `--watch` 分钟用完。连续失败或被限流会拉长间隔，失败过多就停止。
6. **核验**：成功必须在“我的预约”里查到。超时、断开、异常响应时先查列表：查到就算成功；**完整读完**列表且确认没有，才允许再交一次；读不全就停下来报告 `unconfirmed`。
7. **限流**：HTTP 429 或“请求过于频繁”时完整等待服务器要求的 `Retry-After`；要求超过 60 秒就停止并报告 `rate_limited`，不会缩短等待后重试。

### 验证码识别

学校验证码是 70×30 的 4 位数字图片，来自一个几百张的固定图库。ddddocr 自带两个模型，本工具直接读取模型输出、**只在数字里解码**，
两个模型一致且每一位都足够确定才算“有把握”。在 60 张真实验证码上：55 张有把握（其余 5 张正是人眼也难辨的），单张识别约 23 ms。

## JSON 输出与退出码

加 `--json` 后 stdout 只有 UTF-8 JSON（无颜色、无日志）。普通命令输出一个对象：

```json
{"schema_version":"1","ok":true,"command":"list","data":[{"id":"5f67…","start":"2026-10-23T19:00:00+08:00","end":"2026-10-23T21:30:00+08:00","schedule":{"status":"free","reason":null,"classes":[]}}]}
{"schema_version":"1","ok":false,"command":"reserve","error":{"code":"SCHEDULE_CONFLICT","message":"…","retryable":false},"exit_code":10}
```

`reserve --json` 逐行输出事件（`schedule`、`prepared`、`clock`、`armed`、`captcha_ready`、`attempt`、`watch`、`backoff`、…），最后一行是 `result` 或 `error`；
加 `--detach` 时只输出一行 `detached`（`pid`、`unit`、日志路径、任务状态）。

`--detach` 的后台任务不依赖调用它的程序：有 systemd 的 Linux（含开了 systemd 的 WSL）上，它是独立的临时用户服务
（`systemd-run --user --collect`），Agent 或其服务重启都不影响，结束后自动消失；其他系统上是脱离终端的独立进程。
任务日志和状态在结束一周后自动清理。电脑休眠、WSL 关闭或重启会中断任务（`status` 显示 `stale`）。
`outcome` 取值：`confirmed`、`already_reserved`、`dry_run`、`full`、`conflict`、`closed`、`not_open`、`rate_limited`、`unconfirmed`、`failed`、`aborted`。

| 退出码 | 含义 |
|---|---|
| 0 | 成功（含 confirmed / already_reserved / dry_run） |
| 1 | 内部错误，或预约失败且已核实未约上 |
| 2 | 用法或配置错误 |
| 3 | 需要登录 / 认证失败 / 需要短信验证码 |
| 4 | 网络错误或超时 |
| 5 | 学校接口返回了无法识别的内容 |
| 6 | 讲座不存在 |
| 7 | 这场讲座已有预约任务在运行 |
| 10 | 预约被拒或被阻止：满员、时间冲突、截止、未开放、限流、与课表冲突或无法判断 |
| 11 | 结果无法确认，请用 `seu-lecture reservations` 检查 |
| 12 | 需要确认（写操作必须显式加 `--yes`） |
| 130 | 被中断 |

## 配置与凭据

- **凭据存在哪**（参照 `gh auth login` 的做法）：

  | 方式 | 何时用 | 说明 |
  |---|---|---|
  | 系统凭据管理器（默认） | Windows / macOS / 有桌面的 Linux | Windows 凭据管理器、macOS 钥匙串、Secret Service |
  | 本地文件 `--insecure-storage` | WSL、服务器、容器等没有凭据管理器的机器 | **明文** JSON，权限 600，仅本人可读；位置 `~/.local/state/seu-lecture/credentials.json`（Windows 在 `%LOCALAPPDATA%\seu-lecture\`）；`auth logout` 删除 |
  | 环境变量 `SEU_LECTURE_PASSWORD` | CI、一次性容器 | 不落盘 |

  不做“加密文件”：密钥和密文在同一台机器上，加密并不提供真正的保护，反而让人误以为安全。
- 有 TGT 时各应用（讲座、门户、课表）用 `verifyTgt` 换票，不重复输入密码。
- 可选配置文件：`%APPDATA%\seu-lecture\config.toml`（Windows）/ `~/.config/seu-lecture/config.toml`（Linux）：

```toml
[reserve]
offset_ms = 10          # 最晚可能开放时刻之后的余量
max_attempts = 8        # 每轮提交上限
watch_minutes = 0       # 满员后继续监视的分钟数，0 关闭（每 2 秒一次请求，按需开启）
watch_interval_s = 2    # 监视时查询余量的间隔

[captcha]
length = 4              # 验证码位数，0 表示不限
```

## 实测记录（真实系统）

| 项目 | 状态 |
|---|---|
| 登录（保存的密码 → CAS）、TGT 复用 | ✅ |
| 讲座列表、举办起止时间（`JZSJ` / `HDJSSJ`）、余量 | ✅ |
| 课表：学期、教学周、按周课程、调课记录、冲突过滤 | ✅ |
| 验证码获取与识别 | ✅ 60 张样本中 55 张“有把握”；正式提交时第一张即识别正确 |
| 时钟 | ✅ 讲座系统 `xtdqsj` 与本机相差几十毫秒，误差约 ±50 ms；`*.seu.edu.cn` 的 HTTP `Date` 头快约 375 秒，故不使用 |
| `reserve --detach` 后台运行 + `status` | ✅ 命令数秒内返回，后台任务到点触发；WSL 上模拟“调用方服务被停止”，任务照常完成 |
| 提交接口编码 | ✅ 用不存在的讲座 ID + 错误验证码探测：读取表单字段 `wid` / `vcode`；只发 `paramJson` 回复“非法参数” |
| 真实预约提交与核实 | ✅ 已跑通：按时提交（调度误差 0 ms），约 0.4 s 收到成功回复 `{"msg":"","datas":1,"code":0}`，约 0.5 s 在“我的预约”中核实。开放前约 3 秒取的验证码到点仍有效；等待期间的保活超时只记警告，任务不受影响 |
| “我的预约”字段 | ✅ `WID` 与 `HD_WID` 都是讲座 ID，`YY_WID` 是预约记录 ID |
| 满员捡漏（`--watch`） | ⏳ 仅在模拟系统中验证 |
| `--insecure-storage` | ✅ 自动测试（权限 600、登出删除） |

已知限制：不支持校外 WebVPN 网关；CAS 要求图形验证码时需先在浏览器登录一次；作息时间表若调整且节数不变，需更新 `core/schedule.py` 中的表。

## 开发

```bash
uv sync && uv run pytest && uv run ruff check .   # 测试全部基于 Mock，不访问学校系统
```

```
src/seu_lecture/
├── cli.py / output.py      # 命令行与 JSON 输出
├── api.py                  # 业务 API（CLI 之外的前端也可直接调用）
├── config.py               # 配置、状态文件、任务锁
└── core/
    ├── auth.py             # CAS：任意受保护地址的登录（TGT 或密码）、短信、凭据存储
    ├── client.py           # HTTP 客户端与接口地址
    ├── lecture.py          # 讲座 / 预约解析与分页
    ├── schedule.py         # 课表与冲突判断
    ├── clock.py            # 业务时钟偏差估计、精确计时与调度
    ├── captcha.py          # 验证码提取与 OCR
    ├── reserve.py          # 预约状态机与核验
    └── errors.py
tests/                      # 全部基于 fake_campus.py（模拟的校园系统），不访问学校
```
