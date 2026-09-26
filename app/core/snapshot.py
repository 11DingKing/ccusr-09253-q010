"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .replay import (
    CheckinRecord,
    Event,
    ReplayState,
    StudentProgress,
    explain_checkin,
    replay,
)
from .rules import RuleConfig


@dataclass
class Snapshot:
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]
    # 达标判定所用的现行规则版本；None 表示旧版总量语义。
    rule_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": self.plan_version,
            "freeze_id": self.freeze_id,
            "timezone": self.timezone,
            "required_seconds": self.required_seconds,
            "rule_version": self.rule_version,
            "generated_at": self.generated_at,
            "event_cutoff_id": self.event_cutoff_id,
            "students": self.students,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            plan_version=data["plan_version"],
            freeze_id=data.get("freeze_id"),
            timezone=data["timezone"],
            required_seconds=data["required_seconds"],
            generated_at=data["generated_at"],
            event_cutoff_id=data.get("event_cutoff_id"),
            students=list(data.get("students", [])),
            rule_version=data.get("rule_version"),
        )


def _student_to_dict(progress: StudentProgress, tz_name: str) -> dict[str, Any]:
    return {
        "student_id": progress.student_id,
        "confirmed_seconds": progress.confirmed_seconds,
        "pending_seconds": progress.pending_seconds,
        "adjustment_seconds": progress.adjustment_seconds,
        "total_seconds": progress.total_seconds,
        "counted_seconds": progress.counted_seconds,
        "total_shortfall_seconds": progress.total_shortfall_seconds,
        "lesson_units": progress.lesson_units,
        "pending_lesson_units": progress.pending_lesson_units,
        "meets_requirement": progress.meets_requirement,
        "daily": [
            {"academic_day": d.academic_day, "seconds": d.seconds}
            for d in progress.daily
        ],
        "daily_categories": [
            {
                "academic_day": d.academic_day,
                "category": d.category,
                "seconds": d.seconds,
            }
            for d in progress.daily_categories
        ],
        "categories": [
            {
                "category": c.category,
                "attributed_seconds": c.attributed_seconds,
                "adjustment_seconds": c.adjustment_seconds,
                "counted_seconds": c.counted_seconds,
                "min_seconds": c.min_seconds,
                "max_seconds": c.max_seconds,
                "shortfall_seconds": c.shortfall_seconds,
                "over_cap_seconds": c.over_cap_seconds,
                "meets_minimum": c.meets_minimum,
            }
            for c in progress.categories
        ],
        "exclusions": [
            {
                "reason": e.reason,
                "seconds": e.seconds,
                "event_id": e.event_id,
                "category": e.category,
            }
            for e in progress.exclusions
        ],
        "checkins": [explain_checkin(c, tz_name) for c in progress.checkins],
        "adjustments": [
            {
                "event_id": a.event_id,
                "seconds": a.seconds,
                "reason": a.reason,
                "category": a.category,
            }
            for a in progress.adjustments
        ],
    }


def build_snapshot(
    events: list[Event],
    *,
    plan_version: str,
    timezone_name: str,
    required_seconds: int,
    rules: Mapping[str, RuleConfig] | None = None,
    current_rule: RuleConfig | None = None,
    freeze_id: str | None = None,
    event_cutoff_id: str | None = None,
    generated_at: datetime | None = None,
) -> Snapshot:
    """执行确定性的业务处理。"""
    state: ReplayState = replay(
        events,
        plan_version=plan_version,
        timezone_name=timezone_name,
        required_seconds=required_seconds,
        rules=rules,
        current_rule=current_rule,
        up_to_event_id=event_cutoff_id,
    )
    if generated_at is None:
        generated_at = datetime.now(timezone.utc)
    generated_at = generated_at.astimezone(timezone.utc)

    students = [
        _student_to_dict(state.students[sid], timezone_name)
        for sid in sorted(state.students)
    ]

    return Snapshot(
        plan_version=plan_version,
        freeze_id=freeze_id,
        timezone=timezone_name,
        required_seconds=required_seconds,
        generated_at=generated_at.isoformat().replace("+00:00", "Z"),
        event_cutoff_id=event_cutoff_id,
        students=students,
        rule_version=state.rule_version,
    )


def _index_students(snapshot: Snapshot) -> dict[str, dict[str, Any]]:
    return {s["student_id"]: s for s in snapshot.students}


def diff_snapshots(old: Snapshot, new: Snapshot) -> dict[str, Any]:
    """执行确定性的业务处理。"""
    old_map = _index_students(old)
    new_map = _index_students(new)
    all_ids = sorted(set(old_map) | set(new_map))

    student_changes: list[dict[str, Any]] = []
    for sid in all_ids:
        before = old_map.get(sid)
        after = new_map.get(sid)
        if before is None and after is not None:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "added",
                    "before": None,
                    "after": {
                        "total_seconds": after["total_seconds"],
                        "counted_seconds": after.get("counted_seconds"),
                        "lesson_units": after["lesson_units"],
                        "meets_requirement": after["meets_requirement"],
                    },
                }
            )
            continue
        if after is None and before is not None:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "removed",
                    "before": {
                        "total_seconds": before["total_seconds"],
                        "counted_seconds": before.get("counted_seconds"),
                        "lesson_units": before["lesson_units"],
                        "meets_requirement": before["meets_requirement"],
                    },
                    "after": None,
                }
            )
            continue

        assert before is not None and after is not None
        fields = (
            "confirmed_seconds",
            "pending_seconds",
            "adjustment_seconds",
            "total_seconds",
            "counted_seconds",
            "total_shortfall_seconds",
            "lesson_units",
            "pending_lesson_units",
            "meets_requirement",
        )
        changed_fields = {}
        for field_name in fields:
            if before.get(field_name) != after.get(field_name):
                changed_fields[field_name] = {
                    "before": before.get(field_name),
                    "after": after.get(field_name),
                }
        if changed_fields:
            student_changes.append(
                {
                    "student_id": sid,
                    "change_type": "modified",
                    "fields": changed_fields,
                }
            )

    return {
        "plan_version": old.plan_version,
        "old_freeze_id": old.freeze_id,
        "new_freeze_id": new.freeze_id,
        "old_generated_at": old.generated_at,
        "new_generated_at": new.generated_at,
        "old_event_cutoff_id": old.event_cutoff_id,
        "new_event_cutoff_id": new.event_cutoff_id,
        "old_rule_version": old.rule_version,
        "new_rule_version": new.rule_version,
        "student_changes": student_changes,
        "students_affected": len(student_changes),
    }


def explain_student(
    snapshot: Snapshot, student_id: str
) -> dict[str, Any] | None:
    """执行确定性的业务处理。"""
    for student in snapshot.students:
        if student["student_id"] == student_id:
            return student
    return None
