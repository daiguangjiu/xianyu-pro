"""订单号识别：把订单号从 OCR 文本里捞出来。

闲鱼订单号是纯数字（常见 19 位，老单 16~22 位），截图 OCR 经常出三种岔子：
  1 把长数字拆成几段：「订单编号   33169016   47870」；
  2 把数字认成字母（O/l/S 这类）；
  3 干脆漏掉标签后面的数字，只在下一行留一串数字。

所以这里分两步：
  · 优先：取「订单编号 / 订单号 / 交易单号 / 平台单号」标签旁边（同行或紧邻下一行）的数字；
  · 兜底：标签全丢了时，用「长度像订单号的数字串」当疑似候选，交人工确认，不直接采信。
"""
from __future__ import annotations

import re

# 订单号在截图里的常见标签
ORDER_NO_LABELS: tuple[str, ...] = (
    "订单编号",
    "订单号",
    "交易单号",
    "平台单号",
    "订单ID",
    "订单id",
    "订单 Id",
)

# 标签旁边认到的号：有标签兜着，允许短一点（有些平台单号只有 12~14 位）
LABEL_MIN_LEN = 12
# 没有标签时的兜底：必须像订单号那么长，避免把时间、金额、手机号当号
HINT_MIN_LEN = 15
MAX_LEN = 24


def _tidy(raw: str, min_len: int) -> str:
    """把一段文本还原成订单号：去掉空白与分隔符，只留数字。"""
    value = re.sub(r"[\s\-—_]+", "", raw or "")
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if not (min_len <= len(digits) <= MAX_LEN):
        return ""
    return digits


def order_no_from_text(text: str) -> tuple[str, str]:
    """按标签取订单号，返回 (订单号, 依据)。取不到返回 ("", "")。

    依据会写进日志 / 待确认原因，方便运营判断这个号是怎么来的。
    """
    lines = [line.strip() for line in (text or "").splitlines()]
    for index, line in enumerate(lines):
        if not any(label in line for label in ORDER_NO_LABELS):
            continue
        for label in ORDER_NO_LABELS:
            pos = line.find(label)
            if pos < 0:
                continue
            found = _tidy(line[pos + len(label) :], LABEL_MIN_LEN)
            if found:
                return found, f"标签「{label}」同行"
        # 标签后面的值被 OCR 挤到了下一行（实测常见）
        for follow in lines[index + 1 : index + 3]:
            if not follow or any(label in follow for label in ORDER_NO_LABELS):
                break
            found = _tidy(follow, LABEL_MIN_LEN)
            if found:
                return found, "标签下一行"
    return "", ""


# 「已复制收货信息」弹窗会把「手机号-8893」连成一串（18448261335-8893 → 15 位），
# 长度恰好够到疑似订单号的门槛 —— 实测（2026-10-16）被当成疑似单号提示给运营。
# 数字串的主体是手机号、前后只挂了零星几位的，不是订单号。
_PHONE_IN_DIGITS = re.compile(r"1[3-9]\d{9}")


def _is_phone_with_tail(digits: str) -> bool:
    match = _PHONE_IN_DIGITS.search(digits or "")
    if not match:
        return False
    rest = digits[: match.start()] + digits[match.end() :]
    return len(rest) <= 4


def order_no_hints_from_text(text: str) -> list[str]:
    """没有标签时的疑似订单号：文本里像订单号的长数字串（不直接采信）。"""
    out: list[str] = []
    for raw in re.findall(r"[0-9][0-9 \t\-]{14,40}", text or ""):
        value = _tidy(raw, HINT_MIN_LEN)
        if not value or value in out or _is_phone_with_tail(value):
            continue
        out.append(value)
    return out


def pick_from_candidates(candidates: list[str], min_len: int = LABEL_MIN_LEN) -> str:
    """从多个数字候选里挑一个：只接受唯一候选，多个不一样就当作没看清。"""
    values = [c for c in dict.fromkeys(candidates or []) if c and len(c) >= min_len]
    if len(values) == 1:
        return values[0]
    return ""
