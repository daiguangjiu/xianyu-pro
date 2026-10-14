"""检测仓自己的配置。

刻意**不读渔枢系统的 ``app.config.settings``** —— 两边只是凑巧住在同一台机器、同一张库里，
配置项把默认值取得一样是为了好部署，不是为了让子系统跟着渔枢系统的开关动。

单独部署时：
    WMS_DATABASE_URL=postgresql://…  python -m wms.main
不设 ``WMS_DATABASE_URL`` 就用 ``DATABASE_URL``（= 与平台同库，数据互通）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 包根目录：<项目>/wms
BASE_DIR = Path(__file__).resolve().parent


def _first(*names: str, default: str = "") -> str:
    for name in names:
        value = str(os.getenv(name) or "").strip()
        if value:
            return value
    return default


@dataclass(frozen=True)
class WmsConfig:
    # ---------------------------------------------------------------- 数据与文件
    database_url: str = _first(
        "WMS_DATABASE_URL", "DATABASE_URL", default="sqlite:///data/wms.db"
    )
    media_dir: str = _first("WMS_MEDIA_DIR", "MEDIA_DIR", default="data/media")

    # ---------------------------------------------------------------- 门面
    # 2026-10-04 用户拍板：系统名升级为「渔集」，检测仓只是它下面的一个子系统，
    # 原来那个「检测入库出库管理系统」降成左上角 logo 下面的副标题。
    brand: str = _first("WMS_BRAND", default="渔集")
    system_name: str = _first("WMS_SYSTEM_NAME", default="检测入库出库管理系统")
    # 页面静态资源版本（改了改 aconcss/js 就 +1，逼浏览器换缓存）
    asset_v: str = _first("WMS_ASSET_V", default="1")

    # ---------------------------------------------------------------- 业务开关
    # 超过它天没回来的报修单自动打「超时」标（修复后必须重新检测定级）
    repair_timeout_days: int = int(_first("WMS_REPAIR_TIMEOUT_DAYS", default="15") or 15)
    # 盘点差异升级阈值：比例或金额任一超了，必须主管审批才核销
    stocktake_diff_ratio: float = float(
        _first("WMS_STOCKTAKE_DIFF_RATIO", default="0.005") or 0.005
    )
    stocktake_diff_amount: float = float(
        _first("WMS_STOCKTAKE_DIFF_AMOUNT", default="500") or 500
    )

    @property
    def media_path(self) -> Path:
        path = Path(self.media_dir)
        return path if path.is_absolute() else (BASE_DIR.parent / path).resolve()


config = WmsConfig()
