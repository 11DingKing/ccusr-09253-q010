"""培养方案分项规则：活动类别的最低/最高占比约束。"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from math import ceil, floor
from typing import Mapping


class RuleConfigError(ValueError):
    """封装领域状态与业务约束。"""


def parse_ratio(value: object) -> Fraction:
    """把 API 传入的比例（小数或分数字符串）解析为精确的 Fraction。"""
    try:
        ratio = Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError) as exc:
        raise RuleConfigError(f"比例无法解析: {value!r}") from exc
    if ratio < 0 or ratio > 1:
        raise RuleConfigError("比例必须位于 [0, 1] 区间")
    return ratio


@dataclass(frozen=True)
class CategoryLimit:
    """单个活动类别的占比上下限。"""

    code: str
    min_ratio: Fraction
    max_ratio: Fraction

    def min_seconds(self, required_seconds: int) -> int:
        """最低占比换算为秒：向上取整，恰好整除时不多收一秒。"""
        return ceil(required_seconds * self.min_ratio)

    def max_seconds(self, required_seconds: int) -> int:
        """最高占比换算为秒：向下取整，恰好整除时不少算一秒。"""
        return floor(required_seconds * self.max_ratio)


@dataclass(frozen=True)
class RuleConfig:
    """一个已发布的规则版本。事件导入时按版本固定引用。"""

    rule_version: str
    categories: tuple[CategoryLimit, ...]
    activity_category_map: Mapping[str, str]

    def category_codes(self) -> tuple[str, ...]:
        return tuple(c.code for c in self.categories)

    def category_priority(self) -> dict[str, int]:
        """类别在配置中的声明顺序即重叠时段的分配优先级。"""
        return {c.code: rank for rank, c in enumerate(self.categories)}

    def limit_for(self, code: str) -> CategoryLimit | None:
        for category in self.categories:
            if category.code == code:
                return category
        return None


def validate_categories(categories: list[CategoryLimit] | tuple[CategoryLimit, ...]) -> None:
    """校验一组类别上下限是否自洽、可达标。"""
    if not categories:
        raise RuleConfigError("至少需要一个活动类别")
    codes = [c.code for c in categories]
    if any(not code.strip() for code in codes):
        raise RuleConfigError("类别编码不能为空")
    if len(set(codes)) != len(codes):
        raise RuleConfigError("类别编码不能重复")
    for category in categories:
        if category.min_ratio > category.max_ratio:
            raise RuleConfigError(
                f"类别 {category.code} 的最低占比不能高于最高占比"
            )
    if sum((c.min_ratio for c in categories), Fraction(0)) > 1:
        raise RuleConfigError("各类别最低占比之和不能超过 1")
    if sum((c.max_ratio for c in categories), Fraction(0)) < 1:
        raise RuleConfigError("各类别最高占比之和不能小于 1，否则总学时永远无法达标")
