"""WMS 数据模型：货品主表 / 检测 / 单据 / 库存 / 支撑五层，共 20 张表。

两张表记一套事，是这里最容易搞混的地方，先说清楚：

  ``wms_items.grade``   等级（W/S/A/B/C/D）——**永久属性，只由检测决定**，
                        这台货永远是 B，修好了重新检测才会变。
  ``wms_items.status``  状态（PENDING/GRADED/SELLABLE/…）——**当前处境，由作业流转决定**。
                        混在一起存就会出现「B 档待修」和「B 档可售」分不开这种错。

条码口径（与方案「条码终身不变」对齐）：
  barcode      ``QC`` + 品类(PH) + 年月(2610) + 4 位流水 → ``QCPH26100012``
               **不含等级**：等级是可变字段，写进条码就得换码，与一码到底冲突。
  display_code ``QC-PH-2610-0012``，给人看的（带横杠，标签上印这个）。
"""
from __future__ import annotations

import time
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    select,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutil import now as _now


# ---------------------------------------------------------------- 品类
# 2026-10-04 对标《1688 类目对标看板》（上海婧米售后工单 881 条）收口：
# 品类选项 = 对标表的一级类目（按工单量排序），不再凭空自造「手机 / 平板 / 笔记本」。
# 条码规则不变：QC + 品类码 + 年月 + 流水（如 QCPH26100012）。
CATEGORY_PHONE = "PH"        # 手机通讯 → 1688「数码、电脑」
CATEGORY_APPLIANCE = "HA"    # 家用电器 → 1688「家用电器」
CATEGORY_OFFICE = "OA"       # 电脑、办公 → 1688「数码、电脑」
CATEGORY_COMMERCIAL = "CB"   # 商用电器 → 1688「机械及行业设备」
CATEGORY_DIGITAL = "SZ"      # 数码 → 1688「数码、电脑」
CATEGORY_HARDWARE = "WJ"     # 五金/工具 → 1688「五金、工具」
CATEGORY_INDUSTRIAL = "GY"   # 工业品 → 1688「机械及行业设备」
CATEGORY_BABY = "BB"         # 母婴 → 1688「家用电器（母婴电器）」
CATEGORY_WATCH = "ZJ"        # 钟表眼镜 → 1688「运动户外」（低置信，别当真）
CATEGORY_BEDDING = "CS"      # 床上用品 → 1688「家纺家饰」
CATEGORY_AUTO = "QY"         # 汽车用品 → 1688「汽车用品」
CATEGORY_PET = "CW"          # 宠物生活 → 1688「宠物及园艺」
CATEGORY_COMPONENT = "YQ"    # 元器件 → 1688「电子元器件」
CATEGORY_OTHER = "OT"

CATEGORY_CHOICES = (
    CATEGORY_APPLIANCE,
    CATEGORY_OFFICE,
    CATEGORY_PHONE,
    CATEGORY_COMMERCIAL,
    CATEGORY_DIGITAL,
    CATEGORY_HARDWARE,
    CATEGORY_INDUSTRIAL,
    CATEGORY_BABY,
    CATEGORY_WATCH,
    CATEGORY_BEDDING,
    CATEGORY_AUTO,
    CATEGORY_PET,
    CATEGORY_COMPONENT,
    CATEGORY_OTHER,
)
CATEGORY_LABELS = {
    CATEGORY_APPLIANCE: "家用电器",
    CATEGORY_OFFICE: "电脑、办公",
    CATEGORY_PHONE: "手机通讯",
    CATEGORY_COMMERCIAL: "商用电器",
    CATEGORY_DIGITAL: "数码",
    CATEGORY_HARDWARE: "五金/工具",
    CATEGORY_INDUSTRIAL: "工业品",
    CATEGORY_BABY: "母婴",
    CATEGORY_WATCH: "钟表眼镜",
    CATEGORY_BEDDING: "床上用品",
    CATEGORY_AUTO: "汽车用品",
    CATEGORY_PET: "宠物生活",
    CATEGORY_COMPONENT: "元器件",
    CATEGORY_OTHER: "其他",
    # 老数据兼容：早期品类码（平板 / 笔记本 / 手表）不再进下拉，但存量行要能显示
    "TB": "平板（并入 电脑、办公）",
    "NB": "笔记本（并入 电脑、办公）",
    "WT": "手表（并入 钟表眼镜）",
}

# 品类 ↔ 渔枢系统类目对标数据（来源：类目对标看板 2026-10-04，工单 881 条）。
# 规则页的「品类与对标」卡片直接读这里 —— 对标依据入库为配置，改对标只改这里。
CATEGORY_MAPPING: dict[str, dict] = {
    CATEGORY_APPLIANCE: {"l1": "家用电器", "tickets": 417, "confidence": "高"},
    CATEGORY_OFFICE: {"l1": "数码、电脑", "tickets": 123, "confidence": "高"},
    CATEGORY_PHONE: {"l1": "数码、电脑", "tickets": 119, "confidence": "高"},
    CATEGORY_COMMERCIAL: {"l1": "机械及行业设备", "tickets": 49, "confidence": "高"},
    CATEGORY_DIGITAL: {"l1": "数码、电脑", "tickets": 46, "confidence": "高"},
    CATEGORY_HARDWARE: {"l1": "五金、工具", "tickets": 32, "confidence": "高"},
    CATEGORY_INDUSTRIAL: {"l1": "机械及行业设备", "tickets": 21, "confidence": "低"},
    CATEGORY_BABY: {"l1": "家用电器（母婴电器）", "tickets": 18, "confidence": "中"},
    CATEGORY_WATCH: {"l1": "运动户外", "tickets": 12, "confidence": "低"},
    CATEGORY_BEDDING: {"l1": "家纺家饰", "tickets": 1, "confidence": "中"},
    CATEGORY_AUTO: {"l1": "汽车用品", "tickets": 1, "confidence": "高"},
    CATEGORY_PET: {"l1": "宠物及园艺", "tickets": 1, "confidence": "中"},
    CATEGORY_COMPONENT: {"l1": "电子元器件", "tickets": 1, "confidence": "高"},
}
CATEGORY_CONFIDENCE_TONE = {"高": "done", "中": "pending", "低": "failed"}

# ---------------------------------------------------------------- 三级品类树
# 建档时按**三级品类**选（一级 › 二级 › 三级），每一行叶子都带着对应的渔枢系统三级类目 ——
# 对应关系是**三级对三级**（对标依据《类目对标看板》三级映射 TOP40，对标源上海婧米
# 9.29 售后工单 881 条）。叶子上没写 map / tickets / conf 的就是「还没对标」的品类，
# 页面标「未对标」，等拿到工单数据再补，不猜。
# 存储值是「一级码.二级码.三级码」（如 HA.KC.DFB），条码只取第一段（QCHA…），
# 与老的两位品类码（PH / HA …）并存，历史条码与历史数据都不受影响。
CATEGORY_TREE: dict[str, dict] = {
    CATEGORY_APPLIANCE: {"label": "家用电器", "children": {
        "KC": {"label": "厨房小电", "children": {
            "PBJ": {"label": "破壁机", "map": "家用电器 › 厨房电器 › 家用破壁机", "tickets": 37, "conf": "高"},
            "DGN": {"label": "多功能锅", "map": "家用电器 › 厨房电器 › 多功能料理机", "tickets": 27, "conf": "高"},
            "DFB": {"label": "电饭煲", "map": "家用电器 › 厨房电器 › 迷你电饭煲", "tickets": 25, "conf": "高"},
            "DRSH": {"label": "电热水壶", "map": "家用电器 › 厨房电器 › 电热水壶", "tickets": 14, "conf": "高"},
            "DBC": {"label": "电饼铛", "map": "家用电器 › 厨房电器 › 电饼铛、煎烤炉", "tickets": 13, "conf": "高"},
            "YSH": {"label": "养生壶", "map": "家用电器 › 厨房电器 › 养生壶、煎药壶", "tickets": 13, "conf": "高"},
            "DYLG": {"label": "电压力锅", "map": "家用电器 › 厨房电器 › 电压力锅", "tickets": 11, "conf": "高"},
            "WBL": {"label": "微波炉", "map": "家用电器 › 厨房电器 › 微波炉", "tickets": 11, "conf": "高"},
            "JRC": {"label": "绞肉/切菜机", "map": "家用电器 › 厨房电器 › 家用菜馅机", "tickets": 10, "conf": "高"},
            "DCL": {"label": "电磁炉/电陶炉", "map": "家用电器 › 厨房电器 › 电陶炉", "tickets": 8, "conf": "高"},
            "KQZG": {"label": "空气炸锅", "map": "家用电器 › 厨房电器 › 家用空气炸锅", "tickets": 8, "conf": "高"},
            "DJJ": {"label": "豆浆机", "map": "家用电器 › 厨房电器 › 豆浆机", "tickets": 8, "conf": "高"},
            "ZCQ": {"label": "煮茶器/电茶盘", "map": "家用电器 › 厨房电器 › 电动煮茶器", "tickets": 7, "conf": "高"},
            "QT": {"label": "其它厨房小电"},
        }},
        "CD": {"label": "厨卫大电", "children": {
            "DRSLT": {"label": "电热水龙头", "map": "家用电器 › 大家电 › 电热水龙头", "tickets": 57, "conf": "高"},
            "QT": {"label": "其它厨卫大电"},
        }},
        "JD": {"label": "清洁电器", "children": {
            "SDJQR": {"label": "扫地机器人", "map": "家用电器 › 生活电器 › 家用扫地机", "tickets": 13, "conf": "高"},
            "XCQ": {"label": "吸尘器", "map": "家用电器 › 生活电器 › 家用吸尘器", "tickets": 7, "conf": "高"},
            "QT": {"label": "其它清洁电器"},
        }},
        "JR": {"label": "家庭服务机器人", "children": {
            "CCJQR": {"label": "擦窗机器人", "map": "家用电器 › 生活电器 › 擦窗机器人", "tickets": 38, "conf": "高"},
            "QT": {"label": "其它家庭服务机器人"},
        }},
        "GH": {"label": "个护健康", "children": {
            "DYS": {"label": "电动牙刷", "map": "家用电器 › 个护电器 › 电动牙刷", "tickets": 20, "conf": "高"},
            "ZYP": {"label": "足浴盆", "map": "家用电器 › 个护电器 › 足浴电器", "tickets": 6, "conf": "高"},
            "QT": {"label": "其它个护健康"},
        }},
        "JP": {"label": "家电配件", "children": {
            "DSJ": {"label": "电视配件", "map": "家用电器 › 大家电 › 电视", "tickets": 11, "conf": "高"},
            "QT": {"label": "其它家电配件"},
        }},
        "QT": {"label": "其它家用电器", "children": {"QT": {"label": "其它"}}},
    }},
    CATEGORY_PHONE: {"label": "手机通讯", "children": {
        "DJ": {"label": "对讲机", "children": {
            "MN": {"label": "模拟对讲机", "map": "数码、电脑 › 手机通讯 › 对讲机", "tickets": 63, "conf": "高"},
            "PJ": {"label": "对讲机配件", "map": "数码、电脑 › 手机通讯 › 对讲机", "tickets": 40, "conf": "高"},
            "GW": {"label": "公网对讲机", "map": "数码、电脑 › 手机通讯 › 对讲机", "tickets": 14, "conf": "高"},
            "QT": {"label": "其它对讲机"},
        }},
        "SJ": {"label": "手机", "children": {"QT": {"label": "其它手机"}}},
        "PJ": {"label": "手机配件", "children": {"QT": {"label": "其它手机配件"}}},
        "QT": {"label": "其它手机通讯", "children": {"QT": {"label": "其它"}}},
    }},
    CATEGORY_OFFICE: {"label": "电脑、办公", "children": {
        "DN": {"label": "电脑组件", "children": {
            "XSQ": {"label": "显示器", "map": "数码、电脑 › 电脑硬件及配件 › 液晶显示器", "tickets": 18, "conf": "高"},
            "QT": {"label": "其它电脑组件"},
        }},
        "WS": {"label": "外设产品", "children": {
            "XL": {"label": "线缆", "map": "数码、电脑 › 电脑外设产品 › 线缆附件", "tickets": 8, "conf": "低"},
            "YPH": {"label": "硬盘盒", "map": "数码、电脑 › 电脑外设产品 › 键盘", "tickets": 8, "conf": "高"},
            "JXYP": {"label": "移动机械硬盘", "map": "数码、电脑 › 电脑外设产品 › 机械鼠标", "tickets": 8, "conf": "高"},
            "GTYP": {"label": "移动固态硬盘", "map": "数码、电脑 › 电脑外设产品 › 固态硬盘", "tickets": 7, "conf": "高"},
            "QT": {"label": "其它外设产品"},
        }},
        "WL": {"label": "网络产品", "children": {
            "SW5G": {"label": "5G/4G上网", "map": "数码、电脑 › 网络设备 › 3G/4G上网卡读卡器", "tickets": 14, "conf": "高"},
            "QT": {"label": "其它网络产品"},
        }},
        "BG": {"label": "办公设备", "children": {
            "AFJK": {"label": "安防监控", "map": "办公、文化 › 办公设备 › 智能安防监控配件", "tickets": 14, "conf": "低"},
            "QT": {"label": "其它办公设备"},
        }},
        "HC": {"label": "办公耗材", "children": {
            "XG": {"label": "硒鼓", "map": "办公、文化 › 办公设备 › 硒鼓、粉盒", "tickets": 7, "conf": "高"},
            "QT": {"label": "其它办公耗材"},
        }},
        "QT": {"label": "其它电脑、办公", "children": {"QT": {"label": "其它"}}},
    }},
    CATEGORY_DIGITAL: {"label": "数码", "children": {
        "ZN": {"label": "智能设备", "children": {
            "ZNSB": {"label": "智能手表", "map": "数码、电脑 › 智能设备 › 智能手表", "tickets": 10, "conf": "高"},
            "XR": {"label": "XR设备", "map": "数码、电脑 › 智能设备 › 智能语音设备", "tickets": 8, "conf": "高"},
            "JKSX": {"label": "监控摄像", "map": "数码、电脑 › 智能设备 › 智能摄像机", "tickets": 6, "conf": "高"},
            "QT": {"label": "其它智能设备"},
        }},
        "QT": {"label": "其它数码", "children": {"QT": {"label": "其它"}}},
    }},
    CATEGORY_COMMERCIAL: {"label": "商用电器", "children": {
        "JR": {"label": "商用加热类设备", "children": {
            "DCL": {"label": "商用台式电磁炉/电陶炉", "map": "机械及行业设备 › 食品、饮料加工及餐饮行业设备 › 电陶炉", "tickets": 8, "conf": "低"},
            "DZL": {"label": "电炸炉/炸锅", "map": "机械及行业设备 › 食品、饮料加工及餐饮行业设备 › （未定三级）", "tickets": 7, "conf": "高"},
            "QT": {"label": "其它商用加热类设备"},
        }},
        "SP": {"label": "商用食品机械设备", "children": {
            "DJJ": {"label": "商用豆浆机/破壁机", "map": "机械及行业设备 › 食品、饮料加工及餐饮行业设备 › 商用打蛋机/鲜奶机", "tickets": 6, "conf": "高"},
            "QT": {"label": "其它商用食品机械设备"},
        }},
        "QT": {"label": "其它商用电器", "children": {"QT": {"label": "其它"}}},
    }},
    CATEGORY_BABY: {"label": "母婴", "children": {
        "WY": {"label": "喂养用品", "children": {
            "NDXD": {"label": "暖奶消毒", "map": "家用电器 › 母婴电器 › 消毒柜", "tickets": 8, "conf": "中"},
            "FSLLJ": {"label": "辅食料理机", "map": "家用电器 › 母婴电器 › 多功能料理机", "tickets": 7, "conf": "中"},
            "QT": {"label": "其它喂养用品"},
        }},
        "QT": {"label": "其它母婴", "children": {"QT": {"label": "其它"}}},
    }},
    CATEGORY_WATCH: {"label": "钟表眼镜", "children": {
        "SZ": {"label": "时钟", "children": {
            "GZ": {"label": "挂钟", "map": "运动户外 › （未定二级） › 挂钟、壁钟、墙贴钟", "tickets": 8, "conf": "低"},
            "QT": {"label": "其它时钟"},
        }},
        "YJ": {"label": "眼镜", "children": {"QT": {"label": "其它眼镜"}}},
        "QT": {"label": "其它钟表眼镜", "children": {"QT": {"label": "其它"}}},
    }},
    CATEGORY_HARDWARE: {"label": "五金/工具", "children": {
        "QT": {"label": "未细分", "children": {"QT": {"label": "其它五金/工具"}}}}},
    CATEGORY_INDUSTRIAL: {"label": "工业品", "children": {
        "QT": {"label": "未细分", "children": {"QT": {"label": "其它工业品"}}}}},
    CATEGORY_BEDDING: {"label": "床上用品", "children": {
        "QT": {"label": "未细分", "children": {"QT": {"label": "其它床上用品"}}}}},
    CATEGORY_AUTO: {"label": "汽车用品", "children": {
        "QT": {"label": "未细分", "children": {"QT": {"label": "其它汽车用品"}}}}},
    CATEGORY_PET: {"label": "宠物生活", "children": {
        "QT": {"label": "未细分", "children": {"QT": {"label": "其它宠物生活"}}}}},
    CATEGORY_COMPONENT: {"label": "元器件", "children": {
        "QT": {"label": "未细分", "children": {"QT": {"label": "其它元器件"}}}}},
    CATEGORY_OTHER: {"label": "其他", "children": {
        "QT": {"label": "未细分", "children": {"QT": {"label": "其它"}}}}},
}

# 叶子 / 节点索引：一次建好，后面按完整码 O(1) 查
_CATEGORY_LEAVES: dict[str, dict] = {}
_CATEGORY_NODES: dict[str, dict] = {}


def _index_tree(tree: dict, prefix: str = "") -> None:
    for code, node in tree.items():
        full = f"{prefix}{code}"
        _CATEGORY_NODES[full] = node
        kids = node.get("children") or {}
        if kids:
            _index_tree(kids, full + ".")
        else:
            _CATEGORY_LEAVES[full] = node


_index_tree(CATEGORY_TREE)


def category_leaf(code: str) -> dict | None:
    """完整三级码（``HA.KC.DFB``）→ 叶子节点（含 map / tickets / conf）；不是叶子返回 None。"""
    return _CATEGORY_LEAVES.get(str(code or "").strip().upper())


def category_node(code: str) -> dict | None:
    """任意层级码（``HA`` / ``HA.KC`` / ``HA.KC.DFB``）→ 节点。"""
    return _CATEGORY_NODES.get(str(code or "").strip().upper())


def l1_code_of(category: str) -> str:
    """条码用的一级品类码：三级码取第一段，老的两位码原样返回。"""
    raw = str(category or "").strip().upper()
    return raw.split(".")[0] if raw else CATEGORY_OTHER


def category_path(code: str, sep: str = " › ") -> str:
    """三级中文路径（``家用电器 › 厨房小电 › 电饭煲``）；老码回落一级名。"""
    raw = str(code or "").strip().upper()
    if not raw:
        return "—"
    parts = raw.split(".")
    labels: list[str] = []
    node_parent = CATEGORY_TREE
    for i, part in enumerate(parts):
        node = node_parent.get(part)
        if not node:
            return CATEGORY_LABELS.get(parts[0], raw)
        labels.append(str(node.get("label") or part))
        node_parent = node.get("children") or {}
    return sep.join(labels)


def category_1688(code: str) -> str:
    """对应的渔枢系统三级类目路径（三级对三级）；没对标返回空串。"""
    leaf = category_leaf(code)
    return str(leaf.get("map") or "") if leaf else ""


def leaf_codes() -> list[str]:
    """所有三级叶子码（按树顺序）：``['HA.KC.PBJ', ...]``。"""
    return list(_CATEGORY_LEAVES)


def category_options() -> list[list[str]]:
    """三级叶子下拉：``[[code, 路径], ...]``（按树顺序，收货建档 / 台账筛选都用它）。"""
    return [[code, category_path(code)] for code in _CATEGORY_LEAVES]


def category_label(value: str) -> str:
    raw = str(value or "").strip().upper()
    if not raw:
        return "—"
    if "." in raw:
        return category_path(raw)
    node = _CATEGORY_NODES.get(raw)
    if node:
        return str(node.get("label") or raw)
    return CATEGORY_LABELS.get(raw, raw or "—")


def category_confidence(code: str) -> tuple[str, str]:
    """叶子置信度：``(文案, 徽标 tone)``，没对标返回 ``("未对标", "muted")``。"""
    leaf = category_leaf(code)
    conf = str((leaf or {}).get("conf") or "")
    if not conf:
        return ("未对标", "muted")
    return (conf, CATEGORY_CONFIDENCE_TONE.get(conf, "muted"))


# ---------------------------------------------------------------- 等级
# W 是占位档：已建档未检测。
# N 全新（2026-10-10 新增）：原封未拆 / 拆封未使用都算这一档，比 S 完美还高一档 ——
# 现场这批退货里确实有「没拆过」和「拆了没用过」的货，以前只能判 S，白瞎了这个卖点。
GRADE_W = "W"
GRADE_N = "N"
GRADE_S = "S"
GRADE_A = "A"
GRADE_B = "B"
GRADE_C = "C"
GRADE_D = "D"

GRADE_CHOICES = (GRADE_W, GRADE_N, GRADE_S, GRADE_A, GRADE_B, GRADE_C, GRADE_D)
GRADE_LABELS = {
    GRADE_W: "W 待测",
    GRADE_N: "N 全新",
    GRADE_S: "S 完美",
    GRADE_A: "A 微瑕",
    # 2026-10-13 用户拍板：B 档重定义 —— 缺件、微瑕（非外观）但**不影响售卖使用**，
    # 照常可卖（渔货价 ×0.30 / 建议售价 ×0.40），不再走「厂内维修」
    GRADE_B: "B 缺/瑕疵/可卖",
    GRADE_C: "C 报修",
    GRADE_D: "D 报废",
}
# 页面色块：W 灰 / N 主色（比 S 高一档，单独一个色）/ S 绿 / A 黄 / B 蓝 / C 橙 / D 紫
GRADE_TONES = {
    GRADE_W: "muted",
    GRADE_N: "processing2",
    GRADE_S: "done",
    GRADE_A: "pending",
    GRADE_B: "pushed",
    GRADE_C: "processing2",
    GRADE_D: "screenshot",
}
# 可售档：销售出库认这四档（N 全新 / S 完美 / A 微瑕 / B 缺件微瑕可卖 —— 2026-10-13 起 B 也算）
SELLABLE_GRADES = (GRADE_N, GRADE_S, GRADE_A, GRADE_B)

# 已经判成「卖不了」的两档（2026-10-13）：报修中的 C、报废的 D ——
# 它们不许进可售仓、也不许置成「可售」状态（渔枢那边扫条码上架也要拦）。
NOT_SELLABLE_GRADES = (GRADE_C, GRADE_D)


def grade_label(value: str) -> str:
    return GRADE_LABELS.get(str(value or "").strip().upper(), str(value or "—") or "—")


def grade_tone(value: str) -> str:
    return GRADE_TONES.get(str(value or "").strip().upper(), "muted")


# ---------------------------------------------------------------- 生命周期状态
# 唯一定义处：别处禁止手写状态串（流转白名单见 state.py）
STATUS_PENDING = "PENDING"
STATUS_GRADED = "GRADED"
STATUS_SELLABLE = "SELLABLE"
STATUS_LOCKED = "LOCKED"
STATUS_SOLD = "SOLD"
STATUS_REPAIR = "REPAIR"
STATUS_OUT_REPAIR = "OUT_REPAIR"
STATUS_REWORK = "REWORK"
STATUS_RECALL = "RECALL"
STATUS_PARTS = "PARTS"
STATUS_HAZARD = "HAZARD"

STATUS_CHOICES = (
    STATUS_PENDING,
    STATUS_GRADED,
    STATUS_SELLABLE,
    STATUS_LOCKED,
    STATUS_SOLD,
    STATUS_REPAIR,
    STATUS_OUT_REPAIR,
    STATUS_REWORK,
    STATUS_RECALL,
    STATUS_PARTS,
    STATUS_HAZARD,
)
STATUS_LABELS = {
    STATUS_PENDING: "待检",
    STATUS_GRADED: "已定级",
    STATUS_SELLABLE: "可售",
    STATUS_LOCKED: "已锁",
    STATUS_SOLD: "已售",
    STATUS_REPAIR: "厂修中",
    STATUS_OUT_REPAIR: "报修在外",
    STATUS_REWORK: "返工复检",
    STATUS_RECALL: "召回",
    STATUS_PARTS: "拆机件",
    STATUS_HAZARD: "危废",
}
STATUS_TONES = {
    STATUS_PENDING: "muted",
    STATUS_GRADED: "pending",
    STATUS_SELLABLE: "done",
    STATUS_LOCKED: "processing2",
    STATUS_SOLD: "closed",
    STATUS_REPAIR: "pending",
    STATUS_OUT_REPAIR: "processing2",
    STATUS_REWORK: "pending",
    STATUS_RECALL: "screenshot",
    STATUS_PARTS: "screenshot",
    STATUS_HAZARD: "failed",
}
# 终态：不可再流出
TERMINAL_STATUSES = (STATUS_SOLD, STATUS_HAZARD)
# 禁止任何出库的状态（后端出库接口必须校验）
NO_OUTBOUND_STATUSES = (STATUS_PENDING, STATUS_REWORK, STATUS_HAZARD, STATUS_RECALL, STATUS_SOLD)


def status_label(value: str) -> str:
    return STATUS_LABELS.get(str(value or "").strip().upper(), str(value or "—") or "—")


def status_tone(value: str) -> str:
    return STATUS_TONES.get(str(value or "").strip().upper(), "muted")


# ---------------------------------------------------------------- 仓库
# 仓库分层：一期的底线是「仓库 + 等级」两级隔离（A 档与 S 档不能混放）
WH_PENDING = "PEND"
WH_SALE = "SALE"
WH_FIX = "FIX"
WH_OUT_REPAIR = "OUTR"
WH_PARTS = "PART"
WH_HAZARD = "HAZD"
WH_BACK = "BACK"

WAREHOUSE_DEFAULTS: tuple[tuple[str, str, str, bool], ...] = (
    # code, name, 存放说明, 可否出库
    (WH_PENDING, "待检仓", "W 状态货品，禁出库", False),
    (WH_SALE, "可售仓", "S / A 档，按等级分区", True),
    (WH_FIX, "维修仓", "B 档厂内维修", False),
    (WH_OUT_REPAIR, "报修仓", "C 档待送修（在库部分）", False),
    (WH_PARTS, "拆机件仓", "D 档待拆 / 已拆件", False),
    (WH_HAZARD, "危废区", "安全红线品，永久禁出", False),
    (WH_BACK, "退货暂存区", "召回待检", False),
)

# 内置仓（分级路由 ``GRADE_ROUTE`` 直接写死了它们的码）：不许删除，
# 要腾地方就「停用」—— 删了分级引擎会把货入到一个不存在的仓里。
BUILTIN_WAREHOUSE_CODES: frozenset[str] = frozenset(c for c, _n, _d, _o in WAREHOUSE_DEFAULTS)

# 层级：一张 wms_warehouses 表存五级，用 parent_code 串起来（2026-10-08 页面可维护）
WAREHOUSE_LEVEL_LABELS: dict[int, str] = {1: "仓库", 2: "区", 3: "货架", 4: "层", 5: "位"}
WAREHOUSE_MAX_LEVEL = 5

# 等级 → 默认仓 + 默认状态（定级后走这条路由）
GRADE_ROUTE = {
    GRADE_W: (WH_PENDING, STATUS_PENDING),
    GRADE_N: (WH_SALE, STATUS_SELLABLE),
    GRADE_S: (WH_SALE, STATUS_SELLABLE),
    GRADE_A: (WH_SALE, STATUS_SELLABLE),
    # 2026-10-13：B 档 = 缺件 / 微瑕可卖 → 直进可售仓（以前进维修仓）
    GRADE_B: (WH_SALE, STATUS_SELLABLE),
    GRADE_C: (WH_OUT_REPAIR, STATUS_OUT_REPAIR),
    GRADE_D: (WH_PARTS, STATUS_PARTS),
}


# ---------------------------------------------------------------- 仓库 / 库位名字缓存
# 2026-10-08 起仓库 / 区 / 货架能在「仓库与货架」页上改名、加新的，展示名就不能只认
# 上面那张常量表了。缓存由两处刷新：维护页写完立即刷（马上生效）+ 请求守卫里按 TTL 兜底。
_WAREHOUSE_NAMES: dict[str, str] = {}
_WAREHOUSE_NAMES_AT: float = 0.0


def register_warehouse_names(rows) -> None:
    """把表里的仓库 / 库位灌进名字缓存（编码 → 展示名，库位用全路径）。"""
    global _WAREHOUSE_NAMES_AT
    names: dict[str, str] = {}
    for row in rows:
        code = str(getattr(row, "code", "") or "").strip().upper()
        if not code:
            continue
        label = str(getattr(row, "full_path", "") or "").strip() or str(
            getattr(row, "name", "") or ""
        ).strip()
        names[code] = label or code
    _WAREHOUSE_NAMES.clear()
    _WAREHOUSE_NAMES.update(names)
    _WAREHOUSE_NAMES_AT = time.monotonic()


def ensure_warehouse_names(session, ttl: float = 5.0) -> None:
    """懒加载名字缓存（每 ttl 秒最多查一次表）；查不到就回退常量表，不影响页面。"""
    global _WAREHOUSE_NAMES_AT
    if time.monotonic() - _WAREHOUSE_NAMES_AT < ttl:
        return
    try:
        rows = list(session.scalars(select(WmsWarehouse)).all())
    except Exception:  # noqa: BLE001  表还没建好 / 库连不上：别每请求都试
        _WAREHOUSE_NAMES_AT = time.monotonic()
        return
    register_warehouse_names(rows)


def warehouse_label(code: str) -> str:
    """仓库 / 区 / 货架的展示名：先查表（改名即时生效）→ 内置 7 仓 → 原样给编码。"""
    key = str(code or "").strip().upper()
    if not key:
        return "—"
    if key in _WAREHOUSE_NAMES:
        return _WAREHOUSE_NAMES[key]
    for c, name, _desc, _out in WAREHOUSE_DEFAULTS:
        if c == key:
            return name
    return str(code)


def location_label(code: str) -> str:
    """库位（第 2 级以下）的展示名：表里有就给全路径，没有就原样给（历史手填的值）。"""
    key = str(code or "").strip().upper()
    if not key:
        return ""
    return _WAREHOUSE_NAMES.get(key) or str(code)


# ---------------------------------------------------------------- 库存流水
LEDGER_INBOUND = "INBOUND"      # 入库
LEDGER_GRADE = "GRADE"          # 定级（改档）
LEDGER_LABEL = "LABEL"          # 贴标复核
LEDGER_MOVE = "MOVE"            # 移库
LEDGER_UP = "UP"                # 上架
LEDGER_DOWN = "DOWN"            # 下架
LEDGER_LOCK = "LOCK"            # 锁货
LEDGER_UNLOCK = "UNLOCK"        # 释放
LEDGER_OUT = "OUT"              # 出库
LEDGER_SCRAP = "SCRAP"          # 报废 / 转危废
LEDGER_ADJUST = "ADJUST"        # 盘盈盘亏调整

LEDGER_CHOICES = (
    LEDGER_INBOUND, LEDGER_GRADE, LEDGER_LABEL, LEDGER_MOVE, LEDGER_UP,
    LEDGER_DOWN, LEDGER_LOCK, LEDGER_UNLOCK, LEDGER_OUT, LEDGER_SCRAP, LEDGER_ADJUST,
)
LEDGER_LABELS = {
    LEDGER_INBOUND: "入库",
    LEDGER_GRADE: "定级",
    LEDGER_LABEL: "贴标复核",
    LEDGER_MOVE: "移库",
    LEDGER_UP: "上架",
    LEDGER_DOWN: "下架",
    LEDGER_LOCK: "锁货",
    LEDGER_UNLOCK: "释放",
    LEDGER_OUT: "出库",
    LEDGER_SCRAP: "报废",
    LEDGER_ADJUST: "盘点调整",
}


def ledger_label(value: str) -> str:
    return LEDGER_LABELS.get(str(value or "").strip().upper(), str(value or "—") or "—")


# ---------------------------------------------------------------- 出库
OUT_SALE = "SALE"
OUT_REPAIR = "REPAIR"
OUT_TRANSFER = "TRANSFER"
OUT_PARTS = "PARTS"

OUTBOUND_CHOICES = (OUT_SALE, OUT_REPAIR, OUT_TRANSFER, OUT_PARTS)
OUTBOUND_LABELS = {
    OUT_SALE: "销售出库",
    OUT_REPAIR: "报修出库",
    OUT_TRANSFER: "调拨出库",
    OUT_PARTS: "拆机件出库",
}
OUTBOUND_STATUS_DRAFT = "draft"
OUTBOUND_STATUS_PICKING = "picking"
OUTBOUND_STATUS_SHIPPED = "shipped"
OUTBOUND_STATUS_CANCELLED = "cancelled"
OUTBOUND_STATUS_CHOICES = (
    OUTBOUND_STATUS_DRAFT,
    OUTBOUND_STATUS_PICKING,
    OUTBOUND_STATUS_SHIPPED,
    OUTBOUND_STATUS_CANCELLED,
)
OUTBOUND_STATUS_LABELS = {
    OUTBOUND_STATUS_DRAFT: "草稿",
    OUTBOUND_STATUS_PICKING: "拣货中",
    OUTBOUND_STATUS_SHIPPED: "已出库",
    OUTBOUND_STATUS_CANCELLED: "已取消",
}


def outbound_type_label(value: str) -> str:
    return OUTBOUND_LABELS.get(str(value or "").strip().upper(), str(value or "—") or "—")


def outbound_status_label(value: str) -> str:
    key = str(value or "").strip().lower()
    return OUTBOUND_STATUS_LABELS.get(key, key or "—")


# ---------------------------------------------------------------- 报修
REPAIR_C1 = "C1"
REPAIR_C2 = "C2"
REPAIR_CHANNELS = (REPAIR_C1, REPAIR_C2)
REPAIR_CHANNEL_LABELS = {REPAIR_C1: "C1 线上（京东 / 400）", REPAIR_C2: "C2 线下（授权网点）"}

REPAIR_OPEN = "open"            # 已开单，待送修
REPAIR_SENT = "sent"            # 已寄出（在途）
REPAIR_RETURNED = "returned"    # 已修回（待重检）
REPAIR_CLOSED = "closed"        # 结案（重检完成 / 转拆件）
REPAIR_CANCELLED = "cancelled"  # 撤单
REPAIR_STATUS_CHOICES = (
    REPAIR_OPEN, REPAIR_SENT, REPAIR_RETURNED, REPAIR_CLOSED, REPAIR_CANCELLED,
)
REPAIR_STATUS_LABELS = {
    REPAIR_OPEN: "待送修",
    REPAIR_SENT: "在途报修",
    REPAIR_RETURNED: "已修回",
    REPAIR_CLOSED: "已结案",
    REPAIR_CANCELLED: "已撤单",
}
# 超时预警默认天数（方案口径：默认 15 天）
REPAIR_TIMEOUT_DAYS = 15


def repair_channel_label(value: str) -> str:
    return REPAIR_CHANNEL_LABELS.get(str(value or "").strip().upper(), str(value or "—") or "—")


def repair_status_label(value: str) -> str:
    key = str(value or "").strip().lower()
    return REPAIR_STATUS_LABELS.get(key, key or "—")


# ---------------------------------------------------------------- 拆机件
PART_MAINBOARD = "mainboard"
PART_SCREEN = "screen"
PART_BATTERY = "battery"
PART_CAMERA = "camera"
PART_PORT = "port"
PART_SPEAKER = "speaker"
PART_OTHER = "other"
PART_CHOICES = (
    PART_MAINBOARD, PART_SCREEN, PART_BATTERY, PART_CAMERA, PART_PORT, PART_SPEAKER, PART_OTHER,
)
PART_LABELS = {
    PART_MAINBOARD: "主板",
    PART_SCREEN: "屏总成",
    PART_BATTERY: "电池",
    PART_CAMERA: "摄像头",
    PART_PORT: "尾插",
    PART_SPEAKER: "喇叭",
    PART_OTHER: "其他",
}


def part_label(value: str) -> str:
    return PART_LABELS.get(str(value or "").strip().lower(), str(value or "—") or "—")


# ---------------------------------------------------------------- 检测项
TEST_PASS = "pass"
TEST_FAIL = "fail"
TEST_NA = "na"
TEST_RESULT_CHOICES = (TEST_PASS, TEST_FAIL, TEST_NA)
TEST_RESULT_LABELS = {TEST_PASS: "√ 合格", TEST_FAIL: "× 不合格", TEST_NA: "— 不适用"}

# 安全红线项：命中任意一项 → 直接危废（禁止充电与任何流转）
# 2026-10-13 由 6 项收敛成 3 项（用户拍板）：鼓包与漏液、异味与焦糊火烧、漏电与触电
# 三类本来就是「看到就直接进危废」，拆成 6 格只是让检测员多点 3 下。
# key 沿用 swell / burn / shock 三个老名，历史检测记录照样对得上。
SAFETY_ITEM_KEYS = ("swell", "burn", "shock")


def test_result_label(value: str) -> str:
    return TEST_RESULT_LABELS.get(str(value or "").strip().lower(), str(value or "—") or "—")


# ---------------------------------------------------------------- 批次 / 入库单
BATCH_PURCHASE = "purchase"
BATCH_RECYCLE = "recycle"
BATCH_CLEARANCE = "clearance"
BATCH_RETURN = "return"
BATCH_SOURCES = (BATCH_PURCHASE, BATCH_RECYCLE, BATCH_CLEARANCE, BATCH_RETURN)
BATCH_SOURCE_LABELS = {
    BATCH_PURCHASE: "采购",
    BATCH_RECYCLE: "回收",
    BATCH_CLEARANCE: "清仓",
    BATCH_RETURN: "退换",
}

BATCH_DRAFT = "draft"
BATCH_OPEN = "open"
BATCH_CLOSED = "closed"
BATCH_STATUS_CHOICES = (BATCH_DRAFT, BATCH_OPEN, BATCH_CLOSED)
BATCH_STATUS_LABELS = {
    BATCH_DRAFT: "草稿",
    BATCH_OPEN: "进行中",
    BATCH_CLOSED: "已完成",
}


def batch_source_label(value: str) -> str:
    return BATCH_SOURCE_LABELS.get(str(value or "").strip().lower(), str(value or "—") or "—")


def batch_status_label(value: str) -> str:
    key = str(value or "").strip().lower()
    return BATCH_STATUS_LABELS.get(key, key or "—")


# ---------------------------------------------------------------- 标签
LABEL_W = "W"
LABEL_FORMAL = "formal"
LABEL_REPRINT = "reprint"
LABEL_TYPES = (LABEL_W, LABEL_FORMAL, LABEL_REPRINT)
LABEL_TYPE_LABELS = {
    LABEL_W: "W 占位码",
    LABEL_FORMAL: "正式码",
    LABEL_REPRINT: "补打",
}


def label_type_label(value: str) -> str:
    return LABEL_TYPE_LABELS.get(str(value or "").strip().lower(), str(value or "—") or "—")


# ---------------------------------------------------------------- 盘点
STOCKTAKE_CYCLE = "cycle"
STOCKTAKE_BATCH = "batch"
STOCKTAKE_ALL = "all"
STOCKTAKE_VALUE = "value"
STOCKTAKE_SCOPES = (STOCKTAKE_CYCLE, STOCKTAKE_BATCH, STOCKTAKE_ALL, STOCKTAKE_VALUE)
STOCKTAKE_SCOPE_LABELS = {
    STOCKTAKE_CYCLE: "循环盘点",
    STOCKTAKE_BATCH: "批次盘点",
    STOCKTAKE_ALL: "月度全盘",
    STOCKTAKE_VALUE: "专项盘点（高价值）",
}

STOCKTAKE_DRAFT = "draft"
STOCKTAKE_DONE = "done"
STOCKTAKE_APPROVED = "approved"
STOCKTAKE_STATUS_CHOICES = (STOCKTAKE_DRAFT, STOCKTAKE_DONE, STOCKTAKE_APPROVED)
STOCKTAKE_STATUS_LABELS = {
    STOCKTAKE_DRAFT: "盘点中",
    STOCKTAKE_DONE: "待审批",
    STOCKTAKE_APPROVED: "已审批",
}
# 差异率阈值（超过自动升级主管审批）
STOCKTAKE_DIFF_RATE_LIMIT = 0.005
# 单笔差异金额阈值（元）
STOCKTAKE_DIFF_AMOUNT_LIMIT = 500.0


def stocktake_scope_label(value: str) -> str:
    return STOCKTAKE_SCOPE_LABELS.get(str(value or "").strip().lower(), str(value or "—") or "—")


def stocktake_status_label(value: str) -> str:
    key = str(value or "").strip().lower()
    return STOCKTAKE_STATUS_LABELS.get(key, key or "—")


# ---------------------------------------------------------------- 表
class WmsWarehouse(Base):
    """仓库与库位：一张表存五级（仓库 → 区 → 货架 → 层 → 位），用 parent_code 串起来。

    一期可以只用仓库级（level=1）；库位开了之后，出库时会比对「库位等级区」
    与「货品等级」，不一致直接告警（仓管拿错一单就是几百块）。
    """

    __tablename__ = "wms_warehouses"
    __table_args__ = (
        Index("ix_wms_wh_parent", "parent_code"),
        UniqueConstraint("code", name="uq_wms_warehouse_code"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(64), default="")
    # 1=仓库 2=区 3=货架 4=层 5=位
    level: Mapped[int] = mapped_column(Integer, default=1)
    parent_code: Mapped[str] = mapped_column(String(32), default="")
    full_path: Mapped[str] = mapped_column(String(255), default="")
    # 库位归属的等级区（S 区 / A 区）：出库时用来校验有没有放错区
    grade_zone: Mapped[str] = mapped_column(String(4), default="")
    # 这个仓能不能出库（危废区 / 待检仓 = False）
    can_out: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsSupplier(Base):
    """货品来源：采购 / 回收 / 清仓 / 退换的对方（供应商门户不做，Excel 导入替代）。"""

    __tablename__ = "wms_suppliers"
    __table_args__ = (UniqueConstraint("code", name="uq_wms_supplier_code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(128), default="")
    contact: Mapped[str] = mapped_column(String(64), default="")
    phone: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsGradeRule(Base):
    """分级规则：品类 × 条件 → 等级。**改分级标准只改这里，不改代码。**

    conditions 是 JSON：``{"all_of":[{"field":"battery_health","op":">=","value":85}]}``；
    safety_flag=true 的规则永远第一匹配（安全红线优先于一切）。
    """

    __tablename__ = "wms_grade_rules"
    __table_args__ = (Index("ix_wms_grade_rule_cat", "category"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    category: Mapped[str] = mapped_column(String(32), default="*")
    priority: Mapped[int] = mapped_column(Integer, default=100)
    conditions: Mapped[dict] = mapped_column(JSON, default=dict)
    grade: Mapped[str] = mapped_column(String(2), default=GRADE_A)
    safety_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(128), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    effective_from: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    effective_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsPriceRule(Base):
    """价格系数：等级 → 系数 / 最低毛利率 / 低于底价需审批的线。"""

    __tablename__ = "wms_price_rules"
    __table_args__ = (UniqueConstraint("grade", name="uq_wms_price_rule_grade"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    grade: Mapped[str] = mapped_column(String(2), default=GRADE_A)
    # 渔货价（对外供货价）= 基准价 × factor
    factor: Mapped[float] = mapped_column(Float, default=1.0)
    # 建议售价系数（2026-10-10）：建议售价 = 基准价 × suggest_factor。
    # 它是渔枢 / 渔货栈「建议售价」的基础数据 —— 渔集把货定完级、确定渔货价的同时
    # 就把建议售价算出来，渔枢建档时直接带过去，不让人再拍一次脑袋。
    # 硬规矩：渔货价 ≤ 建议售价（供货价不能比建议零售价还高，见 grading.price_of）。
    suggest_factor: Mapped[float] = mapped_column(Float, default=1.0)
    # 最低毛利率（0.1 = 10%）：低于它只告警不拦截
    min_margin: Mapped[float] = mapped_column(Float, default=0.0)
    # 低于基准价这个比例就要主管审批（0.8 = 八折以下）
    need_approval_below: Mapped[float] = mapped_column(Float, default=0.0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsBatch(Base):
    """批次：号段在这里预留（serial_start / serial_end），建档时按段领用，避免重号。"""

    __tablename__ = "wms_batches"
    __table_args__ = (
        UniqueConstraint("batch_no", name="uq_wms_batch_no"),
        Index("ix_wms_batch_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(32), default="")
    category: Mapped[str] = mapped_column(String(32), default=CATEGORY_PHONE)
    source_type: Mapped[str] = mapped_column(String(16), default=BATCH_PURCHASE)
    supplier_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supplier_name: Mapped[str] = mapped_column(String(128), default="")
    qty_expected: Mapped[int] = mapped_column(Integer, default=0)
    qty_actual: Mapped[int] = mapped_column(Integer, default=0)
    # 混合品类批次（2026-10-08）：吃进来的这一批货什么类目都有，建批次时定不了品类，
    # 品类按导入表格每行定（item.category 各写各的），批次的 category 只作兜底。
    mixed: Mapped[bool] = mapped_column(Boolean, default=False)
    # 号段：YYMM-NN + 4 位流水（如 2610-01-0001 ~ 2610-01-0500）
    serial_start: Mapped[int] = mapped_column(Integer, default=1)
    serial_end: Mapped[int] = mapped_column(Integer, default=500)
    serial_next: Mapped[int] = mapped_column(Integer, default=1)
    warehouse_code: Mapped[str] = mapped_column(String(16), default=WH_PENDING)
    status: Mapped[str] = mapped_column(String(16), default=BATCH_DRAFT)
    # 禁用（2026-10-08）：批次建错 / 不再用了，就「禁用」—— 它不再出现在建档、检测、
    # 导入的批次选择里，但**历史货品和流水一条不少**（跟「删除」的区别：删只能删空批次）。
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsInboundOrder(Base):
    """入库单：与批次分离 —— 可以整批一次入，也可以分几次入。"""

    __tablename__ = "wms_inbound_orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_wms_inbound_no"),
        Index("ix_wms_inbound_batch", "batch_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), default="")
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_no: Mapped[str] = mapped_column(String(32), default="")
    warehouse_code: Mapped[str] = mapped_column(String(16), default=WH_PENDING)
    total_qty: Mapped[int] = mapped_column(Integer, default=0)
    checked_qty: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="draft")
    checker: Mapped[str] = mapped_column(String(64), default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsItem(Base):
    """货品主表（一机一码）—— 系统的心脏。

    grade 与 status 分开存（见文件头说明）；ver 是乐观锁，防两个检测员同时改同一台。
    """

    __tablename__ = "wms_items"
    __table_args__ = (
        UniqueConstraint("barcode", name="uq_wms_item_barcode"),
        Index("ix_wms_item_status", "status"),
        Index("ix_wms_item_grade", "grade"),
        Index("ix_wms_item_batch", "batch_id"),
        Index("ix_wms_item_warehouse", "warehouse_code"),
        # 货号：供应商清单 / Excel 里带来的编号，扫码枪扫它就是扫这台货
        Index("ix_wms_item_sku", "sku_code"),
        # 渔货号：点进台账认这一款货（一个渔货号下挂着 N 条码商品）
        Index("ix_wms_item_fish", "fish_code"),
        # 检修后自己贴的码（扫码也要认它）
        Index("ix_wms_item_own", "own_barcode"),
        Index("ix_wms_item_import", "import_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 条码本体：QC + 品类 + 年月 + 流水，唯一、终身不变、**不含等级**
    barcode: Mapped[str] = mapped_column(String(24), default="")
    display_code: Mapped[str] = mapped_column(String(32), default="")
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_no: Mapped[str] = mapped_column(String(32), default="")
    inbound_order_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    category: Mapped[str] = mapped_column(String(32), default=CATEGORY_PHONE)
    brand: Mapped[str] = mapped_column(String(64), default="")
    # 产品 / 商品名称（2026-10-08，按供应商清单口径）：清单里「产品」那一列往往是一长串商品名
    # （品牌 + 卖点 + 规格），信息量比品牌型号大得多 —— 展示与检测载入优先用它。
    product_name: Mapped[str] = mapped_column(String(255), default="")
    model: Mapped[str] = mapped_column(String(128), default="")
    spec: Mapped[str] = mapped_column(String(128), default="")
    serial_no: Mapped[str] = mapped_column(String(64), default="")
    # 数量（2026-10-10）：**一台货品可以带 N 台**（同一个条码，不再按数量拆成 N 个码）。
    # 供应商清单里「数量 3」就是这一条货 3 台 —— 以前是复制成 3 条货品、3 个条码，
    # 现场扫码会变成「到底扫哪一台」，台账也数不清；现在建 1 条、qty=3，
    # 条码终身唯一、数量单独摆在最显眼的地方。
    # 口径：**流转整台走**（入库 / 出库 / 报修 / 核销都按整台的数量进出，不拆分），
    # 库存余额与流水 qty_delta 都用这个数（见 ledger.apply）。
    qty: Mapped[int] = mapped_column(Integer, default=1)
    # 进货货号（2026-10-08，原名「货号」2026-10-14 改名）：Excel 导入带来的供应商货号 / 款号
    # —— 现场检测**扫货号**就认这台货，与系统条码 QC… 是两套号：条码终身不变、
    # 货号是**外部编号**（一台一个，同一个供应商货号不能去重），所以内部认「款」不用它。
    sku_code: Mapped[str] = mapped_column(String(64), default="")
    # 渔货号（2026-10-14）：渔集自己的**商品款**编码 —— ``YS + 建档日期 + 当天 4 位流水``。
    # 它是一个款级别的概念：**一个渔货号对应多个条码商品**（同款多台、多次收货、多批次），
    # 货品台账以它为维度（见 ``intake.next_fish_code`` / ``resolve_fish_code``）。
    # 渔枢系统的商品档案（商家编码）与库存也认它：库存 = 该渔货号下所有条码商品的合计台数。
    fish_code: Mapped[str] = mapped_column(String(32), default="")
    # 检修后条码（2026-10-08）：检修规整完**自己重新贴的码**（不再用供应商那个）——
    # 供应商货号可能重号 / 磨花，自有码一码一台、终身唯一，复检与出库扫它就够。
    own_barcode: Mapped[str] = mapped_column(String(64), default="")
    own_barcode_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 扩展字段（导入表格里沉淀下来的列，见 wms_item_fields）：{"颜色": "黑色", "供应商批次": "A12"}
    extra: Mapped[dict] = mapped_column(JSON, default=dict)
    # 这一台是第几次导入建档带进来的（追溯用）
    import_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grade: Mapped[str] = mapped_column(String(2), default=GRADE_W)
    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING)
    warehouse_code: Mapped[str] = mapped_column(String(16), default=WH_PENDING)
    location_code: Mapped[str] = mapped_column(String(32), default="")
    purchase_cost: Mapped[float] = mapped_column(Float, default=0.0)
    base_price: Mapped[float] = mapped_column(Float, default=0.0)
    selling_price: Mapped[float] = mapped_column(Float, default=0.0)
    # 建议售价（2026-10-12）：手工建档 / 编辑时人工填的建议零售价（照渔枢「规格 · 价格 · 库存」的口径）。
    # 留空（0）就按「市场价 × 建议售价系数」算出来（见 WmsPriceRule.suggest_factor）；
    # 渔枢「扫渔集货品」带出资料时优先用它 —— 人工填过的数不该被系数盖掉。
    suggest_price: Mapped[float] = mapped_column(Float, default=0.0)
    residual_value: Mapped[float] = mapped_column(Float, default=0.0)
    battery_health: Mapped[int | None] = mapped_column(Integer, nullable=True)
    grade_reason: Mapped[str] = mapped_column(Text, default="")
    flaw_desc: Mapped[str] = mapped_column(Text, default="")
    photos: Mapped[list] = mapped_column(JSON, default=list)
    # 被哪个订单锁定（出库单号 / 外部订单号）
    locked_by: Mapped[str] = mapped_column(String(64), default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 安全红线命中：禁止充电与任何流转
    safety_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    # 待复检（检测员判「?」存疑，进复检池不隔夜）
    need_recheck: Mapped[bool] = mapped_column(Boolean, default=False)
    ver: Mapped[int] = mapped_column(Integer, default=0)
    graded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def title(self) -> str:
        """展示名：清单带来的「产品」全名优先（信息最全），其次品牌 + 型号 + 规格。"""
        name = str(self.product_name or "").strip()
        if name:
            return name
        parts = [self.brand, self.model, self.spec]
        return " ".join(p for p in parts if str(p or "").strip()) or self.barcode


class WmsItemField(Base):
    """商品扩展字段（2026-10-08）：Excel 导入时表格里多出来的列，登记后就成了商品表的字段。

    系统自带的那些（货号 / 品牌 / 型号 / 规格 / 序列号 / 成本…）是 ``wms_items`` 上的真列，
    **不在这里记**；这张表只管「导入表格带进来、后来沉淀下来的自定义列」（值存
    ``wms_items.extra``）。字段登记之后可以在「商品字段」页改名、改类型、停用、排序 ——
    改名不改值（值按 key 存），所以历史货品的取值不受影响。
    """

    __tablename__ = "wms_item_fields"
    __table_args__ = (UniqueConstraint("key", name="uq_wms_item_field_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 字段标识（英文 / 拼音 / 数字串，导入时由表头生成，值按它存进 extra）
    key: Mapped[str] = mapped_column(String(64), default="")
    # 展示名（就是表格里那个表头，可改）
    label: Mapped[str] = mapped_column(String(64), default="")
    # text / number / date（只影响展示与导入时的解析，不做强校验）
    field_type: Mapped[str] = mapped_column(String(16), default="text")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    sort: Mapped[int] = mapped_column(Integer, default=100)
    # 来源（2026-10-08）：这个字段是从哪张表带进来的（导入时的文件名 + 批次号），
    # 手工加的写「手工添加」—— 商品字段以导入的表头为准，出处要在页面上交代清楚。
    source: Mapped[str] = mapped_column(String(255), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsImportLog(Base):
    """Excel 导入建档的记录：哪张表、多少行、成了几台、哪些列沉淀成了字段。

    导入是**不可撤销的写库存动作**（生成条码 + 写入库流水），所以要留痕：
    事后对不上数能追到是哪张表、按什么映射导进来的。
    """

    __tablename__ = "wms_import_logs"
    __table_args__ = (Index("ix_wms_import_batch", "batch_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_no: Mapped[str] = mapped_column(String(32), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    ok_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    # 列映射 {表头: 字段 key}，沉淀成字段的那些也在里面
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    # 本次沉淀 / 复用的扩展字段 key
    fields: Mapped[list] = mapped_column(JSON, default=list)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    operator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsTestRecord(Base):
    """检测记录：**一台货可以有多次**（初检 / 复检 / 修回重检），只追加不覆盖。"""

    __tablename__ = "wms_test_records"
    __table_args__ = (
        Index("ix_wms_test_item", "item_id"),
        Index("ix_wms_test_tester", "tester"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode: Mapped[str] = mapped_column(String(24), default="")
    category: Mapped[str] = mapped_column(String(32), default=CATEGORY_PHONE)
    round: Mapped[int] = mapped_column(Integer, default=1)
    result_json: Mapped[dict] = mapped_column(JSON, default=dict)
    battery_health: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    flaw_count: Mapped[int] = mapped_column(Integer, default=0)
    all_items_pass: Mapped[bool] = mapped_column(Boolean, default=False)
    safety_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    suggest_grade: Mapped[str] = mapped_column(String(2), default="")
    final_grade: Mapped[str] = mapped_column(String(2), default="")
    # 引擎依据：给检测员看的，不能只返回档位（见方案 8.2）
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 人工覆盖引擎建议：必须填理由，这是规则调优的唯一依据
    override_reason: Mapped[str] = mapped_column(Text, default="")
    uncertain: Mapped[bool] = mapped_column(Boolean, default=False)
    is_recheck: Mapped[bool] = mapped_column(Boolean, default=False)
    tester: Mapped[str] = mapped_column(String(64), default="")
    photo_urls: Mapped[list] = mapped_column(JSON, default=list)
    note: Mapped[str] = mapped_column(Text, default="")
    tested_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsTestItem(Base):
    """检测项结果：逐项独立存，便于统计「哪项最常坏」。"""

    __tablename__ = "wms_test_items"
    __table_args__ = (
        Index("ix_wms_test_item_record", "record_id"),
        Index("ix_wms_test_item_key", "item_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    record_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_key: Mapped[str] = mapped_column(String(32), default="")
    item_label: Mapped[str] = mapped_column(String(64), default="")
    result: Mapped[str] = mapped_column(String(8), default=TEST_PASS)
    fail_desc: Mapped[str] = mapped_column(Text, default="")
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsStockLedger(Base):
    """库存流水：**append-only**，禁改禁删。库存余额由它汇总。

    idempotent_key 防网络重试造成重复扣减（同一单重复提交只会记一条）。
    """

    __tablename__ = "wms_stock_ledgers"
    __table_args__ = (
        UniqueConstraint("idempotent_key", name="uq_wms_ledger_idem"),
        Index("ix_wms_ledger_item", "item_id"),
        Index("ix_wms_ledger_biz", "biz_type"),
        Index("ix_wms_ledger_at", "operated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode: Mapped[str] = mapped_column(String(24), default="")
    biz_type: Mapped[str] = mapped_column(String(16), default=LEDGER_INBOUND)
    biz_id: Mapped[str] = mapped_column(String(64), default="")
    from_warehouse: Mapped[str] = mapped_column(String(16), default="")
    to_warehouse: Mapped[str] = mapped_column(String(16), default="")
    from_status: Mapped[str] = mapped_column(String(16), default="")
    to_status: Mapped[str] = mapped_column(String(16), default="")
    qty_delta: Mapped[int] = mapped_column(Integer, default=0)
    operator: Mapped[str] = mapped_column(String(64), default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    idempotent_key: Mapped[str] = mapped_column(String(128), default="")
    operated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsStockBalance(Base):
    """库存余额（仓库 × 状态 × 等级）：**由流水汇总，不允许手工改**。

    口径见方案 4.2：在库总数不含在外报修，但要含拆机件；这两个数报表里分开列。
    """

    __tablename__ = "wms_stock_balances"
    __table_args__ = (
        UniqueConstraint("warehouse_code", "status", "grade", name="uq_wms_balance"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    warehouse_code: Mapped[str] = mapped_column(String(16), default="")
    status: Mapped[str] = mapped_column(String(16), default="")
    grade: Mapped[str] = mapped_column(String(2), default="")
    qty: Mapped[int] = mapped_column(Integer, default=0)
    # 在途：报修寄出去的货，物理上不在手上，但钱已垫付
    in_transit_qty: Mapped[int] = mapped_column(Integer, default=0)
    last_ledger_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsOutboundOrder(Base):
    """出库单：4 类（销售 / 报修 / 调拨 / 拆机件）共一张表，用 type 区分。"""

    __tablename__ = "wms_outbound_orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_wms_outbound_no"),
        Index("ix_wms_outbound_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), default="")
    type: Mapped[str] = mapped_column(String(16), default=OUT_SALE)
    # 关联单据号（销售订单号 / 报修工单号 / 调拨单号）
    target_no: Mapped[str] = mapped_column(String(64), default="")
    warehouse_code: Mapped[str] = mapped_column(String(16), default=WH_SALE)
    to_warehouse: Mapped[str] = mapped_column(String(16), default="")
    item_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default=OUTBOUND_STATUS_DRAFT)
    express_company: Mapped[str] = mapped_column(String(64), default="")
    waybill_no: Mapped[str] = mapped_column(String(64), default="")
    receiver_name: Mapped[str] = mapped_column(String(64), default="")
    receiver_phone: Mapped[str] = mapped_column(String(64), default="")
    receiver_addr: Mapped[str] = mapped_column(Text, default="")
    remark: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsOutboundItem(Base):
    """出库明细：含拣货校验结果（错货 / 错等级当场拦，记录拦截原因）。"""

    __tablename__ = "wms_outbound_items"
    __table_args__ = (Index("ix_wms_outbound_item_order", "outbound_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    outbound_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode: Mapped[str] = mapped_column(String(24), default="")
    # 订单要求的等级（销售出库必填，用来拦「A 档当 S 档发」）
    require_grade: Mapped[str] = mapped_column(String(2), default="")
    grade: Mapped[str] = mapped_column(String(2), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    price: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    picked: Mapped[bool] = mapped_column(Boolean, default=False)
    picked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 拦截原因（为空 = 校验通过）
    fail_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsRepairOrder(Base):
    """报修工单（C 档）：线上 / 线下双通道，解决「货寄出去没人跟进」的黑洞。"""

    __tablename__ = "wms_repair_orders"
    __table_args__ = (
        UniqueConstraint("order_no", name="uq_wms_repair_no"),
        Index("ix_wms_repair_status", "status"),
        Index("ix_wms_repair_item", "item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_no: Mapped[str] = mapped_column(String(32), default="")
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode: Mapped[str] = mapped_column(String(24), default="")
    category: Mapped[str] = mapped_column(String(32), default=CATEGORY_PHONE)
    title: Mapped[str] = mapped_column(String(255), default="")
    serial_no: Mapped[str] = mapped_column(String(64), default="")
    # C1 线上（京东 / 400）/ C2 线下（授权网点）
    channel: Mapped[str] = mapped_column(String(4), default=REPAIR_C1)
    vendor: Mapped[str] = mapped_column(String(128), default="")
    # 凭证号：C1 是工单号 / 寄修单号，C2 是网点联单号
    ticket_no: Mapped[str] = mapped_column(String(128), default="")
    contact_name: Mapped[str] = mapped_column(String(64), default="")
    contact_phone: Mapped[str] = mapped_column(String(64), default="")
    contact_addr: Mapped[str] = mapped_column(Text, default="")
    fault_desc: Mapped[str] = mapped_column(Text, default="")
    photos: Mapped[list] = mapped_column(JSON, default=list)
    # 承诺时效（默认 15 天）：倒计时 + 超时进待处理
    promise_days: Mapped[int] = mapped_column(Integer, default=REPAIR_TIMEOUT_DAYS)
    promise_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 垫付金额（钱垫着、货在别人手里，是这里最需要被看见的数）
    paid_amount: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default=REPAIR_OPEN)
    timeout_flag: Mapped[bool] = mapped_column(Boolean, default=False)
    outbound_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    waybill_no: Mapped[str] = mapped_column(String(64), default="")
    result_note: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsPartsItem(Base):
    """拆机件残值明细：D 档强制逐件估值，**无明细不允许关闭工单**。"""

    __tablename__ = "wms_parts_items"
    __table_args__ = (Index("ix_wms_parts_item", "item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode: Mapped[str] = mapped_column(String(24), default="")
    category: Mapped[str] = mapped_column(String(32), default=CATEGORY_PHONE)
    part_type: Mapped[str] = mapped_column(String(16), default=PART_MAINBOARD)
    part_name: Mapped[str] = mapped_column(String(128), default="")
    est_value: Mapped[float] = mapped_column(Float, default=0.0)
    actual_sold_price: Mapped[float] = mapped_column(Float, default=0.0)
    outbound_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sold_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsStocktake(Base):
    """盘点单：循环 / 批次 / 全盘 / 专项四类，差异需审批，差异率超阈值自动升级。"""

    __tablename__ = "wms_stocktakes"
    __table_args__ = (UniqueConstraint("take_no", name="uq_wms_stocktake_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    take_no: Mapped[str] = mapped_column(String(32), default="")
    scope: Mapped[str] = mapped_column(String(16), default=STOCKTAKE_CYCLE)
    warehouse_code: Mapped[str] = mapped_column(String(16), default="")
    grade: Mapped[str] = mapped_column(String(2), default="")
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    plan_qty: Mapped[int] = mapped_column(Integer, default=0)
    actual_qty: Mapped[int] = mapped_column(Integer, default=0)
    diff_qty: Mapped[int] = mapped_column(Integer, default=0)
    diff_rate: Mapped[float] = mapped_column(Float, default=0.0)
    # 差异金额（按成本估）：超阈值自动升级主管
    diff_amount: Mapped[float] = mapped_column(Float, default=0.0)
    need_upgrade: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=STOCKTAKE_DRAFT)
    operator: Mapped[str] = mapped_column(String(64), default="")
    approver: Mapped[str] = mapped_column(String(64), default="")
    diff_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsStocktakeLine(Base):
    """盘点明细：扫一个记一个，**未扫到的不自动算盘亏**（防误判）。"""

    __tablename__ = "wms_stocktake_lines"
    __table_args__ = (
        Index("ix_wms_stocktake_line", "take_id"),
        UniqueConstraint("take_id", "barcode", name="uq_wms_stocktake_line"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    take_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode: Mapped[str] = mapped_column(String(24), default="")
    grade: Mapped[str] = mapped_column(String(2), default="")
    status: Mapped[str] = mapped_column(String(16), default="")
    # 账面应该在哪、实盘在哪
    book_warehouse: Mapped[str] = mapped_column(String(16), default="")
    found_warehouse: Mapped[str] = mapped_column(String(16), default="")
    found: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    scanned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsLabelPrint(Base):
    """标签打印 / 补打记录：**补打必须填原因**，同批补打率超阈值要告警。"""

    __tablename__ = "wms_label_prints"
    __table_args__ = (Index("ix_wms_label_item", "item_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    item_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    barcode: Mapped[str] = mapped_column(String(24), default="")
    display_code: Mapped[str] = mapped_column(String(32), default="")
    # 份数（2026-10-10）：**跟货品数量走** —— 这台货是 3 台就打 3 张一模一样的标签
    # （一个条码底下 3 台，每台身上贴一张）。数量 = 1 时退回打印机档案上的默认份数。
    copies: Mapped[int] = mapped_column(Integer, default=1)
    # W（占位）/ formal（正式）/ reprint（补打）
    label_type: Mapped[str] = mapped_column(String(16), default=LABEL_W)
    grade: Mapped[str] = mapped_column(String(2), default=GRADE_W)
    action_text: Mapped[str] = mapped_column(String(64), default="")
    # 指令集：TSPL / ZPL（适配器换机型只改指令）
    dialect: Mapped[str] = mapped_column(String(8), default="TSPL")
    command: Mapped[str] = mapped_column(Text, default="")
    transport: Mapped[str] = mapped_column(String(16), default="NET9100")
    printer: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="queued")
    operator: Mapped[str] = mapped_column(String(64), default="")
    # 补打原因（补打必填）
    reason: Mapped[str] = mapped_column(Text, default="")
    batch_no: Mapped[str] = mapped_column(String(32), default="")
    # 云打印（2026-10-08）：下发到芯烨条码云之后的回执与痕迹
    order_no: Mapped[str] = mapped_column(String(64), default="")
    printer_sn: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    printed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsPrinter(Base):
    """云打印机档案（2026-10-08）：机身编号 + 这台机器的纸张与打印参数。

    ``sn`` 是机身顶部标签上的编号 —— 云打印全靠它认机器，现场换机器只要改这里，
    业务代码与模板都不用动。``is_default`` 的那台是「入队即打」的落脚点。
    """

    __tablename__ = "wms_printers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sn: Mapped[str] = mapped_column(String(64), default="", unique=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    # label 标签纸（间隙纸）/ receipt 小票（连续纸，间隙设 0）
    kind: Mapped[str] = mapped_column(String(16), default="label")
    width_mm: Mapped[int] = mapped_column(Integer, default=60)
    height_mm: Mapped[int] = mapped_column(Integer, default=40)
    gap_mm: Mapped[int] = mapped_column(Integer, default=2)
    copies: Mapped[int] = mapped_column(Integer, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsPrintConfig(Base):
    """打印配置（key / value）：云账号、接口地址、标签模板、下发行为。

    为什么不用配置文件：现场换账号、换模板都得**在页面上改**，改完立刻生效，
    不该为了改一个密钥去重启服务（2026-10-08）。
    """

    __tablename__ = "wms_print_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(48), default="", unique=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class WmsCloudPrintEvent(Base):
    """云打印回调留痕（2026-10-09）：芯烨云推过来的每一条都**原样存一份**。

    为什么连认不出的报文也要存：回调里「订单号 / 机器号 / 状态」各家写法不统一
    （orderId、orderNo、taskId、sn、printerSn、state、printState…），先照单全收，
    对不上本地任务也不丢 —— 回头照真实报文补一条键名就能解析，不用等下一次打印。
    """

    __tablename__ = "wms_cloud_print_events"
    __table_args__ = (Index("ix_wms_cloud_evt_order", "order_no"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # order 打印结果 / printer 机器状态 / unknown 没认出来
    event_type: Mapped[str] = mapped_column(String(16), default="unknown")
    order_no: Mapped[str] = mapped_column(String(64), default="")
    printer_sn: Mapped[str] = mapped_column(String(64), default="")
    # 归一化后的结果：printed / failed / queued / unknown
    state: Mapped[str] = mapped_column(String(16), default="unknown")
    raw_state: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    payload: Mapped[str] = mapped_column(Text, default="")  # 原始报文（JSON 字符串）
    # 对上的本地打印任务（没对上就是 None，报文照样留着）
    matched_print_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applied: Mapped[bool] = mapped_column(Boolean, default=False)
    source_ip: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsAuditLog(Base):
    """审计日志：改档 / 改价 / 盘盈盘亏 / 报修撤单 / 危废处理，全部留痕。"""

    __tablename__ = "wms_audit_logs"
    __table_args__ = (Index("ix_wms_audit_entity", "entity", "entity_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), default="")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    action: Mapped[str] = mapped_column(String(64), default="")
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")
    operator: Mapped[str] = mapped_column(String(64), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class WmsSyncLog(Base):
    """中台同步：幂等键 + 重试计数，连续失败进死信（status=dead）并告警。"""

    __tablename__ = "wms_sync_logs"
    __table_args__ = (
        UniqueConstraint("idempotent_key", name="uq_wms_sync_idem"),
        Index("ix_wms_sync_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity: Mapped[str] = mapped_column(String(32), default="item")
    entity_id: Mapped[str] = mapped_column(String(64), default="")
    target: Mapped[str] = mapped_column(String(32), default="hub")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    idempotent_key: Mapped[str] = mapped_column(String(128), default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ================================================================================
# 检测仓自己的权限模型（**不属于渔枢系统的角色权限体系**）
#
# 平台账号（USERS）只是「登录身份」，检测仓的授权在这里独立记账：
#   WmsRole        角色（仓库主管 / 检测员 / 仓管 / 财务 / 只读）
#   WmsRolePerm    角色 -> 权限码
#   WmsUserRole    平台账号 -> 角色
# 平台管理员天然拥有全部权限，**不需要在这里配**；除此之外没有角色 = 没有任何权限。
# ================================================================================
class WmsRole(Base):
    """检测仓角色。``builtin`` 的行是系统内置（删除也删不掉的那几个常用岗）。"""

    __tablename__ = "wms_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), default="")
    name: Mapped[str] = mapped_column(String(40), default="")
    remark: Mapped[str] = mapped_column(String(200), default="")
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("code", name="uq_wms_role_code"),)


class WmsRolePerm(Base):
    """角色拥有的权限码。"""

    __tablename__ = "wms_role_perms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_id: Mapped[int] = mapped_column(Integer, default=0)
    perm: Mapped[str] = mapped_column(String(60), default="")
    __table_args__ = (
        UniqueConstraint("role_id", "perm", name="uq_wms_role_perm"),
        Index("ix_wms_role_perm_role", "role_id"),
    )


class WmsUserRole(Base):
    """平台账号在检测仓的角色（一个人可以有多个角色，权限取并集）。"""

    __tablename__ = "wms_user_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 平台 USERS 表的主键：互通靠它，检测仓不维护人员档案
    user_id: Mapped[int] = mapped_column(Integer, default=0)
    # 冗余一份账号名 / 姓名，列表页不用回头查平台库
    username: Mapped[str] = mapped_column(String(64), default="")
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role_id: Mapped[int] = mapped_column(Integer, default=0)
    granted_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (
        UniqueConstraint("user_id", "role_id", name="uq_wms_user_role"),
        Index("ix_wms_user_role_user", "user_id"),
    )
