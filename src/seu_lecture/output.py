"""Output contract: JSON envelope, JSONL events, exit codes and human rendering.

Machine mode (``--json``) writes only UTF-8 JSON to stdout, never ANSI codes.
Human mode uses Rich; diagnostics always go to stderr.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from rich.console import Console
from rich.markup import escape

from seu_lecture.core.errors import SeuLectureError
from seu_lecture.core.lecture import SHANGHAI

SCHEMA_VERSION = "1"

EXIT_CODES: dict[int, str] = {
    0: "success (including confirmed / already reserved / dry run)",
    1: "internal error or reservation failed for an unclassified reason",
    2: "usage or configuration error",
    3: "authentication required / failed / SMS code required",
    4: "network error or timeout",
    5: "unexpected response from the school system",
    6: "lecture not found",
    7: "another reservation task is already running",
    10: "reservation rejected: full, time conflict, closed, not open, rate limited",
    11: "reservation result unconfirmed - check `seu-lecture reservations`",
    12: "confirmation required (pass --yes to authorize the write)",
    130: "interrupted",
}


class Output:
    def __init__(self, json_mode: bool, *, verbose: bool = False) -> None:
        self.json_mode = json_mode
        self.verbose = verbose
        self.out = Console(highlight=False)
        self.err = Console(stderr=True, highlight=False)
        if json_mode and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")  # Windows pipes default to the ANSI code page

    def _write(self, data: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def success(self, command: str, data: Any, render: Callable[[Console], None] | None = None,
                ) -> int:
        if self.json_mode:
            self._write({"schema_version": SCHEMA_VERSION, "ok": True, "command": command,
                         "data": data})
        elif render:
            render(self.out)
        else:
            self.out.print_json(data=data)
        return 0

    def failure(self, command: str, error: SeuLectureError, *, stream: bool = False) -> int:
        if self.json_mode:
            body: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
            if stream:
                body.update(event="error", ts=_ts(None))
            body.update(ok=False, command=command, error=error.to_dict(),
                        exit_code=error.exit_code)
            self._write(body)
        else:
            self.err.print(f"[bold red]错误[/] [red]{error.code}[/]: {escape(error.message)}")
            hint = HINTS.get(error.code)
            if hint:
                self.err.print(f"[dim]提示：{hint}[/]")
        return error.exit_code

    def event(self, record: dict[str, Any]) -> None:
        if self.json_mode:
            self._write({"schema_version": SCHEMA_VERSION, "ts": _ts(record.get("wall")),
                         **record})
            return
        line = render_event(record)
        if line:
            self.err.print(line)

    def info(self, message: str) -> None:
        if not self.json_mode:
            self.err.print(message)


def _ts(wall: float | None) -> str:
    moment = datetime.fromtimestamp(wall, SHANGHAI) if wall else datetime.now(SHANGHAI)
    return moment.isoformat(timespec="milliseconds")


HINTS = {
    "AUTH_REQUIRED": "先运行 `seu-lecture auth login`，或设置 SEU_LECTURE_PASSWORD 环境变量",
    "SMS_REQUIRED": "短信验证码已发送时，用 `seu-lecture auth login --sms-code <验证码>` 重试",
    "NETWORK_ERROR": "确认在校园网内或已连接学校 VPN；可运行 `seu-lecture doctor`",
    "NETWORK_TIMEOUT": "网络超时，可运行 `seu-lecture doctor` 检查连通性",
    "CONFIRMATION_REQUIRED": "预约是写操作，需要显式加 --yes 授权",
    "TASK_RUNNING": "用 `seu-lecture status` 查看正在运行的任务",
    "PROTOCOL_ERROR": "学校接口可能已变更，请带上 `--json` 输出提 issue",
}

_EVENT_TEXT = {
    "prepare": "准备阶段：校验会话、加载讲座与 OCR",
    "wait": "等待阶段",
}


def render_event(e: dict[str, Any]) -> str | None:
    kind = e.get("event")
    if kind == "phase":
        if e.get("phase") == "fire":
            return f"[bold magenta]→ 触发提交[/]（调度误差 {e.get('late_ms')} ms）"
        return f"[cyan]·[/] {_EVENT_TEXT.get(e.get('phase', ''), e.get('phase'))}"
    if kind == "prepared":
        lec = e.get("lecture") or {}
        return (f"[green]✓[/] 目标：{escape(str(lec.get('title')))}  开放时间 {e.get('at')}  "
                f"余量 {lec.get('remaining')}  OCR 预热 {e.get('ocr_warmup_ms')} ms")
    if kind == "clock":
        return (f"[cyan]·[/] 服务器 Date 偏差估计 {e.get('offset_ms')} ± {e.get('error_ms')} ms"
                f"（RTT {e.get('rtt_ms')} ms，{e.get('samples')} 个样本）")
    if kind == "plan":
        return f"[cyan]·[/] 候选开放时刻：{'、'.join(e.get('candidates') or [])}"
    if kind == "candidate_not_open":
        return "[yellow]·[/] 较早的候选时刻尚未开放，等待下一个候选时刻"
    if kind == "armed":
        fire = datetime.fromtimestamp(e["fire_at"], SHANGHAI).isoformat(timespec="milliseconds")
        return f"[cyan]·[/] 已就绪，将在 {fire} 发出请求（RTT {e.get('rtt_ms')} ms）"
    if kind == "attempt":
        return f"  第 {e.get('n')} 次提交：{e.get('reply')}  {escape(str(e.get('message') or ''))}"
    if kind in ("warning", "clock_jump"):
        return f"[yellow]![/] {escape(str(e.get('message') or e.get('hint')))}"
    if kind == "relogin":
        return f"[yellow]↻[/] 重新登录：{escape(str(e.get('reason')))}"
    if kind == "resubmit":
        return "[yellow]↻[/] 已核实未预约，允许重新提交一次"
    if kind == "keepalive":
        return "[dim]· 会话保活[/]"
    return None
