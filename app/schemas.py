"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int
    active_rule_version: str | None = None


class CategoryRuleIn(BaseModel):
    key: str = Field(..., min_length=1, max_length=64)
    min_permille: int = Field(0, ge=0, le=1000)
    max_permille: int = Field(1000, ge=0, le=1000)

    @model_validator(mode="after")
    def _check_order(self) -> "CategoryRuleIn":
        if self.min_permille > self.max_permille:
            raise ValueError("min_permille must not exceed max_permille")
        return self


class AuthorizationWindowIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=64)
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "AuthorizationWindowIn":
        if self.end <= self.start:
            raise ValueError("window end must be after start")
        return self

    @field_validator("start", "end")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class RuleDefinitionIn(BaseModel):
    categories: list[CategoryRuleIn] = Field(default_factory=list)
    authorization_windows: list[AuthorizationWindowIn] = Field(default_factory=list)
    activity_category_map: dict[str, str] = Field(default_factory=dict)
    default_category: str | None = None

    @model_validator(mode="after")
    def _check_references(self) -> "RuleDefinitionIn":
        keys = [c.key for c in self.categories]
        if len(set(keys)) != len(keys):
            raise ValueError("category keys must be unique")
        known = set(keys)
        for window in self.authorization_windows:
            if window.category not in known:
                raise ValueError(
                    "authorization window references unknown category "
                    f"'{window.category}'"
                )
        for activity_type, category in self.activity_category_map.items():
            if category not in known:
                raise ValueError(
                    "activity_category_map references unknown category "
                    f"'{category}'"
                )
        if self.default_category is not None and self.default_category not in known:
            raise ValueError(
                "default_category references unknown category "
                f"'{self.default_category}'"
            )
        return self


class RuleIn(BaseModel):
    definition: RuleDefinitionIn
    activate: bool = False


class RuleOut(BaseModel):
    plan_version: str
    rule_version: str
    definition: dict[str, Any]
    is_active: bool
    created_at: str


class RuleListOut(BaseModel):
    plan_version: str
    active_rule_version: str | None
    rules: list[RuleOut]


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""
    category: str | None = None


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int
    categories: dict[str, int] = Field(default_factory=dict)


class NonCountableOut(BaseModel):
    reason: str
    seconds: int
    category: str | None = None


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]
    rule_version: str | None = None
    category: str | None = None
    countable_seconds: int = 0
    allocated_seconds: int = 0
    non_countable: list[NonCountableOut] = Field(default_factory=list)
    segments: list[dict[str, Any]] = Field(default_factory=list)


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str
    category: str | None = None


class CategoryBreakdownOut(BaseModel):
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


class StudentProgressOut(BaseModel):
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
    categories: list[CategoryBreakdownOut] = Field(default_factory=list)
    non_countable: list[NonCountableOut] = Field(default_factory=list)
    event_rule_versions: list[str] = Field(default_factory=list)
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    rule_version: str | None = None
    rule_versions: list[str] = Field(default_factory=list)
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class CategoryGapOut(BaseModel):
    category: str
    min_seconds: int
    credited_seconds: int
    gap_seconds: int


class WarningOut(BaseModel):
    student_id: str
    meets_requirement: bool
    total_seconds: int
    required_seconds: int
    total_gap_seconds: int
    pending_seconds: int
    category_gaps: list[CategoryGapOut]
    non_countable: list[NonCountableOut]


class WarningsOut(BaseModel):
    plan_version: str
    rule_version: str | None
    generated_at: str
    students_at_risk: int
    warnings: list[WarningOut]


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int
