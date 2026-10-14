"""渔集 v1.0.32：红线先修不先废 + 报修的货不许进可售仓 / 不许被渔枢拿去上架。

用户 2026-10-13 三条拍板：

① **安全红线先走报修（C）**，不再直接判 D 危废 —— 电池鼓包 / 异味 / 漏电这些
   先送去检修，修得好照样卖；只有「完全不能用了、很严重的功能故障和缺件」才判 D。
   （完全不能用 = 3 项及以上功能没过；缺件 = 人工勾「缺件严重 / 无维修价值」）

② **分级规则体检**：删掉与 priority 25 那条完全同档同结果的「B 小修」重复规则；
   给「必须官方修」补上排除条件（勾了官方修又勾缺件严重的，该判 D 不是 C）。

③ **报修 / 报废的货不许进可售卖仓**，渔枢系统建档上架扫条码时也要拦 ——
   渔枢上架商品的条码必须是「可售档 + 可售状态 + 在可售仓」的货。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tests  # noqa: F401  先加载包，确保测试库配置在任何 app 模块之前生效
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import SessionLocal as AppSession, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.web.auth import COOKIE_NAME, ensure_default_admin, make_session  # noqa: E402
from wms import grading, inspection, intake, seed as wms_seed, state  # noqa: E402
from wms import permissions as wms_permissions  # noqa: E402
from wms.db import init_tables as wms_init_tables  # noqa: E402
from wms.models import (  # noqa: E402
    GRADE_C,
    GRADE_D,
    TEST_FAIL,
    TEST_PASS,
    WmsBatch,
    WmsGradeRule,
    WmsItem,
    WmsPriceRule,
    WmsStockBalance,
    WmsStockLedger,
    WmsTestItem,
    WmsTestRecord,
)

