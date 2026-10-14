"""选品规则引擎。

公开入口：
    from src.selection import run_selection, build_default_rules, SelectionEngine

设计：
    - Rule（硬过滤）+ ScorerRule（软评分）两套接口并存
    - run_selection() 一站式完成：拉数据 → 跑规则 → upsert ChannelListing 草稿
"""
from src.selection.engine import (
    BannedWordsRule,
    Candidate,
    CategoryBlacklistRule,
    MarginScorerRule,
    MinMarginRule,
    NotOversoldRule,
    PlaceholderMLScorerRule,
    PriceRangeRule,
    ReturnRateRule,
    ReturnRateScorerRule,
    Rule,
    ScorerRule,
    SelectionContext,
    SelectionEngine,
    SelectionResult,
    SelectionSettings,
    StockScorerRule,
    StockThresholdRule,
    build_default_rules,
    build_default_scorers,
    build_default_settings,
    run_selection,
)

__all__ = [
    # 入口
    "run_selection",
    "SelectionEngine",
    "SelectionResult",
    # 配置
    "SelectionSettings",
    "SelectionContext",
    # 候选
    "Candidate",
    # 硬规则
    "Rule",
    "PriceRangeRule",
    "MinMarginRule",
    "StockThresholdRule",
    "ReturnRateRule",
    "CategoryBlacklistRule",
    "BannedWordsRule",
    "NotOversoldRule",
    # 软评分
    "ScorerRule",
    "MarginScorerRule",
    "StockScorerRule",
    "ReturnRateScorerRule",
    "PlaceholderMLScorerRule",
    # 工厂
    "build_default_settings",
    "build_default_rules",
    "build_default_scorers",
]