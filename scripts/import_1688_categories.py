#!/usr/bin/env python
"""把 1688 的类目树导进渔枢系统「货源 · 类目管理」（product_categories 表）。

数据来源：scripts/data/1688-category-tree.csv（1688 开放平台拉下来的全量类目树，
一行一个节点，带 level1/2/3 的 id 与名字）。本脚本只取**三级**（level ≤ 3）那一层，
与类目管理页的三级结构对齐；四级 / 五级是类目属性，不进类目表。

怎么用（在仓库根目录）：
    .venv/bin/python scripts/import_1688_categories.py              # 预演：只打印会怎么变
    .venv/bin/python scripts/import_1688_categories.py --apply      # 真的写库
    .venv/bin/python scripts/import_1688_categories.py --apply --keep-old   # 老类目不禁用

规则（幂等，重复跑只补齐不重复建）：
  · 认领顺序：先按 source_id（1688 类目 id），再按「同一父级 + 同名」，都没有才新建；
  · **只补齐、不删除**：运营自己加的类目（source_id 为空）原样保留；
  · 老的那套类目（渔货栈常量播种的 10 个一级，source_id 形如 apparel / ap_women）
    默认**停用但保留数据** —— 历史商品的「家用电器 / 手机配件」这类文本还能解析，
    只是不再出现在发布页的级联里（--keep-old 可以保留启用）；
  · **「其他」兜底**：一级「其他」→ 二级「其他」→ 三级「其他」，始终启用。
    上架时类目认不到、或运营临时没想好放哪儿，就落这里（不静默塞进某个真类目）；
  · sort_order 按 CSV 里的出现顺序（1688 自己的排序），后来运营在页面上调过的不动。
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DEFAULT_CSV = ROOT / "scripts" / "data" / "1688-category-tree.csv"
SOURCE_PREFIX = "1688-"
OTHER_KEY = "hub_other"


# --------------------------------------------------------------------- 读 CSV
def read_tree(csv_path: Path) -> list[dict]:
    """CSV → 三级树：按 (一级 id, 二级 id) 组织，三级按 (二级, 三级) 组合去重。

    同一个三级类目可能挂在多个二级下（1688 里有 21 个这种），两边都要建，
    不然从另一条路径选类目时会选不到。
    """
    tops: "OrderedDict[str, dict]" = OrderedDict()
    with csv_path.open(encoding="utf-8-sig") as fp:
        for row in csv.DictReader(fp):
            l1_id = (row.get("level1_id") or "").strip()
            l2_id = (row.get("level2_id") or "").strip()
            l3_id = (row.get("level3_id") or "").strip()
            if not l1_id:
                continue
            top = tops.setdefault(
                l1_id,
                {
                    "id": l1_id,
                    "name": (row.get("level1_name") or "").strip(),
                    "children": OrderedDict(),
                },
            )
            if not top["name"]:
                top["name"] = (row.get("level1_name") or "").strip()
            if not l2_id:
                continue
            sub = top["children"].setdefault(
                l2_id,
                {
                    "id": l2_id,
                    "name": (row.get("level2_name") or "").strip(),
                    "children": OrderedDict(),
                },
            )
            if not sub["name"]:
                sub["name"] = (row.get("level2_name") or "").strip()
            if not l3_id:
                continue
            sub["children"].setdefault(l3_id, {"id": l3_id, "name": (row.get("level3_name") or "").strip()})
    return [
        {
            "id": t["id"],
            "name": t["name"],
            "children": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "children": list(s["children"].values()),
                }
                for s in t["children"].values()
            ],
        }
        for t in tops.values()
        if t["name"]
    ]


# --------------------------------------------------------------------- 写库
def _node_id(row) -> str:
    return f"{SOURCE_PREFIX}{row['id']}"


def apply_tree(session, tree: list[dict], *, retire_old: bool) -> dict:
    """把树写进库：只补不删，老类目（非 1688 来源）默认停用。"""
    from app.models import ProductCategory
    from sqlalchemy import select, func

    existing = list(session.scalars(select(ProductCategory)))
    by_source = {n.source_id: n for n in existing if n.source_id}
    by_parent_name: dict[tuple, object] = {}
    for n in existing:
        by_parent_name.setdefault((n.parent_id, n.name), n)

    added = updated = 0
    pending: list[ProductCategory] = []

    def flush() -> None:
        nonlocal pending
        if not pending:
            return
        session.add_all(pending)
        session.flush()
        pending = []

    for top_order, top in enumerate(tree):
        if not top["name"]:
            continue
        source_id = _node_id(top)
        node = by_source.get(source_id) or by_parent_name.get((None, top["name"]))
        if node is None:
            node = ProductCategory(
                parent_id=None, level=1, name=top["name"], sort_order=top_order,
                source_id=source_id, enabled=True,
            )
            session.add(node)
            session.flush()
            by_source[source_id] = node
            by_parent_name[(None, top["name"])] = node
            added += 1
        else:
            changed = False
            if node.source_id != source_id:
                node.source_id = source_id
                changed = True
            if node.sort_order != top_order:
                node.sort_order = top_order
                changed = True
            if not node.enabled:  # 1688 树里有的类目，重新启用（可能上次被停过）
                node.enabled = True
                changed = True
            updated += 1 if changed else 0

        for sub_order, sub in enumerate(top["children"]):
            if not sub["name"]:
                continue
            sub_source = _node_id(sub)
            sub_node = by_source.get(sub_source) or by_parent_name.get((node.id, sub["name"]))
            if sub_node is None:
                sub_node = ProductCategory(
                    parent_id=node.id, level=2, name=sub["name"], sort_order=sub_order,
                    source_id=sub_source, enabled=True,
                )
                session.add(sub_node)
                session.flush()
                by_source[sub_source] = sub_node
                by_parent_name[(node.id, sub["name"])] = sub_node
                added += 1
            else:
                changed = False
                if sub_node.source_id != sub_source:
                    sub_node.source_id = sub_source
                    changed = True
                if sub_node.parent_id != node.id:
                    sub_node.parent_id = node.id
                    changed = True
                if sub_node.sort_order != sub_order:
                    sub_node.sort_order = sub_order
                    changed = True
                if not sub_node.enabled:
                    sub_node.enabled = True
                    changed = True
                updated += 1 if changed else 0

            have = set(
                session.scalars(
                    select(ProductCategory.name).where(ProductCategory.parent_id == sub_node.id)
                )
            )
            order = len(have)
            for leaf in sub["children"]:
                if not leaf["name"] or leaf["name"] in have:
                    continue
                pending.append(
                    ProductCategory(
                        parent_id=sub_node.id, level=3, name=leaf["name"],
                        sort_order=order,
                        # 三级 source_id 沿用「二级_l3_N」：渔货栈按它反推二级类目 id
                        source_id=f"{sub_source}_l3_{order}",
                        enabled=True,
                    )
                )
                have.add(leaf["name"])
                order += 1
                added += 1
                if len(pending) >= 500:
                    flush()
    flush()

    # —— 「其他」兜底：一级 / 二级 / 三级各一个，始终启用 ——
    other = by_source.get(OTHER_KEY)
    if other is None:
        other = ProductCategory(
            parent_id=None, level=1, name="其他", sort_order=9999,
            source_id=OTHER_KEY, enabled=True,
        )
        session.add(other)
        session.flush()
        by_source[OTHER_KEY] = other
        added += 1
    other_sub = next(
        (n for n in _children_of(session, other.id) if n.name == "其他"), None
    )
    if other_sub is None:
        other_sub = ProductCategory(
            parent_id=other.id, level=2, name="其他", sort_order=0,
            source_id=f"{OTHER_KEY}_sub", enabled=True,
        )
        session.add(other_sub)
        session.flush()
        added += 1
    if not any(n.name == "其他" for n in _children_of(session, other_sub.id)):
        session.add(
            ProductCategory(
                parent_id=other_sub.id, level=3, name="其他", sort_order=0,
                source_id=f"{OTHER_KEY}_sub_l3_0", enabled=True,
            )
        )
        added += 1
    other.enabled = True

    retired = 0
    if retire_old:
        # 老类目（渔货栈常量播种的 10 个一级）停用：一级停用即整棵子树不出现在发布页，
        # 数据保留 —— 历史商品的「家用电器 / 手机配件」这类文本还能解析出名字。
        for node in session.scalars(select(ProductCategory).where(ProductCategory.level == 1)):
            if node.source_id and not node.source_id.startswith(SOURCE_PREFIX) and node.source_id != OTHER_KEY:
                if node.enabled:
                    node.enabled = False
                    retired += 1

    session.commit()
    total = session.scalar(select(func.count()).select_from(ProductCategory))
    return {"added": added, "updated": updated, "retired": retired, "total": total}


def _children_of(session, parent_id: int) -> list:
    from app.models import ProductCategory
    from sqlalchemy import select

    return list(
        session.scalars(select(ProductCategory).where(ProductCategory.parent_id == parent_id))
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="导入 1688 类目树到渔枢系统类目管理")
    parser.add_argument("--csv", default=str(DEFAULT_CSV), help="类目 CSV 路径")
    parser.add_argument("--apply", action="store_true", help="真的写库（默认只预演）")
    parser.add_argument("--keep-old", action="store_true", help="老类目保持启用（默认停用）")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"找不到类目文件：{csv_path}")
        raise SystemExit(1)

    tree = read_tree(csv_path)
    n1 = len(tree)
    n2 = sum(len(t["children"]) for t in tree)
    n3 = sum(len(s["children"]) for t in tree for s in t["children"])
    print(f"读到类目树：一级 {n1} 个 / 二级 {n2} 个 / 三级 {n3} 个")
    print(f"  前 5 个一级：{'、'.join(t['name'] for t in tree[:5])}")

    from app.db import init_db, SessionLocal

    init_db()
    with SessionLocal() as session:
        if not args.apply:
            print("预演模式（加 --apply 才写库）。当前库里已有的节点会按名字 / source_id 复用。")
            return
        result = apply_tree(session, tree, retire_old=not args.keep_old)
    print(
        "写入完成：新增 {added} 个 / 更新 {updated} 个 / 停用老类目 {retired} 个，"
        "类目表共 {total} 个节点".format(**result)
    )
    print("提示：渔货栈最多 60 秒后读到新类目；「其他」作为兜底类目始终启用。")


if __name__ == "__main__":
    main()
