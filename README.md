<div align="center">

# seu-lecture-cli

**东南大学本科生人文素质讲座预约命令行工具（非官方）**

对照课表筛讲座 · 定时预约 · 结果可核验 · 适合 Agent 调用

[![CI](https://github.com/wynter-yaxley/seu-lecture-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/wynter-yaxley/seu-lecture-cli/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

简体中文 | [English](README-EN.md)

</div>

> [!WARNING]
> 本项目是**非官方**的个人开源工具，与东南大学无关。请遵守学校规定，只用**自己的账号**预约**自己会去听**的讲座；
> 不保证一定约得到，学校接口随时可能变化。

## 它能做什么

- **对照课表筛讲座**：读取“我的课表”，按讲座**实际举办时间**判断是否和上课冲突（处理单双周、调课、停课、补课）。信息不全时明确标“无法判断”，绝不当作无冲突。
- **定时预约**：常驻进程提前登录、预热连接，按讲座系统自己的时钟在开放时刻提交；开放前就备好一张“有把握”的验证码，到点只发一个请求。
- **满员捡漏（可选）**：加 `--watch 30` 后，开放时没约上（满员 / 未开放）会继续监视余量 30 分钟，有人退约立刻补交。默认关闭，因为它会持续发请求。
- **结果核验**：只有在“我的预约”里查到才算成功；结果不明时先查再说，不盲目重复提交。
- **稳定的 JSON**：所有命令支持 `--json`，有统一的错误码和退出码。
- **凭据安全**：密码和 CAS 登录态只存系统凭据管理器；始终校验 TLS 证书。

## 安装

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。必须在**校园网**内，或先连上学校 VPN 客户端。

```bash
uv tool install git+https://github.com/wynter-yaxley/seu-lecture-cli
# 或从源码运行：git clone … && cd seu-lecture-cli && uv sync && uv run seu-lecture --help
```

验证码识别依赖 [ddddocr](https://github.com/sml2h3/ddddocr)，首次安装约需下载 150 MB。

## 上手

```bash
seu-lecture auth login -u 213xxxxxx     # 输入密码；陌生设备会要短信验证码
seu-lecture list --free                 # 只看和课表不冲突的讲座
seu-lecture list --schedule             # 全部讲座 + 冲突说明
seu-lecture schedule                    # 本周课表（真实日期和时间）；--week 6 / --date 2026-10-30
seu-lecture show 5f6766 --schedule      # 单场详情，ID 可写 ≥6 位前缀
seu-lecture reserve 5f6766 --dry-run    # 演练：走完全部流程但不提交
seu-lecture reserve 5f6766              # 正式预约（会先确认）；在预约开放前几分钟启动
seu-lecture reservations                # 我的预约
```

## 命令

| 命令 | 说明 | 写入学校系统 |
|---|---|---|
| `auth login` / `auth status` / `auth logout` | 登录（支持短信二次验证）、状态、清除保存的凭据 | 否 |
| `list` | 讲座列表；`-k` 关键词、`--available`、`--schedule`、`--free` | 否 |
| `show <id>` | 讲座详情；`--schedule` 对照课表，`--raw` 原始字段 | 否 |
| `schedule` | 某一教学周的课表（默认本周） | 否 |
| `reservations` | 我的预约 | 否 |
| `reserve <id>` | 定时预约 + 核验 | **是**（需确认或 `--yes`） |
| `status` | 最近一次 / 正在运行的预约任务 | 否 |

`reserve` 常用参数：`--at`（开放时间，默认取讲座的“预约开始”）、`--dry-run`、`--yes`、`--ignore-schedule`（跳过课表复查）、
`--watch`（满员后继续监视的分钟数，默认 0 = 关闭）、`--offset-ms`（最晚可能开放时刻之后的余量，默认 10）、
`--clock auto|local`、`--max-attempts`（每轮提交上限，默认 8）、`--no-prefetch`。

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
4. **到点提交**：关键路径上只有一个请求；验证码错就立刻换一张再交，“未开始”在 3 秒内重试，两次提交至少间隔 100 ms，每轮最多 `--max-attempts` 次。
5. **满员捡漏（`--watch` 开启时）**：这一轮以“满员 / 未开放 / 验证码一直不对”结束时，进入监视：每 2 秒查一次余量，有空位立即补交（每次最多 3 次提交），
   直到约上、预约截止、讲座开始或 `--watch` 分钟用完。连续失败或被限流会拉长间隔，失败过多就停止。
6. **核验**：成功必须在“我的预约”里查到。超时、断开、异常响应时先查列表：查到就算成功；**完整读完**列表且确认没有，才允许再交一次；读不全就停下来报告 `unconfirmed`。
7. **限流**：HTTP 429 或“请求过于频繁”时遵守 `Retry-After`（最长 60 秒），不会硬顶。

### 验证码识别

学校验证码是 70×30 的 4 位数字图片，来自一个几百张的固定图库。ddddocr 自带两个模型，本工具直接读取模型输出、**只在数字里解码**，
两个模型一致且每一位都足够确定才算“有把握”。在 60 张真实验证码上：55 张有把握（其余 5 张正是人眼也难辨的），单张识别约 23 ms。

## 给脚本和 Agent 用

加 `--json` 后 stdout 只有 UTF-8 JSON（无颜色、无日志）。普通命令输出一个对象：

```json
{"schema_version":"1","ok":true,"command":"list","data":[{"id":"5f67…","start":"2026-10-23T19:00:00+08:00","end":"2026-10-23T21:30:00+08:00","schedule":{"status":"free","reason":null,"classes":[]}}]}
{"schema_version":"1","ok":false,"command":"reserve","error":{"code":"SCHEDULE_CONFLICT","message":"…","retryable":false},"exit_code":10}
```

`reserve --json` 逐行输出事件（`schedule`、`prepared`、`clock`、`armed`、`captcha_ready`、`attempt`、`watch`、`backoff`、…），最后一行是 `result` 或 `error`。
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
| 7 | 已有另一个预约任务在运行 |
| 10 | 预约被拒或被阻止：满员、时间冲突、截止、未开放、限流、与课表冲突或无法判断 |
| 11 | 结果无法确认，请用 `seu-lecture reservations` 检查 |
| 12 | 需要确认（写操作必须显式加 `--yes`） |
| 130 | 被中断 |

推荐分工：Agent 负责查询和读取 `status --json`；用户明确同意后用 `reserve <id> --yes --json` 启动独立进程完成定时提交。
**让 Agent 调用本工具前，请把 [AGENT_GUIDE.md](AGENT_GUIDE.md) 放进它的上下文**：其中规定了必须遵守的约束、标准流程和每种结果的处理方式。
非交互登录用 `--password-stdin`；遇到 `SMS_REQUIRED` 时短信已发出，带 `--sms-code` 重试。业务逻辑在 `seu_lecture.api.LectureService`。

## 配置与凭据

- 密码和 CAS 登录态（TGT）只存系统凭据管理器（Windows 凭据管理器 / macOS 钥匙串 / Secret Service）。没有安全存储时不保存，可用 `SEU_LECTURE_PASSWORD`。
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

## 实测记录与验收状态（2026-10-21）

| 项目 | 状态 |
|---|---|
| 真实登录（保存的密码 → CAS）、TGT 缓存复用 | ✅ 已实测 |
| 真实讲座列表、举办起止时间（`JZSJ`/`HDJSSJ`）、余量 | ✅ 已实测 |
| 真实课表：学期、教学周、按周课程、调课记录、冲突过滤 | ✅ 已实测 |
| 真实验证码获取与识别 | ✅ 已实测：60 张样本，取图 17–39 ms；55 张判为有把握、与人工核对一致（以前记录的“把 1 读成 7”是人工标注错误：图库里没有 1，那是本字体的 7）；准确率的最终依据要等真实提交 |
| 时钟 | ✅ 讲座系统 `xtdqsj` 与本机相差约 −17 ± 49 ms；而所有 `*.seu.edu.cn` 的 HTTP `Date` 头快约 375 秒（前置网关），故不使用 |
| 全流程演练（`--dry-run`，真实系统，不提交） | ✅ 登录 → 课表复查 → 时钟校准 → 开放前 3 秒备好验证码 → 到点触发（调度误差 0 ms） |
| **真实预约提交与“我的预约”核验** | ⏳ **待验收**（尚未提交过真实预约；`addReservation.do` 的参数编码和各种响应文案未经真实验证） |
| 满员捡漏（监视余量后补交） | ⏳ 仅 Mock 验证 |
| 预取验证码在开放时刻是否仍有效 | ⏳ 未知（可用 `--no-prefetch` 关闭） |

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

## 致谢

独立实现，未复制第三方代码。协议信息参考了 [Warma10032/fetch_lecture](https://github.com/Warma10032/fetch_lecture)（MIT；其 `seu_auth.py` 为 GPL-3.0，未复制）、
[MuZiJin701/seu-lecture-reserve-http](https://github.com/MuZiJin701/seu-lecture-reserve-http)、[candyb0x/SEU-Lecture-Revenge](https://github.com/candyb0x/SEU-Lecture-Revenge)，
以及学校课表应用自身的前端代码。

## 许可证

[MIT](LICENSE)
