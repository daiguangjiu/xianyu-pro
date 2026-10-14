"""定价模块：按 PriceRule 计算下游售价。

公式：下游售价 = round(1688成本价 × (1 + 毛利率) + 固定运费, 2)
规则优先级：channel > category > supplier > global
"""

from src.pricing.engine import calculate_price, pick_rule

__all__ = ["calculate_price", "pick_rule"]