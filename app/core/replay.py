"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Iterable, Mapping

from .clock import (
    elapsed_seconds,
    intersect_intervals,
    split_by_academic_day,
    subtract_intervals,
    to_lesson_units,
    to_utc,
    union_seconds,
)
from .rules import DEFAULT_RULE_VERSION, RuleDef, default_rule


class EventType(StrEnum):
    CHECKIN = "checkin"
    MENTOR_CONFIRM = "mentor_confirm"
    LEAVE_CORRECTION = "leave_correction"


class CheckinStatus(StrEnum):
    CONFIRMED = "CONFIRMED"
    PENDING = "PENDING"


INTERNSHIP_TYPE = "internship"
UNCATEGORIZED_KEY = "uncategorized"


@dataclass(frozen=True)
class Event:
    """封装领域状态与业务约束。"""

    event_id: str
    plan_version: str
    event_type: EventType
    student_id: str
    payload: dict[str, Any]
    created_at: datetime
    rule_version: str | None = None


@dataclass
class _Segment:
    """签到被授权区间截断后的可计入片段。"""

    start: datetime
    end: datetime
    category: str | None
    event_id: str


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
    category: str | None = None
    countable_seconds: int = 0
    allocated_seconds: int = 0
    outside_window_seconds: int = 0
    overlap_seconds: int = 0
    countable_segments: list[_Segment] = field(default_factory=list)
    pieces: list[tuple[datetime, datetime, str | None]] = field(default_factory=list)

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
    category: str | None = None


@dataclass
class DayTotal:
    academic_day: str
    seconds: int
    categories: dict[str, int] = field(default_factory=dict)


@dataclass
class CategoryProgress:
    """单个活动类别的分项进度与达标依据。"""

    category: str
    counted_seconds: int = 0
    pending_seconds: int = 0
    adjustment_seconds: int = 0
    credited_seconds: int = 0
    min_permille: int | None = None
    max_permille: int | None = None
    min_seconds: int | None = None
    max_seconds: int | None = None
    gap_seconds: int = 0
    excess_seconds: int = 0
    min_met: bool = True
    lesson_units: int = 0


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
    countable_seconds: int = 0
    uncategorized_seconds: int = 0
    total_gap_seconds: int = 0
    categories: list[CategoryProgress] = field(default_factory=list)
    non_countable: list[dict[str, Any]] = field(default_factory=list)
    event_rule_versions: list[str] = field(default_factory=list)
    daily: list[DayTotal] = field(default_factory=list)
    checkins: list[CheckinRecord] = field(default_factory=list)
    adjustments: list[Adjustment] = field(default_factory=list)


@dataclass
class ReplayState:
    plan_version: str
    timezone: str
    required_seconds: int
    students: dict[str, StudentProgress]
    active_rule_version: str | None = None
    rule_versions: list[str] = field(default_factory=list)


def _parse_checkin(event: Event, rule: RuleDef) -> CheckinRecord:
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
        rule_version=event.rule_version or DEFAULT_RULE_VERSION,
        category=rule.category_for(activity_type),
    )


def _truncate_record(
    record: CheckinRecord, rule: RuleDef
) -> tuple[list[_Segment], int]:
    """按该类别的授权区间截断签到，返回可计入片段与区间外秒数。"""
    if record.category is None:
        segment = _Segment(
            record.start_utc, record.end_utc, None, record.event_id
        )
        return [segment], 0
    windows = rule.windows_for(record.category)
    if not windows:
        segment = _Segment(
            record.start_utc, record.end_utc, record.category, record.event_id
        )
        return [segment], 0
    interval = (record.start_utc, record.end_utc)
    inside = intersect_intervals(interval, windows)
    outside = subtract_intervals(interval, windows)
    segments = [
        _Segment(seg_start, seg_end, record.category, record.event_id)
        for seg_start, seg_end in inside
    ]
    outside_seconds = sum(elapsed_seconds(s, e) for s, e in outside)
    return segments, outside_seconds


def _priority_key(active_rule: RuleDef):
    """重叠片段的归属优先级：规则中类别顺序优先，未知类别次之，未分类最后。"""
    order = {c.key: rank for rank, c in enumerate(active_rule.categories)}

    def key(segment: _Segment) -> tuple[int, str, str]:
        if segment.category is None:
            return (1_000_000, "￿", segment.event_id)
        return (order.get(segment.category, 500_000), segment.category, segment.event_id)

    return key


def _allocate(
    segments: list[_Segment],
    priority_key,
) -> tuple[list[tuple[datetime, datetime, str | None, str]], dict[str, int]]:
    """扫描线合并重叠片段：每个原子区间只归属一个类别，其余记为重叠损耗。"""
    if not segments:
        return [], {}
    points = sorted({s.start for s in segments} | {s.end for s in segments})
    by_start: dict[datetime, list[_Segment]] = {}
    for segment in segments:
        by_start.setdefault(segment.start, []).append(segment)

    active: list[_Segment] = []
    allocated: list[tuple[datetime, datetime, str | None, str]] = []
    overlap_loss: dict[str, int] = {}
    for index, point in enumerate(points):
        if index > 0:
            prev = points[index - 1]
            if active and point > prev:
                duration = elapsed_seconds(prev, point)
                winner = min(active, key=priority_key)
                allocated.append((prev, point, winner.category, winner.event_id))
                for segment in active:
                    if segment is not winner:
                        overlap_loss[segment.event_id] = (
                            overlap_loss.get(segment.event_id, 0) + duration
                        )
        if active:
            active = [s for s in active if s.end > point]
        active.extend(by_start.get(point, ()))
    return allocated, overlap_loss


def replay(
    events: Iterable[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    up_to_event_id: str | None = None,
    rules: Mapping[str, RuleDef] | None = None,
    active_rule_version: str | None = None,
) -> ReplayState:
    """执行确定性的业务处理。"""
    sorted_events = sorted(
        (e for e in events if e.plan_version == plan_version),
        key=lambda e: e.event_id,
    )
    if up_to_event_id is not None:
        sorted_events = [e for e in sorted_events if e.event_id <= up_to_event_id]

    rule_defs: Mapping[str, RuleDef] = rules or {}

    def resolve(version: str | None) -> RuleDef:
        if version is None:
            return default_rule()
        return rule_defs.get(version) or default_rule()

    active_label = active_rule_version or DEFAULT_RULE_VERSION
    active_rule = rule_defs.get(active_label) or default_rule()
    priority_key = _priority_key(active_rule)

    checkins_by_student: dict[str, list[CheckinRecord]] = {}
    checkin_index: dict[str, CheckinRecord] = {}
    adjustments_by_student: dict[str, list[Adjustment]] = {}
    versions_by_student: dict[str, set[str]] = {}
    involved_versions: set[str] = set()

    for event in sorted_events:
        pinned_version = event.rule_version or DEFAULT_RULE_VERSION
        involved_versions.add(pinned_version)
        versions_by_student.setdefault(event.student_id, set()).add(pinned_version)
        if event.event_type == EventType.CHECKIN:
            rule = resolve(event.rule_version)
            record = _parse_checkin(event, rule)
            segments, outside_seconds = _truncate_record(record, rule)
            record.countable_segments = segments
            record.outside_window_seconds = outside_seconds
            record.countable_seconds = sum(
                elapsed_seconds(s.start, s.end) for s in segments
            )
            checkins_by_student.setdefault(event.student_id, []).append(record)
            checkin_index[event.event_id] = record
        elif event.event_type == EventType.MENTOR_CONFIRM:
            target_id = event.payload.get("checkin_event_id")
            target = checkin_index.get(target_id)
            if target is not None and target.student_id == event.student_id:
                target.status = CheckinStatus.CONFIRMED
        elif event.event_type == EventType.LEAVE_CORRECTION:
            seconds = int(event.payload.get("adjustment_seconds", 0))
            category = event.payload.get("category")
            if category is not None:
                category = str(category).strip() or None
            adjustments_by_student.setdefault(event.student_id, []).append(
                Adjustment(
                    event_id=event.event_id,
                    student_id=event.student_id,
                    seconds=seconds,
                    reason=str(event.payload.get("reason", "")),
                    category=category,
                )
            )

    all_students = set(checkins_by_student) | set(adjustments_by_student)
    students: dict[str, StudentProgress] = {}
    for student_id in all_students:
        records = checkins_by_student.get(student_id, [])
        adjustments = adjustments_by_student.get(student_id, [])

        confirmed_records = [r for r in records if r.counts]
        pending_records = [r for r in records if r.status == CheckinStatus.PENDING]

        confirmed_seconds = union_seconds(
            [(r.start_utc, r.end_utc) for r in confirmed_records]
        )

        confirmed_segments = [
            segment for r in confirmed_records for segment in r.countable_segments
        ]
        pending_segments = [
            segment for r in pending_records for segment in r.countable_segments
        ]

        allocated, overlap_loss = _allocate(confirmed_segments, priority_key)
        pending_allocated, _ = _allocate(pending_segments, priority_key)

        counted_by_category: dict[str | None, int] = {}
        pieces_by_event: dict[str, list[tuple[datetime, datetime, str | None]]] = {}
        for seg_start, seg_end, category, event_id in allocated:
            duration = elapsed_seconds(seg_start, seg_end)
            counted_by_category[category] = (
                counted_by_category.get(category, 0) + duration
            )
            pieces_by_event.setdefault(event_id, []).append(
                (seg_start, seg_end, category)
            )

        for record in confirmed_records:
            record.overlap_seconds = overlap_loss.get(record.event_id, 0)
            record.pieces = pieces_by_event.get(record.event_id, [])
            record.allocated_seconds = sum(
                elapsed_seconds(start, end) for start, end, _ in record.pieces
            )

        pending_by_category: dict[str | None, int] = {}
        for seg_start, seg_end, category, _ in pending_allocated:
            pending_by_category[category] = pending_by_category.get(
                category, 0
            ) + elapsed_seconds(seg_start, seg_end)
        pending_seconds = sum(pending_by_category.values())

        day_totals: dict[str, int] = {}
        day_categories: dict[str, dict[str, int]] = {}
        for seg_start, seg_end, category, _ in allocated:
            for day, part_start, part_end in split_by_academic_day(
                seg_start, seg_end, timezone_name
            ):
                key = day.isoformat()
                duration = elapsed_seconds(part_start, part_end)
                day_totals[key] = day_totals.get(key, 0) + duration
                category_key = category if category is not None else UNCATEGORIZED_KEY
                bucket = day_categories.setdefault(key, {})
                bucket[category_key] = bucket.get(category_key, 0) + duration
        daily = [
            DayTotal(
                academic_day=day,
                seconds=secs,
                categories=day_categories.get(day, {}),
            )
            for day, secs in sorted(day_totals.items())
        ]

        adjustment_seconds = sum(a.seconds for a in adjustments)
        adjustment_by_category: dict[str, int] = {}
        adjustment_uncategorized = 0
        for adjustment in adjustments:
            if adjustment.category is None:
                adjustment_uncategorized += adjustment.seconds
            else:
                adjustment_by_category[adjustment.category] = (
                    adjustment_by_category.get(adjustment.category, 0)
                    + adjustment.seconds
                )

        ordered_keys = [c.key for c in active_rule.categories]
        extra_keys = sorted(
            key
            for key in (
                set(counted_by_category)
                | set(adjustment_by_category)
                | set(pending_by_category)
            ) - {None}
            if key not in ordered_keys
        )
        categories: list[CategoryProgress] = []
        for key in ordered_keys + extra_keys:
            rule_category = active_rule.category_rule(key)
            counted = counted_by_category.get(key, 0)
            adjustment = adjustment_by_category.get(key, 0)
            adjusted = counted + adjustment
            min_seconds = (
                rule_category.min_seconds(required_seconds)
                if rule_category is not None
                else None
            )
            max_seconds = (
                rule_category.max_seconds(required_seconds)
                if rule_category is not None
                else None
            )
            credited = max(adjusted, 0)
            excess = 0
            if max_seconds is not None and credited > max_seconds:
                excess = credited - max_seconds
                credited = max_seconds
            gap = (
                max(0, min_seconds - credited) if min_seconds is not None else 0
            )
            categories.append(
                CategoryProgress(
                    category=key,
                    counted_seconds=counted,
                    pending_seconds=pending_by_category.get(key, 0),
                    adjustment_seconds=adjustment,
                    credited_seconds=credited,
                    min_permille=(
                        rule_category.min_permille
                        if rule_category is not None
                        else None
                    ),
                    max_permille=(
                        rule_category.max_permille
                        if rule_category is not None
                        else None
                    ),
                    min_seconds=min_seconds,
                    max_seconds=max_seconds,
                    gap_seconds=gap,
                    excess_seconds=excess,
                    min_met=gap == 0,
                    lesson_units=to_lesson_units(credited),
                )
            )

        uncategorized_seconds = counted_by_category.get(None, 0)
        total_seconds = (
            sum(c.credited_seconds for c in categories)
            + uncategorized_seconds
            + adjustment_uncategorized
        )
        if total_seconds < 0:
            total_seconds = 0
        total_gap_seconds = max(0, required_seconds - total_seconds)
        meets_requirement = total_seconds >= required_seconds and all(
            c.gap_seconds == 0 for c in categories
        )

        non_countable: list[dict[str, Any]] = []
        outside_seconds = sum(r.outside_window_seconds for r in records)
        if outside_seconds:
            non_countable.append(
                {
                    "reason": "outside_authorization_window",
                    "seconds": outside_seconds,
                }
            )
        overlap_seconds = sum(overlap_loss.values())
        if overlap_seconds:
            non_countable.append(
                {
                    "reason": "overlap_attributed_elsewhere",
                    "seconds": overlap_seconds,
                }
            )
        if pending_seconds:
            non_countable.append(
                {"reason": "pending_confirmation", "seconds": pending_seconds}
            )
        for category in categories:
            if category.excess_seconds:
                non_countable.append(
                    {
                        "reason": "exceeds_category_cap",
                        "category": category.category,
                        "seconds": category.excess_seconds,
                    }
                )

        students[student_id] = StudentProgress(
            student_id=student_id,
            confirmed_seconds=confirmed_seconds,
            pending_seconds=pending_seconds,
            adjustment_seconds=adjustment_seconds,
            total_seconds=total_seconds,
            lesson_units=to_lesson_units(total_seconds),
            pending_lesson_units=to_lesson_units(pending_seconds),
            meets_requirement=meets_requirement,
            countable_seconds=sum(counted_by_category.values()),
            uncategorized_seconds=uncategorized_seconds,
            total_gap_seconds=total_gap_seconds,
            categories=categories,
            non_countable=non_countable,
            event_rule_versions=sorted(versions_by_student.get(student_id, ())),
            daily=daily,
            checkins=sorted(records, key=lambda r: r.start_utc),
            adjustments=sorted(adjustments, key=lambda a: a.event_id),
        )

    return ReplayState(
        plan_version=plan_version,
        timezone=timezone_name,
        required_seconds=required_seconds,
        students=students,
        active_rule_version=active_label,
        rule_versions=sorted(involved_versions),
    )


def explain_checkin(record: CheckinRecord, tz_name: str) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    segments = split_by_academic_day(record.start_utc, record.end_utc, tz_name)
    non_countable: list[dict[str, Any]] = []
    if record.status == CheckinStatus.PENDING:
        non_countable.append(
            {"reason": "pending_confirmation", "seconds": record.seconds}
        )
    if record.outside_window_seconds:
        non_countable.append(
            {
                "reason": "outside_authorization_window",
                "seconds": record.outside_window_seconds,
            }
        )
    if record.overlap_seconds:
        non_countable.append(
            {
                "reason": "overlap_attributed_elsewhere",
                "seconds": record.overlap_seconds,
            }
        )
    return {
        "event_id": record.event_id,
        "activity_id": record.activity_id,
        "activity_type": record.activity_type,
        "status": record.status.value,
        "counts": record.counts,
        "rule_version": record.rule_version,
        "category": record.category,
        "countable_seconds": record.countable_seconds,
        "allocated_seconds": record.allocated_seconds,
        "non_countable": non_countable,
        "segments": [
            {
                "start_utc": seg_start.isoformat().replace("+00:00", "Z"),
                "end_utc": seg_end.isoformat().replace("+00:00", "Z"),
                "seconds": elapsed_seconds(seg_start, seg_end),
                "category": category,
            }
            for seg_start, seg_end, category in record.pieces
        ],
        "check_in_at_utc": record.start_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "check_out_at_utc": record.end_utc.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
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
    }
