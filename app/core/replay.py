"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .clock import (
    academic_day,
    elapsed_seconds,
    merge_intervals,
    split_by_academic_day,
    to_utc,
    union_seconds,
)
from .rules import RuleConfig


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"

# 不可计入原因编码，学生维度汇总与单次签到解释共用同一套取值。
REASON_PENDING = "pending_mentor_confirmation"
REASON_NO_WINDOW = "no_authorization_window"
REASON_UNMAPPED_ACTIVITY = "unmapped_activity_type"
REASON_NO_RULE = "no_rule_pinned"
REASON_UNKNOWN_CATEGORY = "unknown_category"
REASON_CAP_EXCEEDED = "category_cap_exceeded"
REASON_NOT_IN_RULE = "category_not_in_current_rule"

# 签到片段的归属来源。
SOURCE_MAPPING = "activity_type_mapping"
SOURCE_WINDOW = "category_window"
SOURCE_NONE = "none"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime
    # 导入事件时钉住的规则版本；None 表示导入时方案尚未发布任何规则。
    rule_version: str | None = None


@dataclass(frozen=True)
class CategoryWindow:
    """签到负载中声明的类别授权区间。"""

    category: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class Segment:
    """一次签到被切分出的连续片段：要么归属某个类别，要么携带不可计入原因。"""

    start_utc: datetime
    end_utc: datetime
    category: str | None
    source: str
    reason: str | None
    event_id: str

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)


@dataclass
class CheckinRecord:
    event_id: str
    student_id: str
    activity_id: str
    activity_type: str
    start_utc: datetime
    end_utc: datetime
    status: CheckinStatus
    rule_version: str | None = None
    # None 表示负载未声明授权区间；空列表表示声明了但全部非法。
    windows: list[CategoryWindow] | None = None
    segments: list[Segment] = field(default_factory=list)

    @property
    def seconds(self) -> int:
        return elapsed_seconds(self.start_utc, self.end_utc)

    @property
    def counts(self) -> bool:
        return self.status == CheckinStatus.CONFIRMED


@dataclass
class Adjustment:
    event_id: str
    student_id: str
    seconds: int
    reason: str
    # 可选：修正定向到某个类别；缺省时只影响计入总量。
    category: str | None = None


@dataclass
class DayTotal:
    academic_day: str
    seconds: int


@dataclass
class DayCategoryTotal:
    academic_day: str
    category: str
    seconds: int


@dataclass
class CategoryProgress:
    """单个活动类别在现行规则下的达标情况。"""

    category: str
    attributed_seconds: int
    adjustment_seconds: int
    counted_seconds: int
    min_seconds: int
    max_seconds: int
    shortfall_seconds: int
    over_cap_seconds: int
    meets_minimum: bool


@dataclass
class Exclusion:
    """一段无法计入达标要求的时长及其原因。"""

    reason: str
    seconds: int
    event_id: str | None = None
    category: str | None = None


@dataclass
class StudentProgress:
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    counted_seconds: int = 0
    total_shortfall_seconds: int = 0
    daily: list[DayTotal] = field(default_factory=list)
    daily_categories: list[DayCategoryTotal] = field(default_factory=list)
    categories: list[CategoryProgress] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]
    # 本次重放用于达标判定的现行规则版本；无规则时为 None（旧版总量语义）。
    rule_version: str | None = None


def _parse_windows(payload: Mapping[str, Any]) -> list[CategoryWindow] | None:
    """解析签到负载中的类别授权区间；非法条目跳过，键缺失时返回 None。"""
    raw = payload.get("category_windows")
    if raw is None:
        return None
    windows: list[CategoryWindow] = []
    for item in raw:
        try:
            category = str(item["category"]).strip()
            start = to_utc(datetime.fromisoformat(item["start"]))
            end = to_utc(datetime.fromisoformat(item["end"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not category or end <= start:
            continue
        windows.append(CategoryWindow(category=category, start_utc=start, end_utc=end))
    return windows


def _parse_checkin(
    event: Event, tz_name: str
) -> CheckinRecord:
    start = to_utc(datetime.fromisoformat(event.payload["check_in_at"]))
    end = to_utc(datetime.fromisoformat(event.payload["check_out_at"]))
    activity_type = event.payload.get("activity_type", "regular")
    requires_confirmation = activity_type == INTERNSHIP_TYPE
    status = (
        CheckinStatus.PENDING if requires_confirmation else CheckinStatus.CONFIRMED
    )
    return CheckinRecord(
        event_id=event.event_id,
        student_id=event.student_id,
        activity_id=event.payload.get("activity_id", ""),
        activity_type=activity_type,
        start_utc=start,
        end_utc=end,
        status=status,
        rule_version=event.rule_version,
        windows=_parse_windows(event.payload),
    )


def _subtract_spans(
    span: tuple[datetime, datetime],
    covered: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """从 span 中扣除 covered 里的所有区间，返回剩余片段。"""
    remaining = [span]
    for c_start, c_end in covered:
        next_remaining: list[tuple[datetime, datetime]] = []
        for s, e in remaining:
            if c_end <= s or c_start >= e:
                next_remaining.append((s, e))
                continue
            if s < c_start:
                next_remaining.append((s, c_start))
            if c_end < e:
                next_remaining.append((c_end, e))
        remaining = next_remaining
    return remaining


def _segment_checkin(
    record: CheckinRecord, pinned_rule: RuleConfig | None
) -> list[Segment]:
    """把一次签到切分为类别片段与不可计入片段，片段完整覆盖签到区间。"""
    start, end = record.start_utc, record.end_utc
    event_id = record.event_id

    if record.windows is not None:
        # 显式授权区间：区间求交后先到先得，缝隙视为未授权。
        known = (
            set(pinned_rule.category_codes()) if pinned_rule is not None else None
        )
        segments: list[Segment] = []
        claimed: list[tuple[datetime, datetime]] = []
        ordered = sorted(
            record.windows, key=lambda w: (w.start_utc, w.end_utc, w.category)
        )
        for window in ordered:
            clip_start = max(window.start_utc, start)
            clip_end = min(window.end_utc, end)
            if clip_end <= clip_start:
                continue
            for piece_start, piece_end in _subtract_spans(
                (clip_start, clip_end), claimed
            ):
                claimed.append((piece_start, piece_end))
                if known is not None and window.category not in known:
                    segments.append(
                        Segment(
                            piece_start,
                            piece_end,
                            None,
                            SOURCE_WINDOW,
                            REASON_UNKNOWN_CATEGORY,
                            event_id,
                        )
                    )
                else:
                    segments.append(
                        Segment(
                            piece_start,
                            piece_end,
                            window.category,
                            SOURCE_WINDOW,
                            None,
                            event_id,
                        )
                    )
        for gap_start, gap_end in _subtract_spans(
            (start, end), merge_intervals(claimed)
        ):
            segments.append(
                Segment(gap_start, gap_end, None, SOURCE_NONE, REASON_NO_WINDOW, event_id)
            )
        segments.sort(key=lambda s: (s.start_utc, s.end_utc))
        return segments

    if pinned_rule is None:
        return [Segment(start, end, None, SOURCE_NONE, REASON_NO_RULE, event_id)]
    category = pinned_rule.activity_category_map.get(record.activity_type)
    if category is None:
        return [Segment(start, end, None, SOURCE_NONE, REASON_UNMAPPED_ACTIVITY, event_id)]
    return [Segment(start, end, category, SOURCE_MAPPING, None, event_id)]


def _allocate_segments(
    segments: list[Segment], category_priority: Mapping[str, int]
) -> tuple[dict[str, int], dict[tuple[str, str], int], list[tuple[datetime, datetime, str]]]:
    """在合并后的时间线上按类别分配秒数。

    所有签到片段的端点把时间线切成基本区间；每个基本区间内若被多个类别
    覆盖，则按（规则声明顺序、事件号、类别编码）取胜者，保证重叠时长只
    计入一次。返回各类别秒数、按（原因, 事件）聚合的未归属秒数，以及已
    归属的时间片（供跨日拆分使用）。
    """
    if not segments:
        return {}, {}, []
    points = sorted({s.start_utc for s in segments} | {s.end_utc for s in segments})
    unknown_rank = len(category_priority)
    category_seconds: dict[str, int] = {}
    unattributed: dict[tuple[str, str], int] = {}
    allocated_spans: list[tuple[datetime, datetime, str]] = []
    for a, b in zip(points, points[1:]):
        covering = [s for s in segments if s.start_utc <= a and s.end_utc >= b]
        if not covering:
            continue
        attributed = [s for s in covering if s.category is not None]
        seconds = elapsed_seconds(a, b)
        if attributed:
            winner = min(
                attributed,
                key=lambda s: (
                    category_priority.get(s.category, unknown_rank),
                    s.event_id,
                    s.category or "",
                ),
            )
            assert winner.category is not None
            category_seconds[winner.category] = (
                category_seconds.get(winner.category, 0) + seconds
            )
            allocated_spans.append((a, b, winner.category))
        else:
            winner = min(covering, key=lambda s: s.event_id)
            reason = winner.reason or REASON_NO_WINDOW
            key = (reason, winner.event_id)
            unattributed[key] = unattributed.get(key, 0) + seconds
    return category_seconds, unattributed, allocated_spans


@dataclass
class _CategoryEvaluation:
    categories: list[CategoryProgress]
    exclusions: list[Exclusion]
    counted_seconds: int
    total_shortfall_seconds: int
    meets_requirement: bool
    daily_categories: list[DayCategoryTotal]


def _evaluate_categories(
    records: list[CheckinRecord],
    adjustments: list[Adjustment],
    rules: Mapping[str, RuleConfig],
    current_rule: RuleConfig,
    required_seconds: int,
    tz_name: str,
) -> _CategoryEvaluation:
    """合并重叠区间后按活动类别分配时长，计算达标缺口与不可计入原因。"""
    segments: list[Segment] = []
    for record in records:
        if not record.counts:
            continue
        pinned = rules.get(record.rule_version) if record.rule_version else None
        record_segments = _segment_checkin(record, pinned)
        record.segments = record_segments
        segments.extend(record_segments)

    category_seconds, unattributed, allocated_spans = _allocate_segments(
        segments, current_rule.category_priority()
    )

    rule_codes = set(current_rule.category_codes())
    category_adjustments: dict[str, int] = {}
    uncategorized_adjustment = 0
    for adjustment in adjustments:
        if adjustment.category is not None and adjustment.category in rule_codes:
            category_adjustments[adjustment.category] = (
                category_adjustments.get(adjustment.category, 0) + adjustment.seconds
            )
        else:
            uncategorized_adjustment += adjustment.seconds

    categories: list[CategoryProgress] = []
    exclusions: list[Exclusion] = []
    for limit in current_rule.categories:
        attributed = category_seconds.get(limit.code, 0)
        adj = category_adjustments.get(limit.code, 0)
        effective = attributed + adj
        cap = limit.max_seconds(required_seconds)
        minimum = limit.min_seconds(required_seconds)
        counted = min(max(effective, 0), cap)
        over_cap = max(0, effective - cap)
        categories.append(
            CategoryProgress(
                category=limit.code,
                attributed_seconds=attributed,
                adjustment_seconds=adj,
                counted_seconds=counted,
                min_seconds=minimum,
                max_seconds=cap,
                shortfall_seconds=max(0, minimum - counted),
                over_cap_seconds=over_cap,
                meets_minimum=counted >= minimum,
            )
        )
        if over_cap > 0:
            exclusions.append(
                Exclusion(REASON_CAP_EXCEEDED, over_cap, category=limit.code)
            )

    for code in sorted(category_seconds):
        if code not in rule_codes:
            exclusions.append(
                Exclusion(REASON_NOT_IN_RULE, category_seconds[code], category=code)
            )
    for (reason, event_id), seconds in sorted(unattributed.items()):
        exclusions.append(Exclusion(reason, seconds, event_id=event_id))
    for record in records:
        if record.status == CheckinStatus.PENDING:
            exclusions.append(
                Exclusion(REASON_PENDING, record.seconds, event_id=record.event_id)
            )
    exclusions.sort(key=lambda e: (e.reason, e.event_id or "", e.category or ""))

    counted_seconds = (
        sum(c.counted_seconds for c in categories) + uncategorized_adjustment
    )
    if counted_seconds < 0:
        counted_seconds = 0
    total_shortfall = max(0, required_seconds - counted_seconds)
    meets = counted_seconds >= required_seconds and all(
        c.meets_minimum for c in categories
    )

    day_category: dict[tuple[str, str], int] = {}
    for span_start, span_end, category in allocated_spans:
        for day, seg_start, seg_end in split_by_academic_day(
            span_start, span_end, tz_name
        ):
            key = (day.isoformat(), category)
            day_category[key] = day_category.get(key, 0) + elapsed_seconds(
                seg_start, seg_end
            )
    daily_categories = [
        DayCategoryTotal(academic_day=day, category=category, seconds=seconds)
        for (day, category), seconds in sorted(day_category.items())
    ]

    return _CategoryEvaluation(
        categories=categories,
        exclusions=exclusions,
        counted_seconds=counted_seconds,
        total_shortfall_seconds=total_shortfall,
        meets_requirement=meets,
        daily_categories=daily_categories,
    )


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    rules: Mapping[str, RuleConfig] | None = None,
    current_rule: RuleConfig | None = None,
    up_to_event_id: str | None = None,
) -> ReplayState:
    """执行确定性的业务处理。"""
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}

    for event in sorted_events:
        if event.event_type == EventType.CHECKIN:
            record = _parse_checkin(event, timezone_name)
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            raw_category = event.payload.get("category")
            category = (
                str(raw_category).strip() if raw_category is not None else ""
            )
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                    category=category or None,
                )
            )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed_intervals = [
            (r.start_utc, r.end_utc) for r in records if r.counts
        ]
        pending_intervals = [
            (r.start_utc, r.end_utc)
            for r in records
            if r.status == CheckinStatus.PENDING
        ]

        confirmed_seconds = union_seconds(confirmed_intervals)
        pending_seconds = union_seconds(pending_intervals)
        adjustment_seconds = sum(a.seconds for a in adjustments)
        total_seconds = confirmed_seconds + adjustment_seconds
        if total_seconds < 0:
            total_seconds = 0

        day_totals: dict[str, int] = {}
        for start, end in merge_intervals(confirmed_intervals):
            for day, seg_start, seg_end in split_by_academic_day(
                start, end, timezone_name
            ):
                key = day.isoformat()
                day_totals[key] = day_totals.get(key, 0) + elapsed_seconds(
                    seg_start, seg_end
                )
        daily = [
            DayTotal(academic_day=day, seconds=secs)
            for day, secs in sorted(day_totals.items())
        ]

        if current_rule is not None:
            evaluation = _evaluate_categories(
                records,
                adjustments,
                rules or {},
                current_rule,
                required_seconds,
                timezone_name,
            )
            counted_seconds = evaluation.counted_seconds
            total_shortfall_seconds = evaluation.total_shortfall_seconds
            meets_requirement = evaluation.meets_requirement
            categories = evaluation.categories
            exclusions = evaluation.exclusions
            daily_categories = evaluation.daily_categories
        else:
            # 旧版语义：没有已发布规则时只核算总量。
            counted_seconds = total_seconds
            total_shortfall_seconds = max(0, required_seconds - total_seconds)
            meets_requirement = total_seconds >= required_seconds
            categories = []
            daily_categories = []
            exclusions = [
                Exclusion(REASON_PENDING, r.seconds, event_id=r.event_id)
                for r in records
                if r.status == CheckinStatus.PENDING
            ]

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=total_seconds // (45 * 60),
            pending_lesson_units=pending_seconds // (45 * 60),
            meets_requirement=meets_requirement,
            counted_seconds=counted_seconds,
            total_shortfall_seconds=total_shortfall_seconds,
            daily=daily,
            daily_categories=daily_categories,
            categories=categories,
            exclusions=exclusions,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
        )

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
        rule_version=current_rule.rule_version if current_rule is not None else None,
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)

    def _utc_text(moment: datetime) -> str:
        return (
            moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        )

    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "rule_version": record.rule_version,
        "check_in_at_utc": _utc_text(record.start_utc),
        "check_out_at_utc": _utc_text(record.end_utc),
        "raw_seconds": record.seconds,
        "academic_days": [
            {
                "day": day.isoformat(),
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
            }
            for day, seg_start, seg_end in segments
        ],
        "category_segments": [
            {
                "category": s.category,
                "source": s.source,
                "start_utc": _utc_text(s.start_utc),
                "end_utc": _utc_text(s.end_utc),
                "seconds": s.seconds,
            }
            for s in record.segments
            if s.category is not None
        ],
        "excluded_segments": [
            {
                "reason": s.reason,
                "source": s.source,
                "start_utc": _utc_text(s.start_utc),
                "end_utc": _utc_text(s.end_utc),
                "seconds": s.seconds,
            }
            for s in record.segments
            if s.category is None
        ],
    }
