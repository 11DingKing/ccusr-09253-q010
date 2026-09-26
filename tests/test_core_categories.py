"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.replay import Event, EventType, replay
from app.core.rules import DEFAULT_RULE_VERSION, RuleDef

SH = timezone(timedelta(hours=8))


def _rule(
    rule_version: str = "rv-1",
    *,
    categories: list[dict],
    windows: list[dict] | None = None,
    mapping: dict[str, str] | None = None,
    default: str | None = None,
) -> RuleDef:
    return RuleDef.from_dict(
        rule_version,
        {
            "categories": categories,
            "authorization_windows": windows or [],
            "activity_category_map": mapping or {},
            "default_category": default,
        },
    )


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    *,
    plan_version: str = "P1",
    rule_version: str | None = "rv-1",
) -> Event:
    return Event(
        event_id=event_id,
        plan_version=plan_version,
        event_type=event_type,
        student_id=student_id,
        payload=payload,
        created_at=datetime.now(timezone.utc),
        rule_version=rule_version,
    )


def _checkin(
    eid: str,
    student: str,
    start: str,
    end: str,
    *,
    activity_type: str = "regular",
    rule_version: str | None = "rv-1",
) -> Event:
    return _event(
        eid,
        EventType.CHECKIN,
        student,
        {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
        rule_version=rule_version,
    )


def _correction(
    eid: str,
    student: str,
    seconds: int,
    *,
    category: str | None = None,
    rule_version: str | None = "rv-1",
) -> Event:
    payload: dict = {"adjustment_seconds": seconds, "reason": "manual"}
    if category is not None:
        payload["category"] = category
    return _event(
        eid,
        EventType.LEAVE_CORRECTION,
        student,
        payload,
        rule_version=rule_version,
    )


def _replay(events, *, required: int, rules: dict[str, RuleDef], active: str = "rv-1"):
    return replay(
        events,
        plan_version="P1",
        timezone_name="Asia/Shanghai",
        required_seconds=required,
        rules=rules,
        active_rule_version=active,
    )


def test_permille_thresholds_use_floor_integer_math():
    # 10801 * 500 / 1000 = 5400.5 -> 向下取整为 5400。
    rule = _rule(
        categories=[{"key": "on_campus", "min_permille": 500, "max_permille": 500}],
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:30:00+08:00")
    ]
    state = _replay(events, required=10801, rules={"rv-1": rule})
    category = state.students["S1"].categories[0]
    assert category.min_seconds == 5400
    assert category.max_seconds == 5400
    assert category.counted_seconds == 5400
    assert category.credited_seconds == 5400
    assert category.min_met is True
    assert category.gap_seconds == 0
    assert category.excess_seconds == 0


def test_one_second_below_min_opens_a_gap_of_exactly_one():
    rule = _rule(
        categories=[{"key": "on_campus", "min_permille": 500, "max_permille": 1000}],
        mapping={"regular": "on_campus"},
    )
    start = datetime(2024, 3, 15, 8, 0, tzinfo=SH)
    end = start + timedelta(seconds=5399)
    events = [_checkin("E-01", "S1", start.isoformat(), end.isoformat())]
    state = _replay(events, required=10801, rules={"rv-1": rule})
    category = state.students["S1"].categories[0]
    assert category.min_seconds == 5400
    assert category.counted_seconds == 5399
    assert category.gap_seconds == 1
    assert category.min_met is False
    assert state.students["S1"].meets_requirement is False


def test_one_second_above_cap_is_trimmed_and_reported():
    rule = _rule(
        categories=[{"key": "on_campus", "min_permille": 0, "max_permille": 500}],
        mapping={"regular": "on_campus"},
    )
    start = datetime(2024, 3, 15, 8, 0, tzinfo=SH)
    end = start + timedelta(seconds=5401)
    events = [_checkin("E-01", "S1", start.isoformat(), end.isoformat())]
    state = _replay(events, required=10801, rules={"rv-1": rule})
    progress = state.students["S1"]
    category = progress.categories[0]
    assert category.counted_seconds == 5401
    assert category.credited_seconds == 5400
    assert category.excess_seconds == 1
    assert progress.total_seconds == 5400
    assert {
        "reason": "exceeds_category_cap",
        "category": "on_campus",
        "seconds": 1,
    } in progress.non_countable


def test_category_lesson_units_round_down():
    rule = _rule(
        categories=[{"key": "on_campus"}],
        mapping={"regular": "on_campus"},
    )
    start = datetime(2024, 3, 15, 8, 0, tzinfo=SH)
    events = [
        _checkin(
            "E-01",
            "S1",
            start.isoformat(),
            (start + timedelta(seconds=2699)).isoformat(),
        )
    ]
    state = _replay(events, required=10800, rules={"rv-1": rule})
    assert state.students["S1"].categories[0].lesson_units == 0

    events.append(
        _checkin(
            "E-02",
            "S1",
            (start + timedelta(hours=2)).isoformat(),
            (start + timedelta(hours=2, seconds=1)).isoformat(),
        )
    )
    state = _replay(events, required=10800, rules={"rv-1": rule})
    assert state.students["S1"].categories[0].credited_seconds == 2700
    assert state.students["S1"].categories[0].lesson_units == 1


def test_negative_category_correction_clamps_at_zero():
    rule = _rule(
        categories=[{"key": "on_campus"}],
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T10:00:00+08:00"),
        _correction("E-02", "S1", -3 * 3600, category="on_campus"),
    ]
    state = _replay(events, required=10800, rules={"rv-1": rule})
    progress = state.students["S1"]
    category = progress.categories[0]
    assert category.counted_seconds == 7200
    assert category.adjustment_seconds == -10800
    assert category.credited_seconds == 0
    assert progress.total_seconds == 0
    assert progress.lesson_units == 0


def test_negative_uncategorized_correction_never_makes_total_negative():
    rule = _rule(
        categories=[{"key": "on_campus"}],
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-02", "S1", -7200),
    ]
    state = _replay(events, required=10800, rules={"rv-1": rule})
    progress = state.students["S1"]
    # 未分类负向修正只冲减总量，不影响类别分项。
    assert progress.categories[0].credited_seconds == 3600
    assert progress.total_seconds == 0
    assert progress.adjustment_seconds == -7200


def test_positive_category_correction_respects_cap():
    rule = _rule(
        categories=[{"key": "on_campus", "min_permille": 0, "max_permille": 500}],
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin("E-01", "S1", "2024-03-15T08:00:00+08:00", "2024-03-15T09:00:00+08:00"),
        _correction("E-02", "S1", 1800, category="on_campus"),
    ]
    # required 7200 -> 上限 3600；3600 + 1800 超出部分被截去。
    state = _replay(events, required=7200, rules={"rv-1": rule})
    category = state.students["S1"].categories[0]
    assert category.max_seconds == 3600
    assert category.credited_seconds == 3600
    assert category.excess_seconds == 1800


def test_cross_midnight_checkin_splits_categories_by_academic_day():
    rule = _rule(
        categories=[{"key": "on_campus"}],
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00")
    ]
    state = _replay(events, required=0, rules={"rv-1": rule})
    progress = state.students["S1"]
    days = {d.academic_day: d for d in progress.daily}
    assert days["2024-03-15"].seconds == 7200
    assert days["2024-03-15"].categories == {"on_campus": 7200}
    assert days["2024-03-16"].seconds == 7200
    assert days["2024-03-16"].categories == {"on_campus": 7200}
    # 每日分项之和等于类别计入总量。
    assert sum(d.seconds for d in progress.daily) == 14400
    assert progress.categories[0].counted_seconds == 14400


def test_window_ending_at_midnight_marks_tail_outside():
    rule = _rule(
        categories=[{"key": "on_campus"}],
        windows=[
            {
                "category": "on_campus",
                "start": "2024-03-14T16:00:00Z",  # 2024-03-15 00:00 +08
                "end": "2024-03-15T16:00:00Z",  # 2024-03-16 00:00 +08
            }
        ],
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin("E-01", "S1", "2024-03-15T22:00:00+08:00", "2024-03-16T02:00:00+08:00")
    ]
    state = _replay(events, required=0, rules={"rv-1": rule})
    progress = state.students["S1"]
    category = progress.categories[0]
    assert category.counted_seconds == 7200
    assert {d.academic_day for d in progress.daily} == {"2024-03-15"}
    assert {
        "reason": "outside_authorization_window",
        "seconds": 7200,
    } in progress.non_countable
    record = progress.checkins[0]
    assert record.countable_seconds == 7200
    assert record.outside_window_seconds == 7200


def test_overlap_allocated_to_higher_priority_category():
    # 规则中 enterprise 排在 on_campus 之前，重叠部分归 enterprise。
    rule = _rule(
        categories=[{"key": "enterprise"}, {"key": "on_campus"}],
        mapping={"regular": "on_campus", "internship": "enterprise"},
    )
    events = [
        _checkin("E-01", "S1", "2024-03-15T09:00:00+08:00", "2024-03-15T11:00:00+08:00"),
        _checkin(
            "E-02",
            "S1",
            "2024-03-15T10:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        ),
        _event(
            "E-03",
            EventType.MENTOR_CONFIRM,
            "S1",
            {"checkin_event_id": "E-02"},
        ),
    ]
    state = _replay(events, required=0, rules={"rv-1": rule})
    progress = state.students["S1"]
    by_key = {c.category: c for c in progress.categories}
    assert by_key["enterprise"].counted_seconds == 7200
    assert by_key["on_campus"].counted_seconds == 3600
    # 合并后的并集只有 3 小时，各类别之和不得超出。
    assert progress.countable_seconds == 10800
    assert progress.confirmed_seconds == 10800
    on_campus_record = progress.checkins[0]
    assert on_campus_record.event_id == "E-01"
    assert on_campus_record.overlap_seconds == 3600
    assert on_campus_record.allocated_seconds == 3600
    assert {
        "reason": "overlap_attributed_elsewhere",
        "seconds": 3600,
    } in progress.non_countable


def test_checkin_spanning_multiple_authorization_windows():
    rule = _rule(
        categories=[{"key": "on_campus"}],
        windows=[
            {
                "category": "on_campus",
                "start": "2024-03-15T00:00:00Z",  # 08:00 +08
                "end": "2024-03-15T02:00:00Z",  # 10:00 +08
            },
            {
                "category": "on_campus",
                "start": "2024-03-15T06:00:00Z",  # 14:00 +08
                "end": "2024-03-15T08:00:00Z",  # 16:00 +08
            },
        ],
        mapping={"regular": "on_campus"},
    )
    # 09:00-15:00 跨越两个授权区间，中间 10:00-14:00 不在授权内。
    events = [
        _checkin("E-01", "S1", "2024-03-15T09:00:00+08:00", "2024-03-15T15:00:00+08:00")
    ]
    state = _replay(events, required=0, rules={"rv-1": rule})
    progress = state.students["S1"]
    assert progress.categories[0].counted_seconds == 7200
    record = progress.checkins[0]
    assert record.countable_seconds == 7200
    assert record.outside_window_seconds == 4 * 3600
    assert len(record.pieces) == 2
    assert {
        "reason": "outside_authorization_window",
        "seconds": 4 * 3600,
    } in progress.non_countable


def test_uncategorized_time_counts_toward_total_only():
    rule = _rule(
        categories=[{"key": "on_campus", "min_permille": 500, "max_permille": 1000}],
        mapping={"regular": "on_campus"},
    )
    # 事件固定在无类别映射的旧版规则上，重放时保持未分类。
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            rule_version=DEFAULT_RULE_VERSION,
        )
    ]
    state = _replay(events, required=7200, rules={"rv-1": rule})
    progress = state.students["S1"]
    assert progress.uncategorized_seconds == 7200
    assert progress.total_seconds == 7200
    category = progress.categories[0]
    assert category.counted_seconds == 0
    assert category.gap_seconds == 3600
    assert progress.meets_requirement is False
    assert progress.event_rule_versions == [DEFAULT_RULE_VERSION]


def test_pending_internship_hours_wait_for_confirmation_in_their_category():
    rule = _rule(
        categories=[{"key": "enterprise", "min_permille": 200}],
        mapping={"internship": "enterprise"},
    )
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            activity_type="internship",
        )
    ]
    state = _replay(events, required=10800, rules={"rv-1": rule})
    progress = state.students["S1"]
    category = progress.categories[0]
    assert category.pending_seconds == 4 * 3600
    assert category.counted_seconds == 0
    assert category.gap_seconds == 2160
    assert {
        "reason": "pending_confirmation",
        "seconds": 4 * 3600,
    } in progress.non_countable

    events.append(
        _event(
            "E-02",
            EventType.MENTOR_CONFIRM,
            "S1",
            {"checkin_event_id": "E-01"},
        )
    )
    state = _replay(events, required=10800, rules={"rv-1": rule})
    category = state.students["S1"].categories[0]
    assert category.pending_seconds == 0
    assert category.counted_seconds == 4 * 3600
    assert category.min_met is True


def test_events_keep_their_pinned_rule_version_after_upgrade():
    # rv-1 授权 3 月，rv-2 授权 4 月；事件始终按各自固定的版本截断。
    rv1 = _rule(
        "rv-1",
        categories=[{"key": "on_campus"}],
        windows=[
            {
                "category": "on_campus",
                "start": "2024-03-01T00:00:00Z",
                "end": "2024-04-01T00:00:00Z",
            }
        ],
        mapping={"regular": "on_campus"},
    )
    rv2 = _rule(
        "rv-2",
        categories=[{"key": "on_campus"}],
        windows=[
            {
                "category": "on_campus",
                "start": "2024-04-01T00:00:00Z",
                "end": "2024-05-01T00:00:00Z",
            }
        ],
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            rule_version="rv-1",
        ),
        _checkin(
            "E-02",
            "S1",
            "2024-04-15T08:00:00+08:00",
            "2024-04-15T10:00:00+08:00",
            rule_version="rv-2",
        ),
    ]
    state = _replay(events, required=14400, rules={"rv-1": rv1, "rv-2": rv2}, active="rv-2")
    progress = state.students["S1"]
    # 若 E-01 被错误地按 rv-2 重估，它会落在授权区间外而不计入。
    assert progress.categories[0].counted_seconds == 4 * 3600
    assert progress.non_countable == []
    assert progress.event_rule_versions == ["rv-1", "rv-2"]
    assert state.active_rule_version == "rv-2"
    assert state.rule_versions == ["rv-1", "rv-2"]
