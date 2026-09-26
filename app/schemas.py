"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .core.rules import CategoryLimit, parse_ratio, validate_categories


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CategoryLimitIn(BaseModel):
    code: str = Field(..., min_length=1, max_length=64)
    min_ratio: str = Field(...)
    max_ratio: str = Field(...)

    @field_validator("min_ratio", "max_ratio", mode="before")
    @classmethod
    def _parse_ratio(cls, v: Any) -> str:
        # 接受小数或 "1/3" 形式，统一保存为精确的 Fraction 字符串。
        return str(parse_ratio(v))


class RuleConfigIn(BaseModel):
    categories: list[CategoryLimitIn] = Field(..., min_length=1)
    activity_category_map: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_categories(self) -> "RuleConfigIn":
        codes = [c.code for c in self.categories]
        if len(set(codes)) != len(codes):
            raise ValueError("category codes must be unique")
        unknown = sorted(set(self.activity_category_map.values()) - set(codes))
        if unknown:
            raise ValueError(
                f"activity_category_map references unknown categories: {unknown}"
            )
        validate_categories(
            [
                CategoryLimit(
                    code=c.code,
                    min_ratio=Fraction(c.min_ratio),
                    max_ratio=Fraction(c.max_ratio),
                )
                for c in self.categories
            ]
        )
        return self


class CategoryLimitOut(BaseModel):
    code: str
    min_ratio: str
    max_ratio: str
    min_seconds: int
    max_seconds: int


class RuleConfigOut(BaseModel):
    plan_version: str
    rule_version: str
    status: str
    categories: list[CategoryLimitOut]
    activity_category_map: dict[str, str]
    published_at: str | None
    created_at: str | None


class CategoryWindowIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=64)
    start: datetime
    end: datetime


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime
    # 可选：把签到区间切分为若干类别授权区间。
    category_windows: list[CategoryWindowIn] | None = None

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
    # 可选：把修正定向到某个活动类别。
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
    pinned_rule_version: str | None = None


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class DailyCategoryTotal(BaseModel):
    academic_day: str
    category: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    rule_version: str | None = None
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]
    category_segments: list[dict[str, Any]] = []
    excluded_segments: list[dict[str, Any]] = []


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str
    category: str | None = None


class CategoryProgressOut(BaseModel):
    category: str
    attributed_seconds: int
    adjustment_seconds: int
    counted_seconds: int
    min_seconds: int
    max_seconds: int
    shortfall_seconds: int
    over_cap_seconds: int
    meets_minimum: bool


class ExclusionOut(BaseModel):
    reason: str
    seconds: int
    event_id: str | None = None
    category: str | None = None


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    counted_seconds: int
    total_shortfall_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    daily_categories: list[DailyCategoryTotal]
    categories: list[CategoryProgressOut]
    exclusions: list[ExclusionOut]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    rule_version: str | None = None
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    old_rule_version: str | None = None
    new_rule_version: str | None = None
    student_changes: list[dict[str, Any]]
    students_affected: int


class WarningItem(BaseModel):
    code: str
    seconds: int
    category: str | None = None
    message: str


class StudentWarningsOut(BaseModel):
    student_id: str
    meets_requirement: bool
    severity: str
    warnings: list[WarningItem]


class WarningsOut(BaseModel):
    plan_version: str
    rule_version: str | None
    required_seconds: int
    generated_at: str
    students_at_risk: int
    students: list[StudentWarningsOut]
