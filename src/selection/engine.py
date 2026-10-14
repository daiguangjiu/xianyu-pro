"""选品规则引擎。

设计：
- `Rule`（硬过滤） + `ScorerRule`（软评分） 两套接口并存
    - 硬规则：不通过即 reject，写到 `reject_reasons`，不抛
    - 软规则：返回 [0,1] 之间的分，按 `weight` 加权累加进 `candidate.score`，仅用于排序/筛 topK
- 软规则接口预留：实现 `ScorerRule` 即可插入 ML 模型，不需改 Runner

第 1 轮: 主链路验证（硬规则 + 重叠 upsert ChannelListing）
第 2 轮: 接入 Celery
第 3 轮: 接入权重模型（MLScorerRule）
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from src.models import ChannelListing, PriceRule, SupplierItem
from src.pricing.engine import calculate_price, pick_rule

logger = logging.getLogger(__name__)


# =====================================================================
# 配置（可被 PriceRule / 环境变量覆盖）
# =====================================================================
@dataclass
class SelectionSettings:
    """选品可配置阈值。

    默认值与 docs/selection_rules.md 准入规则对齐：
        价格区间    5 ~ 1000 元
        最低毛利率  ≥ 20%
        库存安全量  ≥ 5
        退货率      ≤ 10%
        违禁词过滤
        类目黑名单
    """

    price_min: Decimal = Decimal("5")
    price_max: Decimal = Decimal("1000")
    min_margin: float = 0.20
    min_stock: int = 5
    max_return_rate: float = 0.10
    banned_words: list[str] = field(default_factory=list)
    blacklist_categories: list[str] = field(default_factory=list)
    # 超卖联动：同步环节发现超卖的商品不进草稿（与 goods/sync.py 联动）
    reject_if_oversold: bool = True
    # 软评分权重（影响 topK 排序）
    margin_scorer_weight: float = 1.0
    stock_scorer_weight: float = 0.5
    return_rate_scorer_weight: float = 0.5


@dataclass
class SelectionContext:
    """一次 run_selection 的上下文。"""

    db: Session
    channel: str = "xianyu"
    shadow_mode: bool = False
    settings: SelectionSettings = field(default_factory=SelectionSettings)


# =====================================================================
# 候选 + 结果
# =====================================================================
@dataclass
class Candidate:
    """待选品的商品（包装 SupplierItem + 拒绝原因 + 评分）。

    `score` 越高表示“越值得选”。硬规则失败的 candidate 仍会进入 run()
    输出，但会带 reject_reasons——便于复盘选品漏斗。
    """

    item: SupplierItem
    reject_reasons: list[str] = field(default_factory=list)
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    suggested_price: Decimal | None = None
    price_rule_id: int | None = None

    def reset(self) -> None:
        """重置可重入状态。"""
        self.reject_reasons.clear()
        self.score = 0.0
        self.score_breakdown.clear()
        self.suggested_price = None
        self.price_rule_id = None


@dataclass
class SelectionResult:
    """一次 run_selection 的汇总。"""

    total: int = 0
    passed: int = 0
    rejected: int = 0
    rejected_reasons: dict[str, int] = field(default_factory=dict)
    listings_written: int = 0
    passed_item_ids: list[str] = field(default_factory=list)
    rejected_item_ids: list[str] = field(default_factory=list)
    shadow_mode: bool = False

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "passed": self.passed,
            "rejected": self.rejected,
            "rejected_reasons": dict(self.rejected_reasons),
            "listings_written": self.listings_written,
            "shadow_mode": self.shadow_mode,
        }


# =====================================================================
# 硬过滤规则（Rule）
# =====================================================================
class Rule(ABC):
    """硬过滤规则：不通过即 reject。

    约定：规则负责写到 `candidate.reject_reasons`，不能抛异常。
    """

    name: str = "Rule"
    priority: int = 100  # 越小越先执行

    @abstractmethod
    def check(self, c: Candidate, ctx: SelectionContext) -> bool: ...


class PriceRangeRule(Rule):
    """价格区间过滤。"""

    name = "price_range"

    def __init__(self, price_min: Decimal, price_max: Decimal):
        self.price_min = price_min
        self.price_max = price_max

    def check(self, c: Candidate, ctx: SelectionContext) -> bool:
        if not (self.price_min <= c.item.cost_price <= self.price_max):
            c.reject_reasons.append(
                f"price_range:{c.item.cost_price} not in [{self.price_min},{self.price_max}]"
            )
            return False
        return True


class MinMarginRule(Rule):
    """最低毛利率。

    `margin_rate` 从 ctx.settings 读取（默认 0.20），调用方可在 run_selection 前覆盖。
    """

    name = "min_margin"

    def check(self, c: Candidate, ctx: SelectionContext) -> bool:
        cost = float(c.item.cost_price)
        if cost <= 0:
            c.reject_reasons.append("min_margin:cost_price<=0")
            return False
        # 使用 ctx 的 margin_rate（可能是 PriceRule 覆盖过的）
        margin = (float(c.suggested_price or Decimal("0")) - cost) / cost
        if margin < ctx.settings.min_margin:
            c.reject_reasons.append(
                f"min_margin:{margin:.2%}<{ctx.settings.min_margin:.2%}"
            )
            return False
        return True


class StockThresholdRule(Rule):
    """库存安全量。"""

    name = "stock_threshold"

    def check(self, c: Candidate, ctx: SelectionContext) -> bool:
        if c.item.stock < ctx.settings.min_stock:
            c.reject_reasons.append(
                f"stock_threshold:{c.item.stock}<{ctx.settings.min_stock}"
            )
            return False
        return True


class ReturnRateRule(Rule):
    """退货率上限（近 30 天）。"""

    name = "return_rate"

    def check(self, c: Candidate, ctx: SelectionContext) -> bool:
        if float(c.item.return_rate) > ctx.settings.max_return_rate:
            c.reject_reasons.append(
                f"return_rate:{float(c.item.return_rate):.2%}>{ctx.settings.max_return_rate:.2%}"
            )
            return False
        return True


class CategoryBlacklistRule(Rule):
    """类目黑名单。

    从 ctx.settings.blacklist_categories 读取（如 ['CAT_VIRTUAL', 'CAT_WEAPON']）。
    """

    name = "category_blacklist"

    def check(self, c: Candidate, ctx: SelectionContext) -> bool:
        if not ctx.settings.blacklist_categories:
            return True
        if (c.item.category_id or "") in ctx.settings.blacklist_categories:
            c.reject_reasons.append(
                f"category_blacklist:{c.item.category_id}"
            )
            return False
        return True


class BannedWordsRule(Rule):
    """违禁词过滤。"""

    name = "banned_words"

    def __init__(self, banned_words: Iterable[str] | None = None):
        self.banned_words = [w.lower() for w in (banned_words or [])]

    def check(self, c: Candidate, ctx: SelectionContext) -> bool:
        words = self.banned_words or ctx.settings.banned_words
        if not words:
            return True
        title = (c.item.title or "").lower()
        hit = [w for w in words if w in title]
        if hit:
            c.reject_reasons.append(f"banned_words:{hit}")
            return False
        return True


class NotOversoldRule(Rule):
    """超卖联动（与 goods/sync.py 联动）。

    当 SupplierItem.is_oversold=True 时（如 SKU 全无库存、total 库存为 0），
    拒绝进入草稿——避免推单后造超卖。
    """

    name = "not_oversold"

    def check(self, c: Candidate, ctx: SelectionContext) -> bool:
        if not ctx.settings.reject_if_oversold:
            return True
        if c.item.is_oversold:
            c.reject_reasons.append("not_oversold:true")
            return False
        return True


# =====================================================================
# 软评分规则（ScorerRule）——未来权重模型接入口
# =====================================================================
class ScorerRule(ABC):
    """软评分规则：返回 [0, 1]，按 weight 加权累加到 candidate.score。

    目的：
    - 硬规则只保证“能不能选”，软规则决定“选哪个”
    - 后续接 ML 模型只需实现 ScorerRule，不必改 Engine

    评分仍会作用于 passed 后的排序/取 topK；不通过的硬规则 candidate 也会被记
    入 score_breakdown，便于复盘为什么不选它（同时累计 reject_reasons）。
    """

    name: str = "ScorerRule"
    weight: float = 1.0

    @abstractmethod
    def score(self, c: Candidate, ctx: SelectionContext) -> float: ...


class MarginScorerRule(ScorerRule):
    """毛利越高分越高（线性归一到 [0, 1]）。"""

    name = "margin_scoring"
    weight: float = 1.0

    def __init__(self, weight: float = 1.0):
        self.weight = weight

    def score(self, c: Candidate, ctx: SelectionContext) -> float:
        cost = float(c.item.cost_price)
        suggested = float(c.suggested_price or Decimal("0"))
        if cost <= 0 or suggested <= 0:
            return 0.0
        margin = (suggested - cost) / cost
        # 0% → 0; 100% → 1; >100% → 1
        return max(0.0, min(1.0, margin))


class StockScorerRule(ScorerRule):
    """库存越多分越高（饱和曲线）。"""

    name = "stock_scoring"

    def __init__(self, weight: float = 0.5, cap: int = 500):
        self.weight = weight
        self.cap = cap

    def score(self, c: Candidate, ctx: SelectionContext) -> float:
        # 库存 / cap 封顶到 1
        return max(0.0, min(1.0, c.item.stock / float(self.cap)))


class ReturnRateScorerRule(ScorerRule):
    """退货率越低分越高。"""

    name = "return_rate_scoring"

    def __init__(self, weight: float = 0.5, max_considered: float = 0.10):
        self.weight = weight
        self.max_considered = max_considered

    def score(self, c: Candidate, ctx: SelectionContext) -> float:
        # 退货率 0 → 1; 退货率 >=max_considered → 0
        rate = float(c.item.return_rate)
        return max(0.0, 1.0 - rate / self.max_considered)


class PlaceholderMLScorerRule(ScorerRule):
    """TODO：后续接权重模型（GBDT / MLP / LLM-as-judge）。

    约定：实现本接口后只需在 build_default_scorers() 里 append 进去。
    """

    name = "placeholder_ml_scoring"
    weight: float = 0.0  # 默认为 0，不会影响总分；接入时手工调高

    def score(self, c: Candidate, ctx: SelectionContext) -> float:
        # TODO: 接模型后返回 model.predict(...) ∈ [0, 1]
        return 0.0


# =====================================================================
# 引擎
# =====================================================================
class SelectionEngine:
    """规则链 + 评分链 执行器。

    硬规则：任意一个不通过 → reject
    软规则：全部计算后加到 score，不决定 rejected/passed，但决定 topK 排序
    """

    def __init__(
        self,
        hard_rules: list[Rule] | None = None,
        scorer_rules: list[ScorerRule] | None = None,
    ):
        self.hard_rules = sorted(hard_rules or [], key=lambda r: r.priority)
        self.scorer_rules = list(scorer_rules or [])

    def run(
        self,
        candidates: Iterable[Candidate],
        ctx: SelectionContext,
    ) -> SelectionResult:
        result = SelectionResult(shadow_mode=ctx.shadow_mode)
        for c in candidates:
            c.reset()
            result.total += 1

            # 1. 算价（PriceRule → calculate_price）
            rule = pick_rule(
                ctx.db,
                channel=ctx.channel,
                category_id=c.item.category_id,
                supplier_id=c.item.supplier_id,
            )
            # PriceRule 命中：用 rule 的 margin；未命中：用 settings.min_margin 兜底
            if rule is not None:
                c.suggested_price = calculate_price(c.item, rule=rule)
            else:
                c.suggested_price = calculate_price(
                    c.item, margin_rate=ctx.settings.min_margin
                )
            c.price_rule_id = rule.id if rule else None

            # 2. 硬规则链
            for r in self.hard_rules:
                if not r.check(c, ctx):
                    # 累计 reject_reasons 计数
                    for reason in c.reject_reasons:
                        key = reason.split(":", 1)[0]
                        result.rejected_reasons[key] = (
                            result.rejected_reasons.get(key, 0) + 1
                        )
                    break  # 一个不通就跳出，其余硬规则不再跑

            # 3. 软评分（无论是否 reject 都跑，便于复盘）
            score_total = 0.0
            for s in self.scorer_rules:
                raw = s.score(c, ctx)
                weighted = raw * s.weight
                c.score_breakdown[s.name] = round(raw, 4)
                score_total += weighted
            c.score = round(score_total, 4)

            if c.reject_reasons:
                result.rejected += 1
                result.rejected_item_ids.append(c.item.item_id)
            else:
                result.passed += 1
                result.passed_item_ids.append(c.item.item_id)

        return result


# =====================================================================
# 编排：拉 SupplierItem → 跑引擎 → upsert ChannelListing 草稿
# =====================================================================
def _load_supplier_items(
    db: Session,
    item_ids: list[str] | None,
) -> list[SupplierItem]:
    """拉 SupplierItem 列表。

    item_ids=None 时只取"可被选"的（不是 oversold 的、不在 is_oversold=True 范围）；
    这里不预过滤 oversold，让 NotOversoldRule 统一拒绝（便于复盘 reject_reasons）。
    """
    q = db.query(SupplierItem)
    if item_ids:
        q = q.filter(SupplierItem.item_id.in_(item_ids))
    return q.all()


def _upsert_listing(
    db: Session,
    *,
    candidate: Candidate,
    channel: str,
    status: str,
    shadow_mode: bool,
) -> ChannelListing | None:
    """upsert ChannelListing（draft=pass / failed=reject）。

    不替换已发布商品（status='published'）的状态，避免下次跑选品把上架了的商品拉回草稿。
    """
    if shadow_mode:
        return None  # 影子模式不写库

    # 选品是平台批量跑的，不挂在某个账号名下 —— owner 留空（平台级记录），
    # 不参与任何账号的货源广场卡片状态（2026-10-13 起铺货状态按账号隔离）
    existing = (
        db.query(ChannelListing)
        .filter(
            ChannelListing.item_id == candidate.item.item_id,
            ChannelListing.channel == channel,
            ChannelListing.owner == "",
        )
        .first()
    )

    if existing is None:
        listing = ChannelListing(
            item_id=candidate.item.item_id,
            channel=channel,
            owner="",
            status=status,
            suggested_price=candidate.suggested_price,
            suggested_title=candidate.item.title,
            reject_reasons=list(candidate.reject_reasons),
        )
        db.add(listing)
        return listing

    # 已发布的不动
    if existing.status == "published":
        return existing

    existing.status = status
    existing.suggested_price = candidate.suggested_price
    existing.suggested_title = candidate.item.title
    existing.reject_reasons = list(candidate.reject_reasons)
    return existing


def run_selection(
    db: Session,
    *,
    item_ids: list[str] | None = None,
    channel: str = "xianyu",
    shadow_mode: bool = False,
    settings: SelectionSettings | None = None,
    engine: SelectionEngine | None = None,
) -> SelectionResult:
    """一次选品的主入口。

    入参:
            db           SQLAlchemy Session
            item_ids     限定 item_id 列表；None = 拉全量
            channel      下游渠道
            shadow_mode  True = 仅算不写库（用来 A/B / 验证策略）
            settings     可覆盖默认阈值；None = 用 build_default_settings()
            engine       可注入自定义规则链；None = 默认链

    返回:
            SelectionResult 含 passed/rejected 与 reject_reasons 漏斗

    写入行为：
            passed → ChannelListing(status="draft")
            rejected → ChannelListing(status="failed")
            shadow_mode=True → 不写库，仅返回 result
            existing published  → 不动
    """
    settings = settings or build_default_settings()
    ctx = SelectionContext(db=db, channel=channel, shadow_mode=shadow_mode, settings=settings)

    if engine is None:
        engine = SelectionEngine(
            hard_rules=build_default_rules(),
            scorer_rules=build_default_scorers(),
        )

    items = _load_supplier_items(db, item_ids)
    candidates = [Candidate(item=it) for it in items]
    result = engine.run(candidates, ctx)

    # 写 ChannelListing（shadow_mode 不写）
    if not shadow_mode:
        for c in candidates:
            status = "draft" if not c.reject_reasons else "failed"
            _upsert_listing(
                db,
                candidate=c,
                channel=channel,
                status=status,
                shadow_mode=shadow_mode,
            )
            result.listings_written += 1
        db.commit()

    logger.info(
        "run_selection channel=%s shadow=%s total=%s passed=%s rejected=%s written=%s",
        channel,
        shadow_mode,
        result.total,
        result.passed,
        result.rejected,
        result.listings_written,
    )
    return result


# =====================================================================
# 默认工厂
# =====================================================================
def build_default_settings(**overrides) -> SelectionSettings:
    """构造默认 SelectionSettings，可被参数覆盖（来自 env / 运营配置）。"""
    base = SelectionSettings()
    base.__dict__.update(overrides)
    return base


def build_default_rules(settings: SelectionSettings | None = None) -> list[Rule]:
    """构造默认硬规则链。"""
    settings = settings or build_default_settings()
    return [
        NotOversoldRule(),
        PriceRangeRule(settings.price_min, settings.price_max),
        StockThresholdRule(),
        ReturnRateRule(),
        CategoryBlacklistRule(),
        MinMarginRule(),
        BannedWordsRule(settings.banned_words),
    ]


def build_default_scorers(settings: SelectionSettings | None = None) -> list[ScorerRule]:
    """构造默认软评分链。

    PlaceholderMLScorerRule() weight=0 不会影响总分；接入 ML 后手工调高 weight。
    """
    settings = settings or build_default_settings()
    return [
        MarginScorerRule(weight=settings.margin_scorer_weight),
        StockScorerRule(weight=settings.stock_scorer_weight),
        ReturnRateScorerRule(weight=settings.return_rate_scorer_weight),
        PlaceholderMLScorerRule(),
    ]


__all__ = [
    # 配置
    "SelectionSettings",
    "SelectionContext",
    # 候选 + 结果
    "Candidate",
    "SelectionResult",
    # 抽象 + 引擎
    "Rule",
    "ScorerRule",
    "SelectionEngine",
    # 硬规则
    "PriceRangeRule",
    "MinMarginRule",
    "StockThresholdRule",
    "ReturnRateRule",
    "CategoryBlacklistRule",
    "BannedWordsRule",
    "NotOversoldRule",
    # 软评分
    "MarginScorerRule",
    "StockScorerRule",
    "ReturnRateScorerRule",
    "PlaceholderMLScorerRule",
    # 工厂 + 入口
    "build_default_settings",
    "build_default_rules",
    "build_default_scorers",
    "run_selection",
]