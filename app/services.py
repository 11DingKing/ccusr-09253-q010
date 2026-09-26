"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from .core.rules import DEFAULT_RULE_VERSION, RuleDef, default_rule
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    get_freeze,
    get_plan,
    get_rule,
    insert_events,
    insert_freeze,
    insert_rule,
    list_rule_rows,
    load_events,
    load_events_up_to,
    max_event_id,
    set_active_rule,
    upsert_plan,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class RuleNotFoundError(Exception):
    pass


class RuleConflictError(Exception):
    pass


def _plan_out(plan) -> dict[str, Any]:
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
        "active_rule_version": plan.active_rule_version,
    }


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return _plan_out(plan)


def ensure_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> dict[str, Any]:
    plan = upsert_plan(
        db,
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    if plan.active_rule_version is None:
        # 注册隐式旧版规则，保证每个事件都能固定到一个规则版本。
        insert_rule(
            db,
            plan_version=plan_version,
            rule_version=DEFAULT_RULE_VERSION,
            definition=default_rule().to_dict(),
        )
        plan = set_active_rule(db, plan_version, DEFAULT_RULE_VERSION)
    return _plan_out(plan)


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _rule_out(plan, row) -> dict[str, Any]:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return {
        "plan_version": plan.plan_version,
        "rule_version": row.rule_version,
        "definition": row.definition,
        "is_active": plan.active_rule_version == row.rule_version,
        "created_at": created_at.isoformat().replace("+00:00", "Z"),
    }


def put_rule(
    db: Session,
    *,
    plan_version: str,
    rule_version: str,
    definition: dict[str, Any],
    activate: bool,
) -> tuple[dict[str, Any], bool]:
    """创建不可变规则版本；同版本不同内容视为冲突。"""
    plan = _require_plan(db, plan_version)
    rule_def = RuleDef.from_dict(rule_version, definition)
    normalized = rule_def.to_dict()
    row = get_rule(db, plan_version, rule_version)
    created = False
    if row is None:
        row = insert_rule(
            db,
            plan_version=plan_version,
            rule_version=rule_version,
            definition=normalized,
        )
        assert row is not None
        created = True
    elif row.definition != normalized:
        raise RuleConflictError(
            f"rule version '{rule_version}' already exists with a different "
            "definition"
        )
    if activate:
        plan = set_active_rule(db, plan_version, rule_version)
    return _rule_out(plan, row), created


def activate_rule(
    db: Session, *, plan_version: str, rule_version: str
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    row = get_rule(db, plan_version, rule_version)
    if row is None:
        raise RuleNotFoundError(
            f"rule version '{rule_version}' is not registered for plan "
            f"'{plan_version}'"
        )
    plan = set_active_rule(db, plan_version, rule_version)
    return _rule_out(plan, row)


def list_rules(db: Session, plan_version: str) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    rows = list_rule_rows(db, plan_version)
    return {
        "plan_version": plan_version,
        "active_rule_version": plan.active_rule_version,
        "rules": [_rule_out(plan, row) for row in rows],
    }


def get_rule_detail(
    db: Session, plan_version: str, rule_version: str
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    row = get_rule(db, plan_version, rule_version)
    if row is None:
        raise RuleNotFoundError(
            f"rule version '{rule_version}' is not registered for plan "
            f"'{plan_version}'"
        )
    return _rule_out(plan, row)


def _rule_defs(db: Session, plan) -> dict[str, RuleDef]:
    defs = {DEFAULT_RULE_VERSION: default_rule()}
    for row in list_rule_rows(db, plan.plan_version):
        defs[row.rule_version] = RuleDef.from_dict(row.rule_version, row.definition)
    return defs


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    accepted, duplicates = insert_events(
        db,
        plan_version=plan_version,
        events=events,
        rule_version=plan.active_rule_version,
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        rules=_rule_defs(db, plan),
        active_rule_version=plan.active_rule_version,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def warnings_report(
    db: Session, plan_version: str, *, include_compliant: bool = False
) -> dict[str, Any]:
    """批量预警：汇总未达标学生的总缺口与各分项缺口。"""
    snap = current_snapshot(db, plan_version)
    entries: list[dict[str, Any]] = []
    for student in snap.students:
        if student["meets_requirement"] and not include_compliant:
            continue
        entries.append(
            {
                "student_id": student["student_id"],
                "meets_requirement": student["meets_requirement"],
                "total_seconds": student["total_seconds"],
                "required_seconds": snap.required_seconds,
                "total_gap_seconds": student["total_gap_seconds"],
                "pending_seconds": student["pending_seconds"],
                "category_gaps": [
                    {
                        "category": category["category"],
                        "min_seconds": category["min_seconds"],
                        "credited_seconds": category["credited_seconds"],
                        "gap_seconds": category["gap_seconds"],
                    }
                    for category in student["categories"]
                    if category["gap_seconds"]
                ],
                "non_countable": student["non_countable"],
            }
        )
    return {
        "plan_version": plan_version,
        "rule_version": snap.rule_version,
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "students_at_risk": len(entries),
        "warnings": entries,
    }


def freeze_semester(
    db: Session, *, plan_version: str, freeze_id: str
) -> tuple[Snapshot, bool]:
    """执行确定性的业务处理。"""
    plan = _require_plan(db, plan_version)
    existing = get_freeze(db, plan_version, freeze_id)
    if existing is not None:
        return Snapshot.from_dict(existing.snapshot), False

    cutoff = max_event_id(db, plan_version)
    events = load_events(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
        rules=_rule_defs(db, plan),
        active_rule_version=plan.active_rule_version,
    )
    row = insert_freeze(
        db,
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snap.to_dict(),
        event_cutoff_id=cutoff,
    )
    if row is None:
        existing = get_freeze(db, plan_version, freeze_id)
        assert existing is not None
        return Snapshot.from_dict(existing.snapshot), False
    return snap, True


def get_frozen_snapshot(
    db: Session, plan_version: str, freeze_id: str
) -> Snapshot:
    _require_plan(db, plan_version)
    row = get_freeze(db, plan_version, freeze_id)
    if row is None:
        raise FreezeNotFoundError(
            f"freeze '{freeze_id}' for plan '{plan_version}' does not exist"
        )
    return Snapshot.from_dict(row.snapshot)


def explain_frozen_student(
    db: Session, plan_version: str, freeze_id: str, student_id: str
) -> dict[str, Any] | None:
    snap = get_frozen_snapshot(db, plan_version, freeze_id)
    return explain_student(snap, student_id)


def diff_freezes(
    db: Session, plan_version: str, old_freeze_id: str, new_freeze_id: str
) -> dict[str, Any]:
    old = get_frozen_snapshot(db, plan_version, old_freeze_id)
    new = get_frozen_snapshot(db, plan_version, new_freeze_id)
    return diff_snapshots(old, new)
