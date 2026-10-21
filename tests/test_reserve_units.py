"""Pure-logic pieces of the reservation module: reply classification, options, results."""

from __future__ import annotations

import json
from typing import Any

import pytest

from seu_lecture.core.errors import ConfigError
from seu_lecture.core.reserve import (
    OUTCOME_EXIT,
    Outcome,
    Reply,
    ReserveOptions,
    TaskResult,
    classify_reply,
)


@pytest.mark.parametrize(("payload", "reply"), [
    ({"code": "1", "msg": "验证码错误"}, Reply.CAPTCHA),
    ({"code": "1", "msg": "验证码错误或已失效"}, Reply.CAPTCHA),
    ({"code": "1", "msg": "您已预约过该活动"}, Reply.ALREADY),
    ({"code": "1", "msg": "请勿重复预约"}, Reply.ALREADY),
    ({"code": "1", "msg": "预约人数已满"}, Reply.FULL),
    ({"code": "1", "msg": "余量不足"}, Reply.FULL),
    ({"code": "1", "msg": "时间上存在冲突"}, Reply.CONFLICT),
    ({"code": "1", "msg": "请求过于频繁"}, Reply.RATE_LIMITED),
    ({"code": "1", "msg": "预约未开始"}, Reply.NOT_OPEN),
    ({"code": "1", "msg": "预约尚未开放"}, Reply.NOT_OPEN),
    ({"code": "1", "msg": "预约已结束"}, Reply.CLOSED),
    ({"code": "1", "msg": "已截止"}, Reply.CLOSED),
    ({"code": "1", "msg": "非法参数"}, Reply.INVALID),
    ({"success": True}, Reply.SUCCESS),
    ({"code": 0, "datas": 1}, Reply.SUCCESS),
    ({"code": "0", "datas": "1"}, Reply.SUCCESS),
    ({"code": "0", "msg": "预约成功", "success": True, "datas": 1}, Reply.SUCCESS),
    ({"msg": "预约成功"}, Reply.SUCCESS),                       # success by message alone
    ({"code": "9", "msg": "系统繁忙"}, Reply.UNKNOWN),
    ({"msg": "预约不成功"}, Reply.UNKNOWN),
    ({"msg": "预约成功后处理失败"}, Reply.UNKNOWN),
    ({"code": "0", "datas": 0}, Reply.UNKNOWN),
    ({"code": "0"}, Reply.UNKNOWN),
    ({}, Reply.UNKNOWN),
    # failure keywords win over success flags (some failures come back with code 0)
    ({"success": True, "msg": "预约人数已满"}, Reply.FULL),
    ({"code": 0, "datas": 1, "msg": "验证码错误"}, Reply.CAPTCHA),
    # the message may live under other keys
    ({"message": "预约人数已满"}, Reply.FULL),
    ({"info": "时间冲突"}, Reply.CONFLICT),
])
def test_classify_reply(payload: dict[str, Any], reply: Reply) -> None:
    assert classify_reply(payload)[0] is reply


def test_conflict_message_mentioning_reserved_activity_is_a_conflict() -> None:
    assert classify_reply({"code": "1", "msg": "与已预约活动时间上存在冲突"})[0] is Reply.CONFLICT


def test_classify_reply_returns_the_message() -> None:
    assert classify_reply({"msg": "预约人数已满"}) == (Reply.FULL, "预约人数已满")
    assert classify_reply({"success": True}) == (Reply.SUCCESS, "")


def test_every_outcome_has_an_exit_code() -> None:
    assert set(OUTCOME_EXIT) == set(Outcome)
    assert OUTCOME_EXIT[Outcome.CONFIRMED] == OUTCOME_EXIT[Outcome.ALREADY_RESERVED] == 0
    assert OUTCOME_EXIT[Outcome.FULL] == OUTCOME_EXIT[Outcome.CONFLICT] == 10
    assert OUTCOME_EXIT[Outcome.UNCONFIRMED] == 11
    assert OUTCOME_EXIT[Outcome.FAILED] == 1
    assert OUTCOME_EXIT[Outcome.ABORTED] == 130


def test_task_result_to_dict_is_json_serialisable() -> None:
    result = TaskResult(Outcome.FULL, "已满", attempts=1, submitted=True,
                        timings={"a": 1.23456, "b": None})
    data = result.to_dict()
    assert data["outcome"] == "full"
    assert data["timings"] == {"a": 1.235, "b": None}
    assert result.exit_code == 10
    assert json.loads(json.dumps(data, ensure_ascii=False)) == data


# ---- ReserveOptions.validate -------------------------------------------------------------

def test_default_options_are_valid() -> None:
    ReserveOptions(lecture_id="x").validate()


@pytest.mark.parametrize("override", [
    {"clock": "sundial"},
    {"max_attempts": 0},
    {"max_attempts": 21},
    {"min_submit_interval_ms": 49.0},
    {"watch_interval_s": 0.5},
    {"offset_ms": -1001.0},
    {"offset_ms": 5001.0},
    {"clock": "server"},  # removed: the Date header is not the app clock
])
def test_invalid_options_raise_config_error(override: dict[str, Any]) -> None:
    with pytest.raises(ConfigError) as info:
        ReserveOptions(lecture_id="x", **override).validate()
    assert info.value.exit_code == 2


@pytest.mark.parametrize("override", [
    {"clock": "auto", "max_attempts": 1, "min_submit_interval_ms": 100.0, "offset_ms": -1000.0},
    {"clock": "local", "max_attempts": 20, "offset_ms": 5000.0},
])
def test_boundary_options_are_accepted(override: dict[str, Any]) -> None:
    ReserveOptions(lecture_id="x", **override).validate()


@pytest.mark.parametrize("override", [
    {"keepalive_s": 0.0},
    {"verify_attempts": 0},
    {"rate_limit_backoff_s": -1.0},
])
def test_unsafe_option_values_are_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ConfigError):
        ReserveOptions(lecture_id="x", **override).validate()
