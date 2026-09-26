"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .core.replay import (
    REASON_NO_RULE,
    REASON_NO_WINDOW,
    REASON_NOT_IN_RULE,
    REASON_UNKNOWN_CATEGORY,
    REASON_UNMAPPED_ACTIVITY,
)
from .core.rules import (
    CategoryLimit,
    RuleConfig,
    parse_ratio,
    validate_categories,
)
from .core.snapshot import Snapshot, build_snapshot, diff_snapshots, explain_student
from .repository import (
    RULE_STATUS_PUBLISHED,
    get_freeze,
    get_plan,
    insert_events,
    insert_freeze,
    list_rule_configs,
    load_events,
    load_events_up_to,
    max_event_id,
    publish_rule_config,
    upsert_plan,
    upsert_rule_config_draft,
)


class PlanNotFoundError(Exception):
    pass


class FreezeConflictError(Exception):
    pass


class FreezeNotFoundError(Exception):
    pass


class RuleNotFoundError(Exception):
    pass


class RuleConfigConflictError(Exception):
    pass


# 学生维度里被视为“未归属时长”的不可计入原因。
UNATTRIBUTED_REASONS = frozenset(
    {
        REASON_NO_WINDOW,
        REASON_UNMAPPED_ACTIVITY,
        REASON_NO_RULE,
        REASON_UNKNOWN_CATEGORY,
        REASON_NOT_IN_RULE,
    }
)


def get_plan_plain(db: Session, plan_version: str) -> dict[str, Any] | None:
    plan = get_plan(db, plan_version)
    if plan is None:
        return None
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


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
    return {
        "plan_version": plan.plan_version,
        "iana_timezone": plan.iana_timezone,
        "required_seconds": plan.required_seconds,
    }


def _require_plan(db: Session, plan_version: str):
    plan = get_plan(db, plan_version)
    if plan is None:
        raise PlanNotFoundError(f"plan version '{plan_version}' is not registered")
    return plan


def _to_core_rule(row) -> RuleConfig:
    return RuleConfig(
        rule_version=row.rule_version,
        categories=tuple(
            CategoryLimit(
                code=str(item["code"]),
                min_ratio=parse_ratio(item["min_ratio"]),
                max_ratio=parse_ratio(item["max_ratio"]),
            )
            for item in row.categories
        ),
        activity_category_map={
            str(k): str(v) for k, v in row.activity_category_map.items()
        },
    )


def _load_rule_context(
    db: Session, plan_version: str
) -> tuple[dict[str, RuleConfig], RuleConfig | None]:
    """加载方案的全部规则版本及当前已发布版本。"""
    rules: dict[str, RuleConfig] = {}
    current: RuleConfig | None = None
    for row in list_rule_configs(db, plan_version):
        config = _to_core_rule(row)
        rules[config.rule_version] = config
        if row.status == RULE_STATUS_PUBLISHED:
            current = config
    return rules, current


def _iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _rule_config_out(row, required_seconds: int) -> dict[str, Any]:
    config = _to_core_rule(row)
    return {
        "plan_version": row.plan_version,
        "rule_version": row.rule_version,
        "status": row.status,
        "categories": [
            {
                "code": c.code,
                "min_ratio": str(c.min_ratio),
                "max_ratio": str(c.max_ratio),
                "min_seconds": c.min_seconds(required_seconds),
                "max_seconds": c.max_seconds(required_seconds),
            }
            for c in config.categories
        ],
        "activity_category_map": dict(config.activity_category_map),
        "published_at": _iso(row.published_at),
        "created_at": _iso(row.created_at),
    }


def put_rule_config(
    db: Session,
    *,
    plan_version: str,
    rule_version: str,
    categories: list[CategoryLimit],
    activity_category_map: dict[str, str],
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    validate_categories(list(categories))
    stored_categories = [
        {
            "code": c.code,
            "min_ratio": str(c.min_ratio),
            "max_ratio": str(c.max_ratio),
        }
        for c in categories
    ]
    row = upsert_rule_config_draft(
        db,
        plan_version=plan_version,
        rule_version=rule_version,
        categories=stored_categories,
        activity_category_map=dict(activity_category_map),
    )
    if row is None:
        raise RuleConfigConflictError(
            f"rule '{rule_version}' for plan '{plan_version}' is already "
            "published or retired and cannot be modified"
        )
    return _rule_config_out(row, plan.required_seconds)


def publish_rule(
    db: Session, *, plan_version: str, rule_version: str
) -> dict[str, Any]:
    plan = _require_plan(db, plan_version)
    try:
        row = publish_rule_config(db, plan_version, rule_version)
    except IntegrityError as exc:
        db.rollback()
        raise RuleConfigConflictError(
            f"another rule for plan '{plan_version}' was published concurrently"
        ) from exc
    if row is None:
        raise RuleNotFoundError(
            f"rule '{rule_version}' for plan '{plan_version}' does not exist"
        )
    return _rule_config_out(row, plan.required_seconds)


def list_rule_config_views(db: Session, plan_version: str) -> list[dict[str, Any]]:
    plan = _require_plan(db, plan_version)
    return [
        _rule_config_out(row, plan.required_seconds)
        for row in list_rule_configs(db, plan_version)
    ]


def get_current_rule_config(
    db: Session, plan_version: str
) -> dict[str, Any] | None:
    plan = _require_plan(db, plan_version)
    for row in list_rule_configs(db, plan_version):
        if row.status == RULE_STATUS_PUBLISHED:
            return _rule_config_out(row, plan.required_seconds)
    return None


def import_events(
    db: Session, *, plan_version: str, events: list[dict[str, Any]]
) -> dict[str, Any]:
    _require_plan(db, plan_version)
    # 规则版本在导入时钉住：之后规则升级不影响已入库事件的解释口径。
    _, current_rule = _load_rule_context(db, plan_version)
    pinned = current_rule.rule_version if current_rule is not None else None
    accepted, duplicates = insert_events(
        db, plan_version=plan_version, events=events, rule_version=pinned
    )
    return {
        "accepted": len(accepted),
        "duplicates": duplicates,
        "rejected": [],
        "pinned_rule_version": pinned,
    }


def current_snapshot(db: Session, plan_version: str) -> Snapshot:
    plan = _require_plan(db, plan_version)
    events = load_events(db, plan_version)
    rules, current_rule = _load_rule_context(db, plan_version)
    return build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        rules=rules,
        current_rule=current_rule,
    )


def student_progress(
    db: Session, plan_version: str, student_id: str
) -> dict[str, Any] | None:
    snap = current_snapshot(db, plan_version)
    return explain_student(snap, student_id)


def batch_warnings(
    db: Session, plan_version: str, *, at_risk_only: bool = False
) -> dict[str, Any]:
    """汇总方案内所有学生的达标预警。"""
    snap = current_snapshot(db, plan_version)
    students: list[dict[str, Any]] = []
    students_at_risk = 0
    for student in snap.students:
        warnings: list[dict[str, Any]] = []
        critical = False
        if student["total_shortfall_seconds"] > 0:
            critical = True
            warnings.append(
                {
                    "code": "total_shortfall",
                    "seconds": student["total_shortfall_seconds"],
                    "category": None,
                    "message": "counted hours are below the plan requirement",
                }
            )
        for category in student["categories"]:
            if category["shortfall_seconds"] > 0:
                critical = True
                warnings.append(
                    {
                        "code": "category_shortfall",
                        "seconds": category["shortfall_seconds"],
                        "category": category["category"],
                        "message": "category is below its minimum ratio",
                    }
                )
            if category["over_cap_seconds"] > 0:
                warnings.append(
                    {
                        "code": "category_cap_exceeded",
                        "seconds": category["over_cap_seconds"],
                        "category": category["category"],
                        "message": "category hours beyond its maximum ratio do not count",
                    }
                )
        if student["pending_seconds"] > 0:
            warnings.append(
                {
                    "code": "pending_confirmation",
                    "seconds": student["pending_seconds"],
                    "category": None,
                    "message": "hours awaiting mentor confirmation",
                }
            )
        unattributed = sum(
            e["seconds"]
            for e in student["exclusions"]
            if e["reason"] in UNATTRIBUTED_REASONS
        )
        if unattributed > 0:
            warnings.append(
                {
                    "code": "unattributed_time",
                    "seconds": unattributed,
                    "category": None,
                    "message": "confirmed hours without a countable category",
                }
            )
        severity = "critical" if critical else ("warning" if warnings else "ok")
        if severity != "ok":
            students_at_risk += 1
        students.append(
            {
                "student_id": student["student_id"],
                "meets_requirement": student["meets_requirement"],
                "severity": severity,
                "warnings": warnings,
            }
        )
    if at_risk_only:
        students = [s for s in students if s["severity"] != "ok"]
    return {
        "plan_version": plan_version,
        "rule_version": snap.rule_version,
        "required_seconds": snap.required_seconds,
        "generated_at": snap.generated_at,
        "students_at_risk": students_at_risk,
        "students": students,
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
    rules, current_rule = _load_rule_context(db, plan_version)
    snap = build_snapshot(
        events,
        plan_version=plan_version,
        timezone_name=plan.iana_timezone,
        required_seconds=plan.required_seconds,
        rules=rules,
        current_rule=current_rule,
        freeze_id=freeze_id,
        event_cutoff_id=cutoff,
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
