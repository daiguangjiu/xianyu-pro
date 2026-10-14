"""生成「汉字 → 拼音首字母」离线字表（app/services/pinyin.py）。

商品编码要用「一二三级类目词简拼首字母」，但生产容器不装 pypinyin，
所以这里一次性把 GB2312 常用汉字（6763 字）的首字母固化进代码，运行时零依赖。

用法（开发机，需要 pypinyin）：
    pip install pypinyin
    python scripts/gen_pinyin_table.py            # 覆盖 app/services/pinyin.py
    python scripts/gen_pinyin_table.py --check    # 只校验现有字表是否覆盖当前类目用字
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET = ROOT / "app" / "services" / "pinyin.py"
PER_LINE = 24


def build_pairs() -> list[tuple[str, str]]:
    from pypinyin import Style, lazy_pinyin

    pairs: list[tuple[str, str]] = []
    for hi in range(0xB0, 0xF8):  # GB2312 一级 + 二级汉字区
        for lo in range(0xA1, 0xFF):
            try:
                char = bytes([hi, lo]).decode("gb2312")
            except Exception:  # noqa: BLE001  空的区位码
                continue
            letter = (lazy_pinyin(char, style=Style.FIRST_LETTER, errors=lambda _: [""]) or [""])[0]
            letter = (letter or "").upper()
            if letter.isalpha():
                pairs.append((char, letter))
    return pairs


def render(pairs: list[tuple[str, str]]) -> str:
    rows = []
    for index in range(0, len(pairs), PER_LINE):
        chunk = pairs[index:index + PER_LINE]
        rows.append("    " + " ".join(f'"{c}": "{l}",' for c, l in chunk))
    body = "\n".join(rows)
    return f'''"""汉字 → 拼音首字母（离线内置表，用于生成商品编码里的类目简拼）。

数据源：GB2312 常用汉字（6763 字），用 pypinyin 一次性生成后固化在这里，
运行时**不依赖 pypinyin**（生产容器不装这个包）。

生成方式（如需扩充字表，开发机执行）：
    pip install pypinyin
    python scripts/gen_pinyin_table.py
"""
from __future__ import annotations

INITIALS: dict[str, str] = {{
{body}
}}


def initials_of(text: str, *, keep_alnum: bool = False) -> str:
    """取字符串里汉字的拼音首字母。

    - 汉字 → 大写首字母（字表里没有的字跳过）；
    - ``keep_alnum=True`` 时字母数字原样保留（大写），其余字符（标点 / 空格 / 斜杠）一律忽略。
    """
    out: list[str] = []
    for ch in text or "":
        letter = INITIALS.get(ch)
        if letter:
            out.append(letter)
        elif keep_alnum and ch.isalnum() and ch.isascii():
            out.append(ch.upper())
    return "".join(out)
'''


def check_coverage() -> int:
    """校验：类目树里出现的汉字是否都在字表里（缺字会让编码少一段）。"""
    from app.services.pinyin import INITIALS

    from src.goods.catalog import CATEGORY_TREE, SOURCING_CATEGORY_TREE, SOURCING_SUB_LEAVES

    names: list[str] = []
    for top in list(SOURCING_CATEGORY_TREE) + list(CATEGORY_TREE):
        names.append(str(top.get("name") or ""))
        for sub in top.get("children") or []:
            names.append(str(sub.get("name") or ""))
            names.extend(str(n) for n in (SOURCING_SUB_LEAVES.get(sub.get("id")) or ()))
    missing = sorted({ch for name in names for ch in name if "\u4e00" <= ch <= "\u9fff" and ch not in INITIALS})
    print(f"字表 {len(INITIALS)} 字；类目用字缺失 {len(missing)} 个")
    if missing:
        print("缺字：" + " ".join(missing))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="生成汉字拼音首字母离线表")
    parser.add_argument("--check", action="store_true", help="只校验覆盖度，不写文件")
    args = parser.parse_args()
    if args.check:
        return check_coverage()
    pairs = build_pairs()
    TARGET.write_text(render(pairs), encoding="utf-8")
    print(f"已写入 {TARGET}（{len(pairs)} 字）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
