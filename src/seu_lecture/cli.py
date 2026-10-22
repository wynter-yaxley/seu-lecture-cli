"""Typer command-line front end. All business logic lives in ``api`` / ``core``."""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from seu_lecture import __version__
from seu_lecture.api import LectureService
from seu_lecture.config import load_settings
from seu_lecture.core.errors import (
    AuthRequired,
    ConfigError,
    ConfirmationRequired,
    SeuLectureError,
)
from seu_lecture.core.lecture import SHANGHAI
from seu_lecture.core.reserve import OUTCOME_EXIT, Outcome, ReserveOptions
from seu_lecture.output import EXIT_CODES, Output

HELP = ("东南大学本科生人文素质讲座预约 CLI（非官方）。\n\n"
        "所有命令支持 --json。退出码：" + "，".join(str(k) for k in EXIT_CODES)
        + "（含义见 docs/how-it-works.md）。")

app = typer.Typer(help=HELP, no_args_is_help=True, add_completion=False,
                  pretty_exceptions_enable=False, rich_markup_mode=None)
auth_app = typer.Typer(help="登录、登出与认证状态", no_args_is_help=True)
app.add_typer(auth_app, name="auth")

JsonOpt = Annotated[bool, typer.Option("--json", help="机器可读 JSON 输出（reserve 为 JSONL 事件流）")]
ConfigOpt = Annotated[Path | None, typer.Option("--config", help="配置文件路径（TOML）")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="出错时在 stderr 打印调试信息")]


def _version(value: bool) -> None:
    if value:
        typer.echo(f"seu-lecture {__version__}")
        raise typer.Exit()


@app.callback()
def root(version: Annotated[bool, typer.Option("--version", callback=_version, is_eager=True,
                                               help="显示版本")] = False) -> None:
    """东南大学本科生人文素质讲座预约 CLI（非官方）。"""


def _interactive(out: Output) -> bool:
    return not out.json_mode and sys.stdin.isatty()


def _service(out: Output, config: Path | None) -> LectureService:
    interactive = _interactive(out)

    def ask_password() -> str | None:
        return getpass.getpass("统一身份认证密码：") if interactive else None

    def ask_sms(info: str) -> str | None:
        if not interactive:
            return None
        out.info(f"[yellow]{escape(info)}[/]")
        return typer.prompt("短信验证码")

    return LectureService(load_settings(config), password_prompt=ask_password, sms_prompt=ask_sms)


def _run(command: str, json_mode: bool, verbose: bool, body: Callable[[Output], int | None],
         *, stream: bool = False) -> None:
    out = Output(json_mode, verbose=verbose)
    try:
        code = body(out) or 0
    except SeuLectureError as exc:
        code = out.failure(command, exc, stream=stream)
    except KeyboardInterrupt:
        code = out.failure(command, _Interrupted("interrupted by user"), stream=stream)
    except Exception as exc:  # unexpected bug: report without leaking internals
        if verbose:
            out.err.print_exception()
        code = out.failure(command, SeuLectureError(f"unexpected {type(exc).__name__}"),
                           stream=stream)
    raise typer.Exit(code)


class _Interrupted(SeuLectureError):
    code = "INTERRUPTED"
    exit_code = 130


# ---- auth -------------------------------------------------------------------

@auth_app.command("login")
def auth_login(
    username: Annotated[str | None, typer.Option("--username", "-u", help="一卡通号/学号")] = None,
    password_stdin: Annotated[bool, typer.Option("--password-stdin",
                                                 help="从 stdin 读取密码（供脚本/Agent 使用）")] = False,
    sms_code: Annotated[str | None, typer.Option("--sms-code", help="短信验证码（非可信设备）")] = None,
    save_password: Annotated[bool, typer.Option("--save-password/--no-save-password",
                                                help="保存密码，以便会话过期后自动重新登录")] = True,
    insecure_storage: Annotated[bool, typer.Option(
        "--insecure-storage", help="没有系统凭据管理器时（WSL/服务器/容器）：明文保存到仅本人可读的文件")] = False,
    json_mode: JsonOpt = False, config: ConfigOpt = None, verbose: VerboseOpt = False,
) -> None:
    """登录统一身份认证并进入本科生讲座系统。"""

    def body(out: Output) -> int:
        svc = _service(out, config)
        interactive = _interactive(out)
        user = username or svc.settings.username
        if not user:
            if not interactive:
                raise ConfigError("--username is required in non-interactive mode")
            user = typer.prompt("一卡通号").strip()
        if password_stdin:
            password = sys.stdin.readline().rstrip("\r\n")
        else:
            password = svc._password(user, prompt=False) or (
                getpass.getpass("统一身份认证密码：") if interactive else "")
        if not password:
            raise AuthRequired("password required (interactive prompt, --password-stdin, "
                               "or SEU_LECTURE_PASSWORD)")
        data = svc.login(user, password, sms_code=sms_code, save_password=save_password,
                         insecure_storage=insecure_storage)

        def render(c: Console) -> None:
            c.print(f"[green]✓ 登录成功[/] {data['username']}")
            if not data["password_saved"]:
                c.print("[yellow]密码未保存：没有系统凭据管理器。可加 --insecure-storage 存到本地文件，"
                        "或用 SEU_LECTURE_PASSWORD。[/]")
            if not data["session_cached"]:
                c.print("[dim]会话未缓存，每次命令都会重新登录。[/]")

        return out.success("auth.login", data, render)

    _run("auth.login", json_mode, verbose, body)


@auth_app.command("status")
def auth_status(
    offline: Annotated[bool, typer.Option("--offline", help="不联网校验会话")] = False,
    json_mode: JsonOpt = False, config: ConfigOpt = None, verbose: VerboseOpt = False,
) -> None:
    """查看账号、凭据保存与会话有效性。"""

    def body(out: Output) -> int:
        data = _service(out, config).auth_status(online=not offline)

        def render(c: Console) -> None:
            yes = {True: "[green]是[/]", False: "[red]否[/]", None: "[dim]未知[/]"}
            c.print(f"账号：{data['username'] or '（未配置）'}")
            c.print(f"凭据存储：{escape(str(data['storage'] or '不可用'))}")
            c.print(f"密码已保存：{yes[data['password_saved']]}")
            c.print(f"会话已缓存：{yes[data['session_cached']]}")
            c.print(f"会话有效：{yes[data['session_valid']]}")

        return out.success("auth.status", data, render)

    _run("auth.status", json_mode, verbose, body)


@auth_app.command("logout")
def auth_logout(json_mode: JsonOpt = False, config: ConfigOpt = None,
                verbose: VerboseOpt = False) -> None:
    """删除系统凭据管理器中保存的密码与会话。"""
    _run("auth.logout", json_mode, verbose,
         lambda out: out.success("auth.logout", _service(out, config).logout(),
                                 lambda c: c.print("[green]✓ 已清除保存的密码与会话[/]")))


# ---- queries ----------------------------------------------------------------

_STATE = {"open": "[green]可预约[/]", "not_open": "[yellow]未开放[/]", "full": "[red]已满[/]",
          "closed": "[dim]已截止[/]", "unknown": "[dim]未知[/]"}


def _span(x: dict[str, Any]) -> str:
    start, end = x.get("start"), x.get("end")
    if not start:
        return escape(x.get("lecture_time") or "-")
    return _short(start) + ("–" + end[11:16] if end else "")


def _schedule_text(check: dict[str, Any]) -> str:
    if check["status"] == "free":
        return "[green]无冲突[/]"
    if check["status"] == "conflict":
        names = "；".join(f"{c['course']} {c['start']}-{c['end']}" for c in check["classes"])
        return f"[red]冲突[/] {escape(names)}"
    return f"[yellow]无法判断[/] {escape(check.get('reason') or '')}"


def _short(value: str | None) -> str:
    if not value:
        return "-"
    return value.replace("T", " ")[:16]


@app.command("list")
def list_cmd(
    keyword: Annotated[str | None, typer.Option("--keyword", "-k", help="按标题/类别筛选")] = None,
    available: Annotated[bool, typer.Option("--available", help="只显示未截止且未满的讲座")] = False,
    schedule: Annotated[bool, typer.Option("--schedule", help="按讲座举办时间对照我的课表")] = False,
    free: Annotated[bool, typer.Option("--free", help="只显示与课表确定无冲突的讲座")] = False,
    json_mode: JsonOpt = False, config: ConfigOpt = None, verbose: VerboseOpt = False,
) -> None:
    """列出本科生素质讲座（--schedule/--free 对照课表）。"""

    def body(out: Output) -> int:
        items = _service(out, config).lectures(keyword=keyword, available=available,
                                               schedule=schedule, free=free)
        with_schedule = schedule or free

        def render(c: Console) -> None:
            table = Table(title=f"讲座列表（{len(items)}）", show_lines=False)
            cols = ["ID", "标题", "举办时间", "预约开始", "余量", "状态"] + (["课表"] if with_schedule else [])
            for col in cols:
                table.add_column(col, overflow="fold")
            for x in items:
                seats = "-" if x["remaining"] is None else f"{x['remaining']}/{x['capacity']}"
                row = [x["id"][:10], escape(x["title"]), _span(x), _short(x["reserve_start"]), seats,
                       _STATE.get(x["state"], x["state"])]
                if with_schedule:
                    row.append(_schedule_text(x["schedule"]))
                table.add_row(*row)
            c.print(table)
            c.print("[dim]ID 可用前缀（≥6 位）：seu-lecture show <ID>[/]")

        return out.success("list", items, render)

    _run("list", json_mode, verbose, body)


@app.command("show")
def show_cmd(
    lecture_id: Annotated[str, typer.Argument(help="讲座 ID 或唯一前缀")],
    raw: Annotated[bool, typer.Option("--raw", help="附带学校接口原始字段")] = False,
    schedule: Annotated[bool, typer.Option("--schedule", help="对照我的课表")] = False,
    json_mode: JsonOpt = False, config: ConfigOpt = None, verbose: VerboseOpt = False,
) -> None:
    """查看单个讲座详情。"""

    def body(out: Output) -> int:
        x = _service(out, config).lecture(lecture_id, raw=raw, schedule=schedule)

        def render(c: Console) -> None:
            labels = {"id": "ID", "title": "标题", "start": "开始", "end": "结束", "location": "地点",
                      "category": "类别", "reserve_start": "预约开始", "reserve_end": "预约截止",
                      "capacity": "总名额", "reserved": "已预约", "remaining": "余量", "state": "状态"}
            for key, label in labels.items():
                value = x.get(key)
                c.print(f"[bold]{label}[/]：{_STATE.get(value, value) if key == 'state' else escape(str(value))}")
            if "schedule" in x:
                c.print(f"[bold]课表[/]：{_schedule_text(x['schedule'])}")
            if raw:
                c.print_json(data=x.get("raw"))

        return out.success("show", x, render)

    _run("show", json_mode, verbose, body)


@app.command("reservations")
def reservations_cmd(json_mode: JsonOpt = False, config: ConfigOpt = None,
                     verbose: VerboseOpt = False) -> None:
    """列出我的已预约讲座。"""

    def body(out: Output) -> int:
        items = _service(out, config).reservations()

        def render(c: Console) -> None:
            table = Table(title=f"我的预约（{len(items)}）")
            for col in ("讲座 ID", "标题", "时间", "已签到", "违约", "活动取消"):
                table.add_column(col, overflow="fold")
            flag = {True: "是", False: "否", None: "-"}
            for r in items:
                table.add_row(r["lecture_id"][:10], escape(r["title"] or "-"), escape(r["lecture_time"] or "-"),
                              flag[r["checked_in"]], flag[r["breached"]], flag[r["cancelled"]])
            c.print(table)

        return out.success("reservations", items, render)

    _run("reservations", json_mode, verbose, body)


# ---- reservation task -----------------------------------------------------------

def parse_at(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace(" ", "T", 1))
    except ValueError:
        raise ConfigError("--at must be ISO 8601, e.g. 2026-10-22T12:00:00+08:00") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=SHANGHAI)


@app.command("reserve")
def reserve_cmd(
    lecture_id: Annotated[str, typer.Argument(help="讲座 ID 或唯一前缀")],
    at: Annotated[str | None, typer.Option("--at", help="开放时间 ISO 8601；缺省取讲座的预约开始时间")] = None,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="确认执行预约写操作（非交互模式必须）")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="完整演练但不提交预约")] = False,
    offset_ms: Annotated[float | None, typer.Option(
        "--offset-ms", help="在最晚可能开放时刻之后再留的余量（毫秒），默认 10")] = None,
    clock: Annotated[str | None, typer.Option("--clock", help="auto（按讲座系统时钟校正）| local")] = None,
    no_prefetch: Annotated[bool, typer.Option("--no-prefetch", help="不在开放前预取验证码")] = False,
    max_attempts: Annotated[int | None, typer.Option("--max-attempts", help="提交次数上限（1-20，默认 8）")] = None,
    watch: Annotated[float | None, typer.Option(
        "--watch", help="满员/未开放后继续监视余量并补交的分钟数（默认 0 = 关闭，约每 2 秒查一次）")] = None,
    ignore_schedule: Annotated[bool, typer.Option("--ignore-schedule",
                                                  help="跳过课表冲突复查（默认冲突或无法判断时拒绝预约）")] = False,
    detach: Annotated[bool, typer.Option(
        "--detach", help="转到后台运行并立即返回（Agent 用）；之后用 status 查看")] = False,
    json_mode: JsonOpt = False, config: ConfigOpt = None, verbose: VerboseOpt = False,
) -> None:
    """常驻进程：预热→精确定时提交→已预约列表核验。--json 输出 JSONL 事件流。"""

    def body(out: Output) -> int:
        svc = _service(out, config)
        values: dict[str, Any] = dict(svc.settings.reserve)
        for key, value in (("offset_ms", offset_ms), ("clock", clock),
                           ("max_attempts", max_attempts), ("watch_minutes", watch)):
            if value is not None:
                values[key] = value
        if no_prefetch:
            values["prefetch_captcha"] = False
        options = ReserveOptions(lecture_id=lecture_id, at=parse_at(at), dry_run=dry_run, **values)
        options.validate()
        if not yes and not dry_run:
            if not _interactive(out):
                raise ConfirmationRequired("reservation is a write operation; pass --yes")
            when = options.at.isoformat() if options.at else "讲座预约开始时间"
            if not typer.confirm(f"确认在 {when} 为讲座 {lecture_id} 提交预约？"):
                raise ConfirmationRequired("cancelled by user")
        if detach:
            args = [lecture_id, "--json"]
            args += ["--dry-run"] if dry_run else ["--yes"]  # consent was checked above
            for flag, value in (("--at", at), ("--offset-ms", offset_ms), ("--clock", clock),
                                ("--max-attempts", max_attempts), ("--watch", watch), ("--config", config)):
                if value is not None:
                    args += [flag, str(value)]
            args += [flag for flag, on in (("--no-prefetch", no_prefetch), ("--ignore-schedule", ignore_schedule),
                                           ("--verbose", verbose)) if on]
            return _detach(out, svc, lecture_id, args)
        result = svc.reserve(options, emit=out.event, check_schedule=not ignore_schedule)
        if not out.json_mode:
            color = "green" if result.exit_code == 0 else ("yellow" if result.exit_code == 11 else "red")
            out.out.print(f"[bold {color}]{result.outcome.value}[/]：{escape(result.message)}")
            t = result.timings
            reply = (f"触发→验证码就绪 {_fmt(t.get('fire_to_ready_ms'))} ms" if options.dry_run
                     else f"触发→首个响应 {_fmt(t.get('fire_to_first_reply_ms'))} ms")
            out.out.print(f"[dim]调度误差 {_fmt(t.get('schedule_late_ms'))} ms；{reply}；"
                          f"提交 {result.attempts} 次[/]")
        return result.exit_code

    _run("reserve", json_mode, verbose, body, stream=True)


# Passed into a systemd unit, which does not inherit the caller's environment.
_PASS_ENV = ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
             "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _systemd_user() -> bool:
    """Is a per-user systemd manager reachable (Linux, including WSL with systemd)?"""
    if sys.platform != "linux" or not shutil.which("systemd-run"):
        return False
    try:
        return subprocess.run(["systemctl", "--user", "show-environment"],
                              capture_output=True, timeout=5).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _launch(command: list[str], log: Path, name: str) -> tuple[Callable[[], bool], str | None]:
    """Start ``command`` outside the caller's process tree; returns ``(alive, unit)``.

    With systemd the task becomes its own transient user service (``--collect``: gone
    once it exits), so restarting the agent's service cannot take it down. Otherwise
    a new session (POSIX) or process group (Windows) detaches it from the terminal.
    """
    if _systemd_user():
        unit = f"seu-lecture-{name}"
        env = [f"--setenv={k}={v}" for k, v in os.environ.items() if k.startswith("SEU_LECTURE_") or k in _PASS_ENV]
        started = subprocess.run(
            ["systemd-run", "--user", "--collect", "--quiet", f"--unit={unit}", f"--working-directory={os.getcwd()}",
             f"--property=StandardOutput=append:{log}", f"--property=StandardError=append:{log}", *env, "--", *command],
            capture_output=True, text=True, timeout=30)
        if started.returncode != 0:
            raise SeuLectureError(f"systemd-run failed: {started.stderr.strip()[:200]}")

        def unit_alive() -> bool:
            return subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit], timeout=5).returncode == 0

        return unit_alive, unit
    extra: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32" else {"start_new_session": True})
    with open(log, "wb") as fh:
        proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT, **extra)
    return (lambda: proc.poll() is None), None


def _detach(out: Output, svc: LectureService, lecture_id: str, args: list[str]) -> int:
    """Run ``reserve`` in a background process and return once it is under way.

    Waits until the task has passed its checks (login, lecture lookup, timetable),
    so early failures are reported here instead of a blind "started".
    """
    tasks = svc.settings.tasks_dir
    tasks.mkdir(parents=True, exist_ok=True)
    name = f"{re.sub(r'[^A-Za-z0-9_-]', '', lecture_id)[:40]}-{datetime.now(SHANGHAI):%Y%m%d-%H%M%S}"
    log = tasks / f"{name}.log"
    launched = datetime.now(SHANGHAI).isoformat()
    alive, unit = _launch([sys.executable, "-m", "seu_lecture", "reserve", *args], log, name)
    status: dict[str, Any] | None = None
    deadline = time.monotonic() + 60
    while alive() and time.monotonic() < deadline:
        # Matched by start time, not pid: on Windows a venv's python.exe is a launcher
        # whose child does the work.
        status = next((t for t in svc.task_status(lecture_id)["tasks"]
                       if str(t.get("started_at") or "") >= launched), None)
        if status and (status.get("phase") or status.get("state") != "running"):
            break
        time.sleep(0.25)
    if status and status.get("state") != "running":
        for _ in range(20):  # let it finish writing its final line
            if not alive():
                break
            time.sleep(0.25)
    if not alive():  # finished or failed already: relay its last line
        lines = [ln for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        last = lines[-1] if lines else ""
        try:
            record = json.loads(last)
        except ValueError:
            record = {}
        if out.json_mode:
            sys.stdout.write(last + "\n")
        else:
            out.out.print(escape(str(record.get("outcome") or record.get("error") or last)))
        if "exit_code" in record:
            return int(record["exit_code"])
        return OUTCOME_EXIT[Outcome(record["outcome"])] if record.get("outcome") in set(Outcome) else 1
    pid = (status or {}).get("pid")
    record = {"event": "detached", "pid": pid, "unit": unit, "lecture_id": lecture_id, "log": str(log),
              "status": status}
    if out.json_mode:
        out.event(record)
    else:
        where = f"systemd 临时服务 {unit}" if unit else f"pid {pid}"
        out.out.print(f"[green]已转到后台[/]（{where}）；用 `seu-lecture status` 查看进度，日志：{escape(str(log))}")
    return 0


def _fmt(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


@app.command("schedule")
def schedule_cmd(
    week: Annotated[int | None, typer.Option("--week", "-w", min=1, help="教学周（默认本周）")] = None,
    day: Annotated[str | None, typer.Option("--date", help="显示该日期所在教学周，YYYY-MM-DD")] = None,
    json_mode: JsonOpt = False, config: ConfigOpt = None, verbose: VerboseOpt = False,
) -> None:
    """查看我的课表（真实日期与上课时间，含调停课）。"""

    def body(out: Output) -> int:
        try:
            when = date.fromisoformat(day) if day else None
        except ValueError:
            raise ConfigError("--date must be YYYY-MM-DD") from None
        data = _service(out, config).schedule(week=week, day=when)

        def render(c: Console) -> None:
            c.print(f"[bold]{escape(data['term']['name'])}[/] 第 {data['week']} 周")
            table = Table(show_lines=False)
            for col in ("日期", "节次", "时间", "课程", "地点", "说明"):
                table.add_column(col, overflow="fold")
            names = "一二三四五六日"
            for d in data["days"]:
                for k in d["classes"]:
                    when_text = f"{k['start']}-{k['end']}" if k["start"] else "[yellow]未知[/]"
                    table.add_row(f"{d['date']} 周{names[d['weekday'] - 1]}", k["periods"], when_text,
                                  escape(k["course"]), escape(k["location"] or "-"),
                                  escape(k["change"] or k["weeks"] or ""))
            c.print(table)

        return out.success("schedule", data, render)

    _run("schedule", json_mode, verbose, body)


@app.command("status")
def status_cmd(
    lecture_id: Annotated[str | None, typer.Argument(help="只看这场讲座（ID 或前缀）")] = None,
    json_mode: JsonOpt = False, config: ConfigOpt = None, verbose: VerboseOpt = False,
) -> None:
    """查看预约任务（正在运行的和最近一周结束的）。只读本地状态，不访问学校系统。"""

    def body(out: Output) -> int:
        data = _service(out, config).task_status(lecture_id)

        def render(c: Console) -> None:
            if not data["tasks"]:
                c.print("没有预约任务")
            for t in data["tasks"]:
                line = f"[bold]{t.get('state')}[/] {escape(str(t.get('title') or t.get('lecture_id')))}"
                if t.get("phase") and t.get("state") == "running":
                    line += f"（{t['phase']}，开放 {escape(str(t.get('target_time') or '?'))}）"
                if result := t.get("result"):
                    line += f" → {result.get('outcome')}：{escape(str(result.get('message')))}"
                if error := t.get("error"):
                    line += f" → [red]{error.get('code')}[/] {escape(str(error.get('message', '')))}"
                c.print(line)

        return out.success("status", data, render)

    _run("status", json_mode, verbose, body)


def main() -> None:
    # Pipes on Windows default to the ANSI code page (cp1252 on CI), which cannot
    # encode the Chinese help/output; always emit UTF-8 (consoles are unaffected).
    for stream in (sys.stdout, sys.stderr):
        if (getattr(stream, "encoding", "") or "").lower().replace("-", "") != "utf8":
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    app()
