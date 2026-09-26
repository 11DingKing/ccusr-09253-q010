"""服务端业务模块。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from .clock import merge_intervals

DEFAULT_RULE_VERSION = "rv-default"

MAX_PERMILLE = 1000


class RuleValidationError(ValueError):
    """封装领域状态与业务约束。"""


@dataclass(frozen=True)
class CategoryRule:
    """单个活动类别的占比约束（千分比，换算为秒时向下取整）。"""

    key: str
    min_permille: int = 0
    max_permille: int = MAX_PERMILLE

    def min_seconds(self, required_seconds: int) -> int:
        return required_seconds * self.min_permille // MAX_PERMILLE

    def max_seconds(self, required_seconds: int) -> int:
        return required_seconds * self.max_permille // MAX_PERMILLE


@dataclass(frozen=True)
class AuthorizationWindow:
    """类别授权区间：只有落在区间内的签到时长才计入该类别。"""

    category: str
    start_utc: datetime
    end_utc: datetime


@dataclass(frozen=True)
class RuleDef:
    """不可变的规则版本定义。"""

    rule_version: str
    categories: tuple[CategoryRule, ...] = ()
    windows: tuple[AuthorizationWindow, ...] = ()
    activity_category_map: Mapping[str, str] = field(default_factory=dict)
    default_category: str | None = None

    @classmethod
    def from_dict(cls, rule_version: str, data: Mapping[str, Any]) -> "RuleDef":
        """解析并校验规则定义，非法定义抛出 RuleValidationError。"""
        if not isinstance(data, Mapping):
            raise RuleValidationError("rule definition must be an object")

        categories: list[CategoryRule] = []
        seen: set[str] = set()
        for item in data.get("categories") or []:
            key = str(item.get("key", "")).strip()
            if not key:
                raise RuleValidationError("category key must not be empty")
            if key in seen:
                raise RuleValidationError(f"duplicate category key '{key}'")
            seen.add(key)
            try:
                min_p = int(item.get("min_permille", 0))
                max_p = int(item.get("max_permille", MAX_PERMILLE))
            except (TypeError, ValueError):
                raise RuleValidationError(
                    "permille values must be integers"
                ) from None
            if not (0 <= min_p <= MAX_PERMILLE) or not (0 <= max_p <= MAX_PERMILLE):
                raise RuleValidationError("permille values must be within 0..1000")
            if min_p > max_p:
                raise RuleValidationError(
                    f"category '{key}': min_permille exceeds max_permille"
                )
            categories.append(
                CategoryRule(key=key, min_permille=min_p, max_permille=max_p)
            )

        known = set(seen)
        windows: list[AuthorizationWindow] = []
        for item in data.get("authorization_windows") or []:
            category = str(item.get("category", "")).strip()
            if category not in known:
                raise RuleValidationError(
                    f"authorization window references unknown category '{category}'"
                )
            start = _parse_instant(item.get("start"))
            end = _parse_instant(item.get("end"))
            if end <= start:
                raise RuleValidationError(
                    "authorization window end must be after start"
                )
            windows.append(
                AuthorizationWindow(category=category, start_utc=start, end_utc=end)
            )

        mapping: dict[str, str] = {}
        for activity_type, category in (
            data.get("activity_category_map") or {}
        ).items():
            target = str(category).strip()
            if target not in known:
                raise RuleValidationError(
                    "activity_category_map references unknown category "
                    f"'{target}'"
                )
            mapping[str(activity_type)] = target

        default_category = data.get("default_category")
        if default_category is not None:
            default_category = str(default_category).strip() or None
            if default_category is not None and default_category not in known:
                raise RuleValidationError(
                    f"default_category references unknown category "
                    f"'{default_category}'"
                )

        return cls(
            rule_version=rule_version,
            categories=tuple(categories),
            windows=tuple(windows),
            activity_category_map=mapping,
            default_category=default_category,
        )

    def to_dict(self) -> dict[str, Any]:
        """序列化为可比较的规范化结构。"""
        return {
            "categories": [
                {
                    "key": c.key,
                    "min_permille": c.min_permille,
                    "max_permille": c.max_permille,
                }
                for c in self.categories
            ],
            "authorization_windows": [
                {
                    "category": w.category,
                    "start": w.start_utc.isoformat().replace("+00:00", "Z"),
                    "end": w.end_utc.isoformat().replace("+00:00", "Z"),
                }
                for w in self.windows
            ],
            "activity_category_map": dict(self.activity_category_map),
            "default_category": self.default_category,
        }

    def category_for(self, activity_type: str) -> str | None:
        """按活动类型映射类别，未命中时使用默认类别。"""
        if activity_type in self.activity_category_map:
            return self.activity_category_map[activity_type]
        return self.default_category

    def category_rule(self, key: str) -> CategoryRule | None:
        for category in self.categories:
            if category.key == key:
                return category
        return None

    def windows_for(self, category: str) -> list[tuple[datetime, datetime]]:
        """该类别的授权区间并集（已合并重叠）。"""
        raw = [
            (w.start_utc, w.end_utc) for w in self.windows if w.category == category
        ]
        return merge_intervals(raw)


def _parse_instant(value: Any) -> datetime:
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str):
        try:
            instant = datetime.fromisoformat(value)
        except ValueError:
            raise RuleValidationError(
                f"invalid window timestamp '{value}'"
            ) from None
    else:
        raise RuleValidationError("window timestamps must be ISO 8601 strings")
    if instant.tzinfo is None:
        raise RuleValidationError("window timestamps must be timezone-aware")
    return instant.astimezone(timezone.utc)


def default_rule() -> RuleDef:
    """隐式旧版规则：无类别、无授权区间，保持既有重放语义。"""
    return RuleDef(rule_version=DEFAULT_RULE_VERSION)
