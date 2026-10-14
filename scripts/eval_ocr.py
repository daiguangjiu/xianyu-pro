"""OCR ROI 专项识别的 A/B 评估：拿订单表里运营确认过的值当标准答案。

做法：
  · 「整图」一列 = 库里已存的 ocr_text 解析出来的值（不用重跑整图 OCR，省时间）；
  · 「ROI」一列  = 同一份文本跑过 ocr_roi.augment 之后解析出来的值；
  · 「真值」     = 订单表里的收货人 / 金额（群里手打或运营核对过的）。

用法（容器内）：
    docker exec <app容器> python scripts/eval_ocr.py [样本数]

输出每张图的三者对照与总命中数，用来判断 ROI 到底纠正了什么、有没有把对的改错。
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, "/app")

from sqlalchemy import create_engine, text as sql_text  # noqa: E402

from app.config import settings  # noqa: E402
from app.parser import ocr_roi  # noqa: E402


def _money_of(values: dict[str, float]) -> float | None:
    """文本里的主要金额：成交价优先，其次商品总价（运费不算）。"""
    for key in ("成交价", "实付款", "商品总价", "售价", "合计"):
        if key in values:
            return values[key]
    return None


def _name_hit(truth: object, got: object) -> bool:
    """姓名是否对上。真值可能是脱敏写法（「王*萌」），星号位不比较。"""
    t, g = str(truth or "").strip(), str(got or "").strip()
    if not t or not g:
        return False
    if "*" in t:
        return len(t) == len(g) and all(a == "*" or a == b for a, b in zip(t, g))
    return t == g


def _money_hit(truth: object, got: object) -> bool:
    try:
        return got is not None and abs(float(truth) - float(got)) < 0.01
    except (TypeError, ValueError):
        return False


def main() -> int:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        rows = conn.execute(
            sql_text(
                """
                select oi.local_path, oi.ocr_text, o.receiver_name,
                       coalesce(o.sale_amount, o.deal_amount) as amount
                from order_images oi
                join orders o on o.id = oi.order_id
                where oi.local_path <> '' and oi.ocr_text <> ''
                  and o.receiver_name is not null and o.receiver_name <> ''
                order by oi.id desc
                limit :n
                """
            ),
            {"n": limit},
        ).fetchall()

    print(f"样本 {len(rows)} 张（真值 = 订单表里运营确认过的收货人 / 金额）\n" + "=" * 118)
    print(
        f"{'图片':<26}{'真值姓名':<10}{'整图':<10}{'ROI':<10}{'姓名':<8}"
        f"{'真值金额':>9}{'整图':>9}{'ROI':>9}{'金额':<8}{'耗时':>7}"
    )
    print("-" * 118)
    stat = {"n": 0, "name_plain": 0, "name_roi": 0, "money_plain": 0, "money_roi": 0}
    total_cost = 0.0
    for path, ocr_text, truth_name, truth_amount in rows:
        if not Path(path).exists():
            print(f"{Path(path).name[:25]:<26}（图片文件已不在，跳过）")
            continue
        stat["n"] += 1
        plain_name = ocr_roi.name_from_text(ocr_text)
        plain_money = _money_of(ocr_roi.money_from_text(ocr_text))
        t0 = time.time()
        new_text, notes = ocr_roi.augment(path, ocr_text)
        total_cost += time.time() - t0
        roi_name = ocr_roi.name_from_text(new_text)
        roi_money = _money_of(ocr_roi.money_from_text(new_text))
        # 整图对了就不算 ROI 的功劳；整图错了、ROI 对上了才算纠正
        if _name_hit(truth_name, plain_name):
            stat["name_plain"] += 1
            name_verdict = "整图✓"
        elif _name_hit(truth_name, roi_name):
            stat["name_roi"] += 1
            name_verdict = "ROI✓"
        else:
            name_verdict = "都✗"
        if _money_hit(truth_amount, plain_money):
            stat["money_plain"] += 1
            money_verdict = "整图✓"
        elif _money_hit(truth_amount, roi_money):
            stat["money_roi"] += 1
            money_verdict = "ROI✓"
        else:
            money_verdict = "都✗"
        print(
            f"{Path(path).name[:25]:<26}{str(truth_name)[:9]:<10}{plain_name[:9] or '-':<10}"
            f"{roi_name[:9] or '-':<10}{name_verdict:<8}"
            f"{float(truth_amount or 0):>9.2f}{plain_money if plain_money is not None else 0:>9.2f}"
            f"{roi_money if roi_money is not None else 0:>9.2f}{money_verdict:<8}"
            f"{time.time() - t0:>6.1f}s"
        )
        for note in notes:
            print(f"{'':<26}! {note}")
    print("-" * 118)
    n = max(stat["n"], 1)
    print(
        f"姓名：整图对 {stat['name_plain']}/{n}，ROI 额外纠正 {stat['name_roi']} 张；"
        f"金额：整图对 {stat['money_plain']}/{n}，ROI 额外纠正 {stat['money_roi']} 张；"
        f"ROI 总耗时 {total_cost:.1f}s（每张 {total_cost / n:.1f}s）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
