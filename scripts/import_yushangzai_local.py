#!/usr/bin/env python3
"""渔货栈图包 → 渔枢自营商品：本地批量导入脚本（2026-10-15 数据需求）。

在 Mac 上跑（图片在移动硬盘上，不要把 500MB 图包传到服务器再导）：

    .venv/bin/python scripts/import_yushangzai_local.py \
        --base https://62.234.85.96 \
        --username admin --password '***' \
        [--dir /Volumes/ORICO/闲鱼商品主图] \
        [--only YS202610080003,YS202610080199] [--dry-run] [--no-skip-existing]

目录约定（可多级嵌套，脚本自动往下找）：``<根目录>/<渔货号（可带中文备注）>/组目录/*.jpg``，
目录名里的 YS+数字 提取为渔货号；一组 = 商品图片，二三四组 = 图文详情图片。
组目录名认这几种写法：一组 / 1组 / 组一 / 两组 / 2组 …（1~4，中文数字或阿拉伯数字）。
**默认跳过线上已导入的渔货号**（按 /products?q= 查得到就跳），要强制重导加 --no-skip-existing
（重导是幂等覆盖，不会攒垃圾）。
服务端接口：``POST {base}/products/import-yushangzai``（fish_code + g1~g4 多文件）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FISH_RE = re.compile(r"YS\d{8,12}", re.IGNORECASE)
CN_NUM = {"一": "1", "两": "2", "二": "2", "三": "3", "四": "4",
          "1": "1", "2": "2", "3": "3", "4": "4"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_BYTES = 15 * 1024 * 1024
MAX_DEPTH = 4   # 目录最多往下钻几层（根/批次/商品/组目录 这种结构富余）


def fish_code_of(name: str) -> str:
    m = FISH_RE.search(name)
    return m.group(0).upper() if m else ""


def group_no_of(name: str) -> str:
    """「一组 / 1组 / 组一 / 两组 / 组三 …」→ '1'~'4'，不认得的返回空。"""
    n = name.strip()
    m = re.fullmatch(r"([一二两三四1-4])组", n) or re.fullmatch(r"组([一二两三四1-4])", n)
    return CN_NUM.get(m.group(1), "") if m else ""


def _images_of(sub: Path) -> list[Path]:
    return [p for p in sorted(sub.iterdir())
            if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES]


def collect_groups(item_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for sub in sorted(item_dir.iterdir()):
        if not sub.is_dir():
            continue
        gi = group_no_of(sub.name)
        if not gi:
            continue
        files = _images_of(sub)
        if files:
            groups[gi] = files
    return groups


def find_product_dirs(root: Path, depth: int = 0,
                      found: dict[str, tuple[Path, dict]] | None = None
                      ) -> dict[str, tuple[Path, dict]]:
    """递归找「商品目录」＝目录名带渔货号 且 里面有组子目录。同号重复取第一处。"""
    if found is None:
        found = {}
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        code = fish_code_of(d.name)
        if code and code not in found:
            groups = collect_groups(d)
            if groups.get("1"):
                found[code] = (d, groups)
                continue
        if depth < MAX_DEPTH:
            find_product_dirs(d, depth + 1, found)
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="https://62.234.85.96")
    ap.add_argument("--dir", default="/Volumes/ORICO/闲鱼商品主图")
    ap.add_argument("--username", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--cookie", default="", help="已有 oh_session 值（免登录）")
    ap.add_argument("--only", default="", help="只导这些渔货号，逗号分隔")
    ap.add_argument("--no-skip-existing", action="store_true",
                    help="不跳过已导入的渔货号（幂等覆盖重导）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    import httpx

    root = Path(args.dir)
    if not root.is_dir():
        print(f"目录不存在：{root}")
        return 1
    only = {c.strip().upper() for c in args.only.split(",") if c.strip()}

    found = find_product_dirs(root)
    items = []
    for code, (d, groups) in found.items():
        if only and code not in only:
            continue
        items.append((code, d, groups))
    if not items:
        print("没有可导入的款式（目录名里要带渔货号 YS+数字，且有组子目录装图）")
        return 1
    items.sort(key=lambda x: x[0])
    print(f"待导入 {len(items)} 款：{', '.join(c for c, _, _ in items)}")
    if args.dry_run:
        for code, d, groups in items:
            counts = " / ".join(f"{g}组×{len(fs)}" for g, fs in sorted(groups.items()))
            print(f"  {code}  {d.name}  {counts}")
        return 0

    verify = not re.search(r"//\d+\.\d+\.\d+\.\d+", args.base)
    client = httpx.Client(base_url=args.base, verify=verify, timeout=600,
                          follow_redirects=True)
    if args.cookie:
        client.cookies.set("oh_session", args.cookie)
    elif args.username and args.password:
        resp = client.post("/login", data={"username": args.username,
                                           "password": args.password})
        if resp.status_code not in (200, 303) or not client.cookies.get("oh_session"):
            print(f"登录失败：HTTP {resp.status_code}")
            return 1
        print("登录成功")
    else:
        print("要提供 --cookie 或 --username/--password 才能登录")
        return 1

    # 已导入的先跳过（2026-10-15 用户：已经导入的可以忽略）——
    # /products?q=渔货号 的结果行里有详情链接 href="/products/<渔货号>"；
    # 注意不能拿「渔货号出现在页面里」判断 —— 搜索框会把 q 原样回显在 value 里，必误判
    existing: set[str] = set()
    if not args.no_skip_existing:
        for code, _, _ in items:
            try:
                page = client.get("/products", params={"q": code})
                if page.status_code == 200 and f'/products/{code}"' in page.text:
                    existing.add(code)
            except Exception as exc:  # noqa: BLE001 查不动就不跳（宁重导不漏导）
                print(f"[提示] 查「{code}」是否已导入失败：{exc}")
        if existing:
            print(f"已在线上、跳过 {len(existing)} 款：{', '.join(sorted(existing))}")

    ok = fail = skipped = 0
    for code, d, groups in items:
        if code in existing:
            skipped += 1
            continue
        files = []
        for gi, paths in groups.items():
            for p in paths:
                if p.stat().st_size > MAX_BYTES:
                    print(f"[警告] {p} 超过 15MB，跳过该张")
                    continue
                files.append(("g" + gi, (p.name, p.read_bytes(), "image/jpeg")))
        try:
            resp = client.post("/products/import-yushangzai",
                               data={"fish_code": code}, files=files)
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] {code}：{exc}")
            fail += 1
            continue
        if resp.status_code == 200 and payload.get("ok"):
            ok += 1
            print(f"[成功] {code} {payload.get('name')} · {payload.get('category')} · "
                  f"渔货价 {payload.get('fish_price')} · 建议 {payload.get('price')} · "
                  f"库存 {payload.get('stock')} · 商品图 {payload.get('main_images')} · "
                  f"详情图 {payload.get('detail_images')} · 新建={payload.get('created')}")
        else:
            fail += 1
            print(f"[失败] {code}：HTTP {resp.status_code} {payload.get('error')}")
    print(f"完成：成功 {ok} / 跳过已导入 {skipped} / 失败 {fail}")
    return 0 if fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
