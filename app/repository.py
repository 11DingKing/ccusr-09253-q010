"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .core.replay import Event as CoreEvent
from .core.replay import EventType
from .models import Event as EventModel
from .models import Freeze, Plan
from .models import RuleConfig as RuleConfigModel

RULE_STATUS_DRAFT = "draft"
RULE_STATUS_PUBLISHED = "published"
RULE_STATUS_RETIRED = "retired"


def get_plan(db: Session, plan_version: str) -> Plan | None:
    return db.get(Plan, plan_version)


def upsert_plan(
    db: Session,
    *,
    plan_version: str,
    iana_timezone: str,
    required_seconds: int,
) -> Plan:
    stmt = sqlite_insert(Plan).values(
        plan_version=plan_version,
        iana_timezone=iana_timezone,
        required_seconds=required_seconds,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["plan_version"],
        set_={
            "iana_timezone": iana_timezone,
            "required_seconds": required_seconds,
        },
    )
    db.execute(stmt)
    db.commit()
    plan = db.get(Plan, plan_version)
    assert plan is not None
    return plan


def _to_core_event(row: EventModel) -> CoreEvent:
    return CoreEvent(
        event_id=row.event_id,
        plan_version=row.plan_version,
        event_type=EventType(row.event_type),
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=row.created_at,
        rule_version=row.rule_version,
    )


def insert_events(
    db: Session,
    *,
    plan_version: str,
    events: list[dict[str, Any]],
    rule_version: str | None = None,
) -> tuple[list[str], list[str]]:
    """执行确定性的业务处理。"""
    accepted: list[str] = []
    duplicates: list[str] = []
    for e in events:
        stmt = sqlite_insert(EventModel).values(
            event_id=e["event_id"],
            plan_version=plan_version,
            student_id=e["student_id"],
            event_type=e["event_type"],
            payload=e["payload"],
            rule_version=rule_version,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["event_id", "plan_version"]
        ).returning(EventModel.id)
        inserted_id = db.execute(stmt).scalar_one_or_none()
        if inserted_id is not None:
            accepted.append(e["event_id"])
        else:
            duplicates.append(e["event_id"])
    db.commit()
    return accepted, duplicates


def load_events(db: Session, plan_version: str) -> list[CoreEvent]:
    stmt = select(EventModel).where(EventModel.plan_version == plan_version)
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def load_events_up_to(
    db: Session, plan_version: str, max_event_id: str
) -> list[CoreEvent]:
    """执行确定性的业务处理。"""
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id <= max_event_id)
    )
    rows = db.execute(stmt).scalars().all()
    return [_to_core_event(r) for r in rows]


def max_event_id(db: Session, plan_version: str) -> str | None:
    stmt = (
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id.desc())
        .limit(1)
    )
    return db.execute(stmt).scalar_one_or_none()


def get_freeze(
    db: Session, plan_version: str, freeze_id: str
) -> Freeze | None:
    return db.get(Freeze, (plan_version, freeze_id))


def insert_freeze(
    db: Session,
    *,
    plan_version: str,
    freeze_id: str,
    snapshot: dict[str, Any],
    event_cutoff_id: str | None,
) -> Freeze | None:
    """执行确定性的业务处理。"""
    stmt = sqlite_insert(Freeze).values(
        plan_version=plan_version,
        freeze_id=freeze_id,
        snapshot=snapshot,
        event_cutoff_id=event_cutoff_id,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "freeze_id"]
    ).returning(Freeze.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    db.commit()
    if inserted is not None:
        return db.get(Freeze, (plan_version, freeze_id))
    return None


def get_rule_config(
    db: Session, plan_version: str, rule_version: str
) -> RuleConfigModel | None:
    return db.get(RuleConfigModel, (plan_version, rule_version))


def list_rule_configs(db: Session, plan_version: str) -> list[RuleConfigModel]:
    stmt = (
        select(RuleConfigModel)
        .where(RuleConfigModel.plan_version == plan_version)
        .order_by(RuleConfigModel.created_at, RuleConfigModel.rule_version)
    )
    return list(db.execute(stmt).scalars().all())


def upsert_rule_config_draft(
    db: Session,
    *,
    plan_version: str,
    rule_version: str,
    categories: list[dict[str, Any]],
    activity_category_map: dict[str, str],
) -> RuleConfigModel | None:
    """写入草稿规则；已发布或已退休的同名规则不可修改，返回 None。"""
    stmt = sqlite_insert(RuleConfigModel).values(
        plan_version=plan_version,
        rule_version=rule_version,
        status=RULE_STATUS_DRAFT,
        categories=categories,
        activity_category_map=activity_category_map,
    )
    stmt = stmt.on_conflict_do_nothing(
        index_elements=["plan_version", "rule_version"]
    ).returning(RuleConfigModel.plan_version)
    inserted = db.execute(stmt).scalar_one_or_none()
    if inserted is not None:
        db.commit()
        return db.get(RuleConfigModel, (plan_version, rule_version))
    existing = db.get(RuleConfigModel, (plan_version, rule_version))
    if existing is None or existing.status != RULE_STATUS_DRAFT:
        db.commit()
        return None
    existing.categories = categories
    existing.activity_category_map = activity_category_map
    db.commit()
    return existing


def publish_rule_config(
    db: Session, plan_version: str, rule_version: str
) -> RuleConfigModel | None:
    """发布指定规则版本，同事务内退休方案此前的已发布版本。"""
    row = db.get(RuleConfigModel, (plan_version, rule_version))
    if row is None:
        return None
    if row.status == RULE_STATUS_PUBLISHED:
        return row
    now = datetime.now(timezone.utc)
    retire_stmt = (
        update(RuleConfigModel)
        .where(RuleConfigModel.plan_version == plan_version)
        .where(RuleConfigModel.status == RULE_STATUS_PUBLISHED)
        .where(RuleConfigModel.rule_version != rule_version)
        .values(status=RULE_STATUS_RETIRED)
    )
    db.execute(retire_stmt)
    row.status = RULE_STATUS_PUBLISHED
    row.published_at = now
    db.commit()
    return row
