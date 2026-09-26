"""分项规则计算内核测试：边界取整、负向修正、跨日拆分与规则升级。"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from fractions import Fraction

import pytest

from app.core.replay import (
    REASON_CAP_EXCEEDED,
    REASON_NO_RULE,
    REASON_NO_WINDOW,
    REASON_NOT_IN_RULE,
    REASON_PENDING,
    REASON_UNKNOWN_CATEGORY,
    REASON_UNMAPPED_ACTIVITY,
    Event,
    EventType,
    explain_checkin,
    replay,
)
from app.core.rules import (
    CategoryLimit,
    RuleConfig,
    RuleConfigError,
    parse_ratio,
    validate_categories,
)

DEFAULT_CATEGORIES = (
    ("on_campus", "0.4", "0.7"),
    ("enterprise", "0.2", "0.5"),
    ("public_welfare", "0", "0.2"),
)
DEFAULT_MAPPING = {
    "regular": "on_campus",
    "internship": "enterprise",
    "volunteer": "public_welfare",
}


def _rule(
    rule_version: str = "R1",
    categories=DEFAULT_CATEGORIES,
    mapping: dict | None = None,
) -> RuleConfig:
    return RuleConfig(
        rule_version=rule_version,
        categories=tuple(
            CategoryLimit(code=code, min_ratio=Fraction(lo), max_ratio=Fraction(hi))
            for code, lo, hi in categories
        ),
        activity_category_map=dict(DEFAULT_MAPPING if mapping is None else mapping),
    )


def _event(
    event_id: str,
    event_type: EventType,
    student_id: str,
    payload: dict,
    plan_version: str = "P1",
    rule_version: str | None = None,
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
    activity_id: str = "A1",
    windows: list[dict] | None = None,
    rule_version: str | None = "R1",
) -> Event:
    payload = {
        "activity_id": activity_id,
        "activity_type": activity_type,
        "check_in_at": start,
        "check_out_at": end,
    }
    if windows is not None:
        payload["category_windows"] = windows
    return _event(eid, EventType.CHECKIN, student, payload, rule_version=rule_version)


def _correction(
    eid: str, student: str, seconds: int, *, category: str | None = None
) -> Event:
    payload: dict = {"adjustment_seconds": seconds, "reason": "adjustment"}
    if category is not None:
        payload["category"] = category
    return _event(eid, EventType.LEAVE_CORRECTION, student, payload)


def _replay(events, *, required: int = 10800, rules=None, current=None, tz="Asia/Shanghai"):
    return replay(
        events,
        plan_version="P1",
        timezone_name=tz,
        required_seconds=required,
        rules=rules,
        current_rule=current,
    )


def _by_code(progress):
    return {c.category: c for c in progress.categories}


# ---------------------------------------------------------------- 边界取整


def test_category_limit_rounding_boundaries():
    limit = CategoryLimit("on_campus", Fraction("0.3333"), Fraction("0.3333"))
    # 10800 * 0.3333 = 3599.64：最低占比向上取整，最高占比向下取整。
    assert limit.min_seconds(10800) == 3600
    assert limit.max_seconds(10800) == 3599

    exact = CategoryLimit("on_campus", Fraction(1, 3), Fraction(1, 3))
    # 恰好整除时不得多收或少算一秒。
    assert exact.min_seconds(10800) == 3600
    assert exact.max_seconds(10800) == 3600
    assert exact.min_seconds(0) == 0
    assert exact.max_seconds(0) == 0

    half = CategoryLimit("c", Fraction("0.5"), Fraction("0.5"))
    assert half.min_seconds(1) == 1
    assert half.max_seconds(1) == 0


def test_parse_ratio_accepts_decimal_and_fraction_strings():
    assert parse_ratio(0.25) == Fraction(1, 4)
    assert parse_ratio("0.3333") == Fraction("0.3333")
    assert parse_ratio("1/3") == Fraction(1, 3)
    with pytest.raises(RuleConfigError):
        parse_ratio("1.5")
    with pytest.raises(RuleConfigError):
        parse_ratio(-0.1)
    with pytest.raises(RuleConfigError):
        parse_ratio("not-a-number")


def test_validate_categories_rejects_infeasible_rules():
    # 最低占比之和超过 1，永远无法同时达标。
    with pytest.raises(RuleConfigError):
        validate_categories(
            [
                CategoryLimit("a", Fraction("0.6"), Fraction("0.7")),
                CategoryLimit("b", Fraction("0.6"), Fraction("0.7")),
            ]
        )
    # 最高占比之和小于 1，总学时永远凑不满。
    with pytest.raises(RuleConfigError):
        validate_categories(
            [
                CategoryLimit("a", Fraction("0.1"), Fraction("0.4")),
                CategoryLimit("b", Fraction("0.1"), Fraction("0.4")),
            ]
        )
    # 单类别下限高于上限。
    with pytest.raises(RuleConfigError):
        validate_categories(
            [CategoryLimit("a", Fraction("0.5"), Fraction("0.4"))]
        )
    with pytest.raises(RuleConfigError):
        validate_categories([])


def test_counted_seconds_clamped_to_max_ratio_boundary():
    rule = _rule(categories=(("on_campus", "0", "0.5"), ("enterprise", "0", "0.5")))
    # required=7200 -> on_campus 上限 3600；签到 3 小时，只有 1 小时可计入。
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T11:00:00+08:00",
        )
    ]
    state = _replay(events, required=7200, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    on_campus = _by_code(progress)["on_campus"]
    assert on_campus.attributed_seconds == 3 * 3600
    assert on_campus.max_seconds == 3600
    assert on_campus.counted_seconds == 3600
    assert on_campus.over_cap_seconds == 2 * 3600
    cap_exclusions = [
        e for e in progress.exclusions if e.reason == REASON_CAP_EXCEEDED
    ]
    assert len(cap_exclusions) == 1
    assert cap_exclusions[0].seconds == 2 * 3600
    assert cap_exclusions[0].category == "on_campus"


def test_category_minimum_boundary_is_inclusive():
    # 10800 * 1/3 = 3600，计入量恰好等于下限时判定达标。
    rule = _rule(
        categories=(("on_campus", "1/3", "1"), ("enterprise", "0", "1")),
        mapping={"regular": "on_campus"},
    )
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        )
    ]
    state = _replay(events, required=10800, rules={"R1": rule}, current=rule)
    on_campus = _by_code(state.students["S1"])["on_campus"]
    assert on_campus.min_seconds == 3600
    assert on_campus.counted_seconds == 3600
    assert on_campus.shortfall_seconds == 0
    assert on_campus.meets_minimum is True


# ------------------------------------------------------------ 跨类别授权区间


def test_checkin_spanning_multiple_category_windows():
    rule = _rule()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T14:00:00+08:00",
            windows=[
                {
                    "category": "on_campus",
                    "start": "2024-03-15T08:00:00+08:00",
                    "end": "2024-03-15T10:00:00+08:00",
                },
                {
                    "category": "enterprise",
                    "start": "2024-03-15T10:00:00+08:00",
                    "end": "2024-03-15T13:00:00+08:00",
                },
            ],
        )
    ]
    state = _replay(events, required=21600, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    by_code = _by_code(progress)
    assert by_code["on_campus"].attributed_seconds == 7200
    assert by_code["enterprise"].attributed_seconds == 3 * 3600
    # 13:00-14:00 不在任何授权区间内，不可计入。
    unauth = [e for e in progress.exclusions if e.reason == REASON_NO_WINDOW]
    assert len(unauth) == 1
    assert unauth[0].seconds == 3600
    assert unauth[0].event_id == "E-01"
    # 合并后总量守恒：各类别归属 + 未授权 = 全部确认时长。
    assert 7200 + 3 * 3600 + 3600 == progress.confirmed_seconds == 6 * 3600
    # 单次签到的解释里能看到每个授权区间的依据。
    checkin = explain_checkin(progress.checkins[0], "Asia/Shanghai")
    assert [s["category"] for s in checkin["category_segments"]] == [
        "on_campus",
        "enterprise",
    ]
    assert checkin["excluded_segments"][0]["reason"] == REASON_NO_WINDOW


def test_overlapping_checkins_are_merged_before_category_allocation():
    rule = _rule()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_type="regular",
        ),
        _checkin(
            "E-02",
            "S1",
            "2024-03-15T09:00:00+08:00",
            "2024-03-15T11:00:00+08:00",
            activity_type="internship",
        ),
        _event("E-03", EventType.MENTOR_CONFIRM, "S1", {"checkin_event_id": "E-02"}),
    ]
    state = _replay(events, required=10800, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    # 重叠区间只计一次总量。
    assert progress.confirmed_seconds == 3 * 3600
    by_code = _by_code(progress)
    # 规则中 on_campus 声明在前，重叠的 09:00-10:00 归 on_campus。
    assert by_code["on_campus"].attributed_seconds == 2 * 3600
    assert by_code["enterprise"].attributed_seconds == 3600
    assert sum(c.attributed_seconds for c in progress.categories) == (
        progress.confirmed_seconds
    )


def test_window_category_unknown_to_pinned_rule_is_excluded():
    rule = _rule(categories=(("on_campus", "0", "1"),), mapping={"regular": "on_campus"})
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            windows=[
                {
                    "category": "enterprise",
                    "start": "2024-03-15T08:00:00+08:00",
                    "end": "2024-03-15T10:00:00+08:00",
                }
            ],
        )
    ]
    state = _replay(events, required=7200, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    assert _by_code(progress)["on_campus"].attributed_seconds == 0
    unknown = [e for e in progress.exclusions if e.reason == REASON_UNKNOWN_CATEGORY]
    assert len(unknown) == 1
    assert unknown[0].seconds == 7200


def test_unmapped_activity_type_is_excluded_with_reason():
    rule = _rule(mapping={"regular": "on_campus"})
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
            activity_type="mystery",
        )
    ]
    state = _replay(events, required=3600, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    assert progress.counted_seconds == 0
    unmapped = [
        e for e in progress.exclusions if e.reason == REASON_UNMAPPED_ACTIVITY
    ]
    assert len(unmapped) == 1
    assert unmapped[0].seconds == 3600


# ------------------------------------------------------------------ 负向修正


def test_negative_category_adjustment_clamps_category_at_zero():
    rule = _rule()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
        # 负向修正超过该类别已归属时长，类别计入量不得为负。
        _correction("E-02", "S1", -7200, category="on_campus"),
    ]
    state = _replay(events, required=10800, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    on_campus = _by_code(progress)["on_campus"]
    assert on_campus.attributed_seconds == 3600
    assert on_campus.adjustment_seconds == -7200
    assert on_campus.counted_seconds == 0
    assert on_campus.shortfall_seconds == on_campus.min_seconds
    # 负向溢出不得拖垮其他类别或总量到负数。
    assert progress.counted_seconds == 0


def test_negative_uncategorized_adjustment_clamps_counted_total_at_zero():
    rule = _rule()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
        _correction("E-02", "S1", -999999),
    ]
    state = _replay(events, required=10800, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    assert progress.counted_seconds == 0
    assert progress.total_seconds == 0
    assert progress.total_shortfall_seconds == 10800


def test_positive_uncategorized_adjustment_counts_toward_total_only():
    rule = _rule()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T09:00:00+08:00",
        ),
        _correction("E-02", "S1", 1800),
    ]
    state = _replay(events, required=10800, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    # 未定向修正只进计入总量，不进任何类别。
    assert progress.counted_seconds == 3600 + 1800
    assert sum(c.counted_seconds for c in progress.categories) == 3600


# ------------------------------------------------------------------ 跨日拆分


def test_cross_day_checkin_splits_category_seconds_by_academic_day():
    rule = _rule()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T22:00:00+08:00",
            "2024-03-16T02:00:00+08:00",
            windows=[
                {
                    "category": "on_campus",
                    "start": "2024-03-15T22:00:00+08:00",
                    "end": "2024-03-15T23:30:00+08:00",
                },
                {
                    "category": "enterprise",
                    "start": "2024-03-15T23:30:00+08:00",
                    "end": "2024-03-16T02:00:00+08:00",
                },
            ],
        )
    ]
    state = _replay(events, required=14400, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    # 总量按学术日拆分保持不变。
    daily = {d.academic_day: d.seconds for d in progress.daily}
    assert daily == {"2024-03-15": 7200, "2024-03-16": 7200}
    # 类别归属同样按学术日拆分。
    daily_categories = {
        (d.academic_day, d.category): d.seconds for d in progress.daily_categories
    }
    assert daily_categories == {
        ("2024-03-15", "on_campus"): 5400,
        ("2024-03-15", "enterprise"): 1800,
        ("2024-03-16", "enterprise"): 7200,
    }


# ------------------------------------------------------------------ 规则升级


def test_rule_upgrade_keeps_each_event_pinned_to_its_own_version():
    r1 = _rule("R1", mapping={"regular": "on_campus"})
    r2 = _rule("R2", mapping={"regular": "enterprise"})
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            rule_version="R1",
        ),
        _checkin(
            "E-02",
            "S1",
            "2024-03-15T10:00:00+08:00",
            "2024-03-15T12:00:00+08:00",
            rule_version="R2",
        ),
    ]
    state = _replay(
        events,
        required=14400,
        rules={"R1": r1, "R2": r2},
        current=r2,
    )
    progress = state.students["S1"]
    by_code = _by_code(progress)
    # E-01 仍按 R1 的映射计入校内，E-02 按 R2 的映射计入企业。
    assert by_code["on_campus"].attributed_seconds == 7200
    assert by_code["enterprise"].attributed_seconds == 7200
    # 达标阈值取现行规则 R2。
    assert state.rule_version == "R2"
    checkins = {c.event_id: c for c in progress.checkins}
    assert checkins["E-01"].rule_version == "R1"
    assert checkins["E-02"].rule_version == "R2"


def test_event_imported_before_any_rule_is_reported_not_counted():
    rule = _rule("R1")
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            rule_version=None,
        )
    ]
    state = _replay(events, required=7200, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    # 总量口径仍在，但分项口径下不可计入。
    assert progress.confirmed_seconds == 7200
    assert progress.counted_seconds == 0
    assert progress.meets_requirement is False
    assert progress.total_shortfall_seconds == 7200
    unpinned = [e for e in progress.exclusions if e.reason == REASON_NO_RULE]
    assert len(unpinned) == 1
    assert unpinned[0].seconds == 7200
    assert unpinned[0].event_id == "E-01"


def test_category_removed_by_rule_upgrade_is_excluded_with_reason():
    r1 = _rule("R1", mapping={"regular": "on_campus"})
    r2 = _rule(
        "R2",
        categories=(("enterprise", "0", "1"),),
        mapping={"regular": "enterprise"},
    )
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            rule_version="R1",
        )
    ]
    state = _replay(events, required=7200, rules={"R1": r1, "R2": r2}, current=r2)
    progress = state.students["S1"]
    # R1 归属的 on_campus 在 R2 中已不存在，不可计入。
    assert progress.counted_seconds == 0
    stale = [e for e in progress.exclusions if e.reason == REASON_NOT_IN_RULE]
    assert len(stale) == 1
    assert stale[0].seconds == 7200
    assert stale[0].category == "on_campus"


def test_pending_checkin_listed_as_exclusion_with_reason():
    rule = _rule()
    events = [
        _checkin(
            "E-01",
            "S1",
            "2024-03-15T08:00:00+08:00",
            "2024-03-15T10:00:00+08:00",
            activity_type="internship",
        )
    ]
    state = _replay(events, required=7200, rules={"R1": rule}, current=rule)
    progress = state.students["S1"]
    pending = [e for e in progress.exclusions if e.reason == REASON_PENDING]
    assert len(pending) == 1
    assert pending[0].seconds == 7200
    assert pending[0].event_id == "E-01"


def test_replay_with_rules_is_order_independent():
    rule = _rule()
    base_events = [
        _checkin(
            f"E-{i:03d}",
            f"S{i % 3}",
            f"2024-03-{10 + i % 5}T{8 + i % 8:02d}:00:00+08:00",
            f"2024-03-{10 + i % 5}T{9 + i % 8:02d}:00:00+08:00",
            activity_type=["regular", "volunteer"][i % 2],
        )
        for i in range(60)
    ]
    base_events.append(_correction("E-900", "S1", -600, category="on_campus"))
    base_events.append(_correction("E-901", "S2", 300))

    kwargs = dict(required=10800, rules={"R1": rule}, current=rule)
    state_a = _replay(base_events, **kwargs)
    shuffled = list(base_events)
    random.Random(7).shuffle(shuffled)
    state_b = _replay(shuffled, **kwargs)

    for sid in state_a.students:
        a, b = state_a.students[sid], state_b.students[sid]
        assert a.counted_seconds == b.counted_seconds
        assert [
            (c.category, c.attributed_seconds, c.counted_seconds)
            for c in a.categories
        ] == [(c.category, c.attributed_seconds, c.counted_seconds) for c in b.categories]
        assert [(e.reason, e.seconds, e.event_id) for e in a.exclusions] == [
            (e.reason, e.seconds, e.event_id) for e in b.exclusions
        ]
