"""截图 OCR 抽象层。

三种模式通过配置 OCR_PROVIDER 切换：
  none      —— 不识别，仅归档图片（默认）
  tesseract —— 调用本机 tesseract 命令行（需自行安装 tesseract 与中文语言包）
  http      —— POST 到自建/第三方 OCR 服务（PaddleOCR-Serving、百度 OCR 等）
"""
from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import requests

from ..config import settings
from .address import looks_like_address
from .ocr_roi import augment as roi_augment
from .order_no import (
    LABEL_MIN_LEN,
    MAX_LEN,
    ORDER_NO_LABELS,
    order_no_from_text,
    order_no_hints_from_text,
    pick_from_candidates,
)

logger = logging.getLogger(__name__)


class OcrError(RuntimeError):
    pass


@dataclass
class OcrText:
    """一次 OCR 的完整结果。

    text           —— 通用文本（中英混排），给字段解析用
    status         —— done | skipped | failed
    order_no       —— 高置信订单号：在「订单编号」标签旁边直接读到的（可直接当正式单号用）
    order_no_hints —— 疑似订单号：整图数字识别出的长数字（15~24 位），只提示、不直接采信
    """

    text: str = ""
    status: str = "skipped"
    order_no: str = ""
    order_no_hints: list[str] = field(default_factory=list)
    # 收货地址专项识别（整图没认出地址行时，按「收货人 + 手机号」下方那一带单独识别）
    address_hints: list[str] = field(default_factory=list)
    # 金额 / 收货人区域专项识别（ocr_roi）纠正了什么：只有整图明显不对时才会有
    roi_notes: list[str] = field(default_factory=list)


def ocr_image(path: str | Path) -> tuple[str, str]:
    """返回 (文本, 状态)。状态：done | skipped | failed（保留旧接口）"""
    out = ocr_image_ex(path)
    return out.text, out.status


def ocr_image_ex(path: str | Path) -> OcrText:
    """OCR 主入口：通用文本 + 订单号线索。

    订单号单独再认一遍的原因：中文模型对长数字串召回很差（实测整篇文本里
    「订单编号」认出来了、后面的 19 位数字整串丢掉），而订单号是纯数字，
    单独用英文模型 + 数字白名单在标签附近识别，命中率高得多。
    """
    path = Path(path)
    if not path.exists():
        return OcrText(status="failed")

    provider = (settings.ocr_provider or "none").lower()
    if provider == "none":
        return OcrText(status="skipped")
    if provider == "http":
        # http 服务返回整篇文本：只在文本里找线索，不做图像层面增强
        try:
            text = _ocr_http(path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("OCR 失败(http): %s", exc)
            return OcrText(status="failed")
        order_no, why = order_no_from_text(text)
        hints = [] if order_no else order_no_hints_from_text(text)
        if order_no:
            logger.info("截图订单号（文本层 %s）：%s", why, order_no)
        text, address_hints = _augment_with_address(path, text)
        return OcrText(
            text=text, status="done", order_no=order_no, order_no_hints=hints, address_hints=address_hints
        )

    try:
        text = _ocr_tesseract(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("OCR 失败(tesseract): %s", exc)
        return OcrText(status="failed")

    order_no, hints = "", []
    try:
        order_no, why = order_no_near_label(path)
        if order_no:
            logger.info("截图订单号（图像层 %s）：%s", why, order_no)
            # 高置信：把号并进文本，后续字段解析就能直接拿到（与截图上的标签写法一致）
            text = f"{text}\n订单编号 {order_no}" if text else f"订单编号 {order_no}"
        else:
            hints = order_no_hints(path)
            if not hints:
                # 图像层没捞到：再按标签从文本里找一次（比如 OCR 认出了标签和值）
                order_no, why = order_no_from_text(text)
                if order_no:
                    logger.info("截图订单号（文本层 %s）：%s", why, order_no)
    except Exception as exc:  # noqa: BLE001
        logger.warning("订单号专项识别失败: %s", exc)
    text, address_hints = _augment_with_address(path, text)
    text, roi_notes = _roi_augment(path, text)
    return OcrText(
        text=text,
        status="done",
        order_no=order_no,
        order_no_hints=hints,
        address_hints=address_hints,
        roi_notes=roi_notes,
    )


def _prepare_image(path: Path) -> Path:
    """灰度 + 放大：手机截图里的订单编号等小字，原始尺寸下会丢字符。

    实测同一张订单截图：原图只能识别出 "33169016 ... 47870"（中间丢 6 位），
    灰度并放大 2 倍后能完整识别出 "3316901665047047870"。
    """
    scale = max(int(settings.ocr_upscale or 1), 1)
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return path
    try:
        with Image.open(path) as im:
            im = im.convert("L")
            if scale > 1:
                im = im.resize((im.width * scale, im.height * scale), Image.LANCZOS)
            out_dir = Path(tempfile.mkdtemp(prefix="ocr_"))
            out = out_dir / f"{path.stem[:40]}.png"
            im.save(out)
            return out
    except Exception as exc:  # noqa: BLE001 预处理失败就用原图
        logger.warning("图片预处理失败，改用原图 OCR: %s", exc)
        return path


def _prepare_variants(path: Path) -> list[Path]:
    """同一张图生成几个预处理版本（都放在同一个临时目录里）。

    手机截图里「订单编号」这类小字在不同阈值下识别结果差别很大：某版能认出标签、
    另一版才认得出数字。多跑几版把文字合并起来，比调一套「万能参数」稳。
    """
    scale = max(int(settings.ocr_upscale or 1), 1)
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
    except ImportError:
        return [path]
    try:
        out_dir = Path(tempfile.mkdtemp(prefix="ocr_"))
        variants: list[Path] = []
        with Image.open(path) as raw:
            base = raw.convert("L")
            plans = [
                ("gray", scale, False, None),
                ("sharp", scale + 1, True, None),
                ("binary", scale, False, 165),
            ]
            for name, factor, contrast, threshold in plans:
                im = base
                if factor > 1:
                    im = im.resize((im.width * factor, im.height * factor), Image.LANCZOS)
                if contrast:
                    im = ImageOps.autocontrast(im)
                if threshold is not None:
                    im = im.point(lambda pixel, t=threshold: 255 if pixel > t else 0)
                target = out_dir / f"{path.stem[:32]}_{name}.png"
                im.save(target)
                variants.append(target)
        return variants or [path]
    except Exception as exc:  # noqa: BLE001 预处理失败就用原图
        logger.warning("图片预处理失败，改用原图 OCR: %s", exc)
        return [path]


def _merge_texts(parts: list[str]) -> str:
    """多版 OCR 文本合并：按行去重，保留先出现的顺序。"""
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        for line in (part or "").splitlines():
            key = line.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
    return "\n".join(out)


def _run_tesseract(target: Path, lang: str, extra: list[str]) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        raise OcrError("未找到 tesseract 命令，请先安装：brew install tesseract")
    cmd = [exe, str(target), "stdout", "-l", lang, *extra]
    out = subprocess.run(cmd, capture_output=True, timeout=180)
    if out.returncode != 0:
        raise OcrError(
            (out.stderr or b"").decode("utf-8", "replace").strip()[:300] or "tesseract 执行失败"
        )
    # tesseract 输出可能夹杂非 UTF-8 字节，不能直接 text=True
    return out.stdout.decode("utf-8", errors="replace").strip()


def _ocr_tesseract(path: Path) -> str:
    lang = settings.ocr_lang or "chi_sim+eng"
    variants = _prepare_variants(path)
    try:
        parts = [_run_tesseract(target, lang, []) for target in variants]
    finally:
        for target in variants:
            if target != path:
                shutil.rmtree(target.parent, ignore_errors=True)
                break
    return _merge_texts(parts)


# 订单号专项识别：只认数字（订单号是纯数字），用英文模型 + 白名单
_DIGIT_WHITELIST = "0123456789"


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image  # noqa: PLC0415

        with Image.open(path) as im:
            return im.width, im.height
    except Exception:  # noqa: BLE001
        return 0, 0


def _digits_ocr(target: Path, psm: str) -> str:
    """纯数字识别：英文模型 + 数字白名单（订单号是纯数字，中文模型对它召回很差）。"""
    return _run_tesseract(
        target, "eng", ["--psm", psm, "-c", f"tessedit_char_whitelist={_DIGIT_WHITELIST}"]
    )


def _crop_digits(path: Path, box: tuple[int, int, int, int], scale: int = 4, psm: str = "7") -> str:
    """裁一块区域放大后做纯数字识别（box 按原图坐标给，越界自动收窄）。"""
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
    except ImportError:
        return ""
    left, top, right, bottom = (max(int(v), 0) for v in box)
    tmp_dir: Path | None = None
    try:
        with Image.open(path) as im:
            left, top = min(left, im.width), min(top, im.height)
            right, bottom = min(right, im.width), min(bottom, im.height)
            if right - left < 8 or bottom - top < 6:
                return ""
            patch = im.convert("L").crop((left, top, right, bottom))
            if scale > 1:
                patch = patch.resize((patch.width * scale, patch.height * scale), Image.LANCZOS)
            patch = ImageOps.autocontrast(patch)
            tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_no_"))
            target = tmp_dir / "digits.png"
            patch.save(target)
        return _digits_ocr(target, psm)
    except Exception as exc:  # noqa: BLE001
        logger.warning("订单号区域数字识别失败: %s", exc)
        return ""
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _pick_order_no(digits_text: str) -> str:
    """从纯数字 OCR 结果里挑订单号。

    值被 OCR 拆成两行（33169016 / 47870）时先整段拼回；整段太长说明这块区域
    混进了别的东西，再按行挑唯一候选——多个不一样就当作没看清，宁缺勿错。
    """
    text = digits_text or ""
    whole = re.sub(r"\D", "", text)
    if LABEL_MIN_LEN <= len(whole) <= MAX_LEN:
        return whole
    chunks = [re.sub(r"\D", "", line) for line in text.splitlines()]
    return pick_from_candidates(chunks, LABEL_MIN_LEN)


def order_no_near_label(path: Path, scale: int | None = None) -> tuple[str, str]:
    """在「订单编号」标签旁边读订单号：先试同行右侧，再试标签下一行。

    OCR 的文本框来自放大后的图，这里按放大倍数还原成原图坐标再裁切。
    返回 (订单号, 依据)；两处都没读到就返回 ("", "")。
    """
    rows = _tesseract_tsv(path)
    if not rows:
        return "", ""
    factor = max(int(scale or settings.ocr_upscale or 1), 1)
    width, _height = _image_size(path)
    lines: dict[int, list[dict]] = {}
    for row in rows:
        try:
            num = int(str(row.get("line_num", 0)) or 0)
        except (TypeError, ValueError):
            num = 0
        lines.setdefault(num, []).append(row)
    for num in sorted(lines):
        words = sorted(lines[num], key=lambda r: r["left"])
        joined = "".join(str(w.get("text") or "") for w in words)
        # 标签本身可能被认残（「订单编号」→「订单编」），所以「订单」+「号」也算
        if not any(label in joined for label in ORDER_NO_LABELS) and not (
            "订单" in joined and "号" in joined
        ):
            continue
        top = min(w["top"] for w in words) // factor
        bottom = max(w["top"] + w["height"] for w in words) // factor
        right = max(w["left"] + w["width"] for w in words) // factor
        found = _pick_order_no(_crop_digits(path, (right - 6, top - 8, width, bottom + 8)))
        if found:
            return found, "标签同行"
        nxt = sorted(lines.get(num + 1, []), key=lambda r: r["left"])
        if nxt:
            box = (
                min(w["left"] for w in nxt) // factor - 6,
                min(w["top"] for w in nxt) // factor - 8,
                max(w["left"] + w["width"] for w in nxt) // factor + 6,
                max(w["top"] + w["height"] for w in nxt) // factor + 8,
            )
            found = _pick_order_no(_crop_digits(path, box))
            if found:
                return found, "标签下一行"
    return "", ""


def order_no_hints(path: Path) -> list[str]:
    """整图扫一遍数字，挑出像订单号的长数字（15~24 位）当疑似候选。"""
    text = _crop_digits(path, (0, 0, 10**6, 10**6), scale=1, psm="11")
    return order_no_hints_from_text(text)


# ---------------------------------------------------------------- 地址专项识别
# 截图里收货信息是一个固定版式的小块：第一行「收货人 + 手机号」，紧跟着的是地址
# （可能折成两行）。整图 OCR 时这块的小字最容易整行丢失 —— 实测真实截图：
# 收货人、手机号都认出来了，下一行地址一个字都没认出来，而底部的营销文案
# 「领券狂补省得Duo! 马上领」反而认得清清楚楚，结果营销文案被当成了地址。
# 这里的做法与订单号专项识别一致：按版式定位这一带，裁剪 + 放大 + 多阈值单独识别。
# 「像不像地址」的口径与解析层共用一份（parser.address），避免两边判断漂移。
_PHONE_IN_TEXT_RE = re.compile(r"1[3-9]\d{9}")
# 定位收货信息块的锚点：优先含手机号的行，其次这些字样
_RECEIVER_ANCHORS = ("收货人", "收件人", "收货地址", "买家已付款", "请尽快发货")
# 地址带的高度上限：锚行行高的倍数（地址最长也就折成两三行）
_ADDR_BAND_LINES = 3.4


def _group_tsv_lines(rows: list[dict]) -> dict[tuple[int, int, int], list[dict]]:
    """把词框分组成行：key 用 (block, par, line)。

    只用 line_num 会把不同 block 的行混成一行（每个 block 的行号都从 1 开始），
    实测整张截图会被并成「一行」，收货人行就找不出来了。
    """
    lines: dict[tuple[int, int, int], list[dict]] = {}

    def _num(row: dict, key: str) -> int:
        try:
            return int(str(row.get(key, 0)) or 0)
        except (TypeError, ValueError):
            return 0

    for row in rows:
        key = (_num(row, "block_num"), _num(row, "par_num"), _num(row, "line_num"))
        lines.setdefault(key, []).append(row)
    return {key: sorted(words, key=lambda r: r["left"]) for key, words in lines.items()}


def _line_box(words: list[dict], factor: int) -> tuple[int, int, int, int]:
    """一行词框的外接框（还原成原图坐标）。"""
    left = min(w["left"] for w in words) // factor
    top = min(w["top"] for w in words) // factor
    right = max(w["left"] + w["width"] for w in words) // factor
    bottom = max(w["top"] + w["height"] for w in words) // factor
    return left, top, right, bottom


def _crop_text(
    path: Path,
    box: tuple[int, int, int, int],
    scale: int = 3,
    psm: str = "6",
    threshold: int | None = None,
) -> str:
    """裁一块区域放大后做通用文字识别（地址用，不能用数字白名单）。"""
    try:
        from PIL import Image, ImageOps  # noqa: PLC0415
    except ImportError:
        return ""
    left, top, right, bottom = (max(int(v), 0) for v in box)
    tmp_dir: Path | None = None
    try:
        with Image.open(path) as im:
            left, top = min(left, im.width), min(top, im.height)
            right, bottom = min(right, im.width), min(bottom, im.height)
            if right - left < 12 or bottom - top < 6:
                return ""
            patch = im.convert("L").crop((left, top, right, bottom))
            if scale > 1:
                patch = patch.resize((patch.width * scale, patch.height * scale), Image.LANCZOS)
            patch = ImageOps.autocontrast(patch)
            if threshold is not None:
                patch = patch.point(lambda pixel, t=threshold: 255 if pixel > t else 0)
            tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_addr_"))
            target = tmp_dir / "text.png"
            patch.save(target)
        return _run_tesseract(target, settings.ocr_lang or "chi_sim+eng", ["--psm", psm])
    except Exception as exc:  # noqa: BLE001
        logger.warning("地址区域识别失败: %s", exc)
        return ""
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# 地址行里常被一起带进来的界面文案：见到就截断（「…湘邮科 去发货」）
_ADDR_UI_NOISE = (
    "去发货", "联系买家", "取消订单", "立即购买", "免费领", "复制", "重新裁取",
    "确认收货", "申请退款", "联系卖家", "查看物流",
)


def _tidy_address(value: str) -> str:
    """清掉地址行里的 OCR 噪声符号、界面文案与尾部连续字母（「…湘邮科PRR」）。"""
    v = re.sub(r"\s+", "", value or "")
    v = re.sub(r"[|丨Il\[\]【】()（）<>=+_“”\"']+", "", v)
    v = re.sub(r"[A-Za-z]{2,}$", "", v)
    for word in _ADDR_UI_NOISE:
        pos = v.find(word)
        if pos > 0:
            v = v[:pos]
    return v.strip("，,。.;；:：-—~·")


def _looks_like_address_line(line: str) -> bool:
    """像不像一条地址行：口径与解析层完全一致（parser.address）。"""
    return looks_like_address(_tidy_address(line))


# 地址折行的续行：很短的纯中文（「…湘邮科」+「技园」）
_ADDR_CONT_RE = re.compile(r"^[\u4e00-\u9fa5]{1,8}$")
_ADDR_CONT_STOP = (
    "备注", "收货人", "收件人", "复制", "去发货", "取消订单", "联系买家", "免费领",
    "发货", "包邮", "评价", "客服", "退款", "退货", "付款", "下单",
)


def _is_address_continuation(text: str) -> bool:
    """这一行是不是上一行地址被折下来的后半截。"""
    v = _tidy_address(text)
    if not _ADDR_CONT_RE.match(v):
        return False
    return not any(word in v for word in _ADDR_CONT_STOP)


def _merge_address_rows(row_texts: list[str]) -> list[str]:
    """逐行识别结果拼成地址候选：像地址的行开头，紧跟的短中文续行并入。"""
    out: list[str] = []
    for raw in row_texts:
        for line in (raw or "").splitlines():
            if _looks_like_address_line(line):
                out.append(_tidy_address(line))
                continue
            piece = _tidy_address(line)
            if out and _is_address_continuation(piece):
                out[-1] += piece
    return out


def _receiver_anchor_line(lines: dict[tuple[int, int, int], list[dict]]) -> tuple | None:
    """收货信息块的第一行：含手机号的行最可靠（OCR 对手机号召回高）。"""
    fallback: tuple | None = None
    for key in sorted(lines, key=lambda k: (k[0], k[1], k[2])):
        words = lines[key]
        compact = re.sub(r"\s+", "", "".join(str(w.get("text") or "") for w in words))
        if _PHONE_IN_TEXT_RE.search(compact):
            return key
        if fallback is None and any(mark in compact for mark in _RECEIVER_ANCHORS):
            fallback = key
    return fallback


def address_lines(path: str | Path, scale: int | None = None) -> list[str]:
    """在「收货人 + 手机号」那一行的下面找地址，返回识别出的候选行。

    做法：先用词框定位收货人行，再把它下面到「下一个被识别出的文字行」之间的
    一条带裁出来（地址行本身往往在整图 OCR 里没被认出来，所以只能靠版式定位），
    放大后按多个阈值各识别一遍，合并结果。识别不出就返回空列表。
    """
    path = Path(path)
    if not path.exists():
        return []
    rows = _tesseract_tsv(path)
    if not rows:
        return []
    lines = _group_tsv_lines(rows)
    anchor = _receiver_anchor_line(lines)
    if anchor is None:
        return []
    factor = max(int(scale or settings.ocr_upscale or 1), 1)
    width, height = _image_size(path)
    if not width or not height:
        return []

    _left, top, _right, bottom = _line_box(lines[anchor], factor)
    line_h = max(bottom - top, 12)
    # 下一个被识别出的文字行就是这条带的天然下界（地址行本身常常没被认出来）
    below = [
        _line_box(words, factor)[1] for words in lines.values() if _line_box(words, factor)[1] > bottom
    ]
    limit = min(below) if below else height
    band_top = max(bottom - 2, 0)
    band_bottom = min(limit, band_top + int(line_h * _ADDR_BAND_LINES))
    if band_bottom - band_top < 6:
        return []

    # 逐行裁开单独识别：地址常折成两行，整块识别会把两行粘在一起
    #（实测「…湘邮科」+「技园」被认成「…湘邮科PRR」），逐行识别才能拼回一条。
    step = max(int(line_h * 1.05), 12)
    row_texts: list[str] = []
    y = band_top
    while y < band_bottom and len(row_texts) < 4:
        row_box = (0, max(y - 2, 0), width, min(y + step + 4, band_bottom + 4))
        row_texts.append(_crop_text(path, row_box, scale=4, psm="7").strip())
        y += step

    candidates = _merge_address_rows(row_texts)
    # 整块再识一遍交叉验证：逐行切分偶尔会切掉字的上下沿，整块识别的结果更完整
    block = _crop_text(path, (0, band_top, width, band_bottom), scale=4, psm="6")
    candidates.extend(_merge_address_rows([block]))

    found: list[str] = []
    for value in candidates:
        if not _looks_like_address_line(value):
            continue
        key = _tidy_address(value)
        if key and key not in found:
            found.append(key)
    if not found:
        return []
    # 同一个地址在不同预处理下会认成几个版本（个别字不同）：按「与其它版本的重合度」
    # 投票排序，重合度最高的排前面 —— 多数版本一致的那条，比单看某一次识别更可信。
    # 同分时优先结尾像个地址的（「…湘邮科技园」优先于「…湘邮科上趾」）。
    ranked = sorted(
        found,
        key=lambda value: (
            0 if value.endswith(_ADDR_TAIL_WORDS) else 1,
            -sum(_shared_fragments(value, other) for other in found if other != value),
            -len(value),
        ),
    )
    return [v for v in ranked if not any(v != other and v in other for other in ranked)]


def _text_has_address(text: str) -> bool:
    return any(_looks_like_address_line(line) for line in (text or "").splitlines())


def _augment_with_address(path: Path, text: str) -> tuple[str, list[str]]:
    """整图没认出地址行时，按版式再专项识别一次，把结果并进文本。

    只有「整图里找不到像地址的行」时才多跑几次 tesseract（常规订单不受影响）。
    """
    if _text_has_address(text):
        return text, []
    try:
        found = address_lines(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("地址专项识别失败: %s", exc)
        return text, []
    if not found:
        return text, []
    logger.info("截图地址（版式专项识别）：%s", found[0])
    joined = "\n".join(found)
    return (f"{text}\n{joined}".strip() if text else joined), found


def _roi_augment(path: Path, text: str) -> tuple[str, list[str]]:
    """金额 / 收货人区域专项识别（ocr_roi）。

    与订单号、地址两项是同一套做法（裁区域 -> 放大 -> 单独认），区别是它只在
    整图结果明显不对时才纠正 —— 认不出来就维持原样，不会把对的改错。
    依赖 tesseract 词框，所以 http 模式（只有整篇文本）下不跑。
    """
    if not getattr(settings, "ocr_roi", True):
        return text, []
    try:
        new_text, notes = roi_augment(path, text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ROI 专项识别失败: %s", exc)
        return text, []
    for note in notes:
        logger.info("截图 ROI：%s", note)
    return new_text, notes


# 地址常见的结尾字：同一地址的几个识别版本里，结尾像地址的那条更可信
_ADDR_TAIL_WORDS = (
    "园", "号", "室", "栋", "单元", "楼", "层", "村", "路", "街", "院", "区", "县", "市",
    "镇", "城", "站", "厂", "场", "苑", "厦", "铺", "组", "幢", "座", "公司", "中心", "学校",
)


def _shared_fragments(a: str, b: str) -> int:
    """两段文字共有的 3 字片段数（错字容忍的相似度，比整串比较稳）。"""
    x, y = _tidy_address(a), _tidy_address(b)
    if len(x) < 3 or len(y) < 3:
        return 0
    grams = {x[i : i + 3] for i in range(len(x) - 2)}
    return sum(1 for i in range(len(y) - 2) if y[i : i + 3] in grams)


def _ocr_http(path: Path) -> str:
    url = settings.ocr_http_url
    if not url:
        raise OcrError("未配置 OCR_HTTP_URL")
    with path.open("rb") as fp:
        files = {settings.ocr_http_field or "image": (path.name, fp)}
        resp = requests.post(url, files=files, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    key = settings.ocr_http_text_key or "text"
    if isinstance(data, dict):
        value = data.get(key)
        if value is None:
            raise OcrError(f"OCR 响应缺少字段 {key}")
        if isinstance(value, list):
            return "\n".join(str(x) for x in value)
        return str(value)
    return str(data)


def _tesseract_tsv(path: Path) -> list[dict]:
    """取 tesseract 的词框（用于定位商品缩略图所在的商品行）。"""
    exe = shutil.which("tesseract")
    if not exe:
        return []
    target = _prepare_image(path)
    try:
        cmd = [exe, str(target), "stdout", "-l", settings.ocr_lang or "chi_sim+eng", "tsv"]
        out = subprocess.run(cmd, capture_output=True, timeout=180)
        text = out.stdout.decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        logger.warning("tesseract tsv 失败: %s", exc)
        return []
    finally:
        if target != path:
            shutil.rmtree(target.parent, ignore_errors=True)

    rows: list[dict] = []
    lines = text.splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) != len(header):
            continue
        row = dict(zip(header, parts))
        try:
            row["left"] = int(row.get("left", 0))
            row["top"] = int(row.get("top", 0))
            row["width"] = int(row.get("width", 0))
            row["height"] = int(row.get("height", 0))
        except (TypeError, ValueError):
            continue
        if not (row.get("text") or "").strip():
            continue
        rows.append(row)
    return rows


# ---- 商品缩略图定位参数（闲鱼 / 淘宝订单详情版式）----
_THUMB_WORK_WIDTH = 540          # 投影用的工作图宽度：缩小后纯 Python 也够快
_THUMB_SCAN_X = (0.01, 0.46)     # 横向搜索区间（占工作图宽比例）
_THUMB_SCAN_Y = (0.10, 0.52)     # 纵向搜索区间：缩略图在页面上半部分，避开底部广告位
_THUMB_MIN_SIDE = 40             # 工作图坐标下的最小边长，太小的不当商品图
_THUMB_MAX_GROW = 0.16           # 向白底扩展的上限（占工作图宽比例）


def _pixels(image) -> list:
    """取像素列表（Pillow 12 起 getdata 弃用，做一层兼容）。"""
    getter = getattr(image, "get_flattened_data", None)
    if callable(getter):
        return list(getter())
    return list(image.getdata())


def _longest_segment(
    counts: list[int], threshold: float, max_gap: int
) -> tuple[int, int] | None:
    """找 counts 里最长的一段连续（允许 max_gap 个空档）区间。"""
    best: tuple[int, int] | None = None
    start: int | None = None
    last = 0
    for index, value in enumerate(counts):
        if value >= threshold:
            if start is None:
                start = index
            last = index
        elif start is not None and index - last > max_gap:
            if best is None or (last - start) > (best[1] - best[0]):
                best = (start, last)
            start = None
    if start is not None and (best is None or (last - start) > (best[1] - best[0])):
        best = (start, last)
    return best


def _locate_product_thumb(image) -> tuple[int, int, int, int] | None:
    """在订单截图里定位商品缩略图，返回原图坐标 (left, top, right, bottom)。

    商品缩略图是照片：色彩/纹理丰富、连续多行都有内容；卡片白底则非常干净。
    所以先用「彩色内容」做行列投影锁定商品主体，再向四周扩展到页面白底为止，
    得到缩略图的真实边界（照片里的浅色木纹也算内容，只有白底才算边界）。
    """
    width, height = image.size
    if width < 160 or height < 160:
        return None
    work_width = min(width, _THUMB_WORK_WIDTH)
    scale = work_width / width
    work = image.convert("RGB").resize(
        (work_width, max(int(round(height * scale)), 1))
    )
    ww, wh = work.size
    x0, x1 = int(ww * _THUMB_SCAN_X[0]), int(ww * _THUMB_SCAN_X[1])
    y0, y1 = int(wh * _THUMB_SCAN_Y[0]), int(wh * _THUMB_SCAN_Y[1])
    if x1 - x0 < 60 or y1 - y0 < 60:
        return None

    region = work.crop((x0, y0, x1, y1))
    rw, rh = region.size
    # 彩色像素：通道差 > 20（照片里的彩色）且不是过暗的阴影
    colored = [
        (max(pixel) - min(pixel) > 20 and max(pixel) > 60)
        for pixel in _pixels(region)
    ]
    rows = [0] * rh
    cols = [0] * rw
    for index, flag in enumerate(colored):
        if flag:
            rows[index // rw] += 1
            cols[index % rw] += 1
    if not any(rows):
        return None

    col_segment = _longest_segment(cols, max(max(cols) * 0.35, rh * 0.05), max(int(rw * 0.04), 2))
    if col_segment is None:
        return None
    c0, c1 = col_segment
    span = c1 - c0 + 1
    inner = [0] * rh
    for row in range(rh):
        base = row * rw
        inner[row] = sum(1 for col in range(c0, c1 + 1) if colored[base + col])
    row_segment = _longest_segment(inner, max(max(inner) * 0.35, span * 0.10), max(int(rh * 0.04), 2))
    if row_segment is None:
        return None
    r0, r1 = row_segment

    left, right = x0 + c0, x0 + c1 + 1
    top, bottom = y0 + r0, y0 + r1 + 1
    if right - left < _THUMB_MIN_SIDE and bottom - top < _THUMB_MIN_SIDE:
        return None

    gray = work.convert("L")

    def is_backdrop(pixels: list[int]) -> bool:
        """整行 / 整列基本是白的 -> 已经到卡片白底了。"""
        return sum(1 for v in pixels if v >= 243) >= len(pixels) * 0.9

    limit = int(ww * _THUMB_MAX_GROW)
    for _ in range(limit):
        if top <= 0 or is_backdrop(_pixels(gray.crop((left, top - 1, right, top)))):
            break
        top -= 1
    for _ in range(limit):
        if bottom >= wh or is_backdrop(_pixels(gray.crop((left, bottom, right, bottom + 1)))):
            break
        bottom += 1
    for _ in range(limit):
        if left <= 0 or is_backdrop(_pixels(gray.crop((left - 1, top, left, bottom)))):
            break
        left -= 1
    for _ in range(limit):
        if right >= ww or is_backdrop(_pixels(gray.crop((right, top, right + 1, bottom)))):
            break
        right += 1

    side = max(right - left, bottom - top)
    if side < _THUMB_MIN_SIDE:
        return None
    center_x = (left + right) / 2
    center_y = (top + bottom) / 2
    bx0 = max(int(round(center_x - side / 2)), 0)
    by0 = max(int(round(center_y - side / 2)), 0)
    bx1 = min(bx0 + side, ww)
    by1 = min(by0 + side, wh)
    if (bx1 - bx0) < _THUMB_MIN_SIDE or (by1 - by0) < _THUMB_MIN_SIDE:
        return None
    return (
        int(bx0 / scale),
        int(by0 / scale),
        int(bx1 / scale),
        int(by1 / scale),
    )


def _locate_product_thumb_by_ocr(src: Path) -> tuple[int, int, int, int] | None:
    """兜底定位：用 OCR 词框找最靠上的「¥」行（商品行），裁它左侧的方图。"""
    rows = _tesseract_tsv(src)
    price_rows = [
        r for r in rows if ("¥" in r["text"] or "￥" in r["text"]) and r["height"] > 0
    ]
    if not price_rows:
        return None
    base_row = min(price_rows, key=lambda r: r["top"])
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError:
        return None
    with Image.open(src) as im:
        width, height = im.size
    scale = max(int(settings.ocr_upscale or 1), 1)
    center_y = base_row["top"] / scale + base_row["height"] / scale / 2
    side = min(width * 0.21, height)
    if side < 40:
        return None
    x0 = int(width * 0.03)
    y0 = max(int(center_y - side / 2), 0)
    x1 = min(x0 + int(side), width)
    y1 = min(y0 + int(side), height)
    if (x1 - x0) < 40 or (y1 - y0) < 40:
        return None
    return (x0, y0, x1, y1)


def crop_product_image(path: str | Path, out_path: str | Path) -> str | None:
    """从订单截图里裁出商品缩略图（详情页与订单列表都用这张图）。

    闲鱼订单详情的版式：商品缩略图是收货信息卡片下方、成交价上方的左侧方图。
    定位顺序：图像分析（彩色内容 + 向白底扩展）-> OCR 词框兜底；
    定位不到、比例不像方图、裁出来是纯色（空白）—— 一律返回 None，宁缺勿错。
    """
    src = Path(path)
    dst = Path(out_path)
    if not src.exists():
        return None
    try:
        from PIL import Image, ImageStat  # noqa: PLC0415
    except ImportError:
        return None

    try:
        with Image.open(src) as raw:
            image = raw.convert("RGB")
            box = _locate_product_thumb(image)
            if box is None:
                box = _locate_product_thumb_by_ocr(src)
            if box is None:
                return None
            crop = image.crop(box)
            if min(crop.size) < 40:
                return None
            stat = ImageStat.Stat(crop.convert("L"))
            if max(stat.stddev) < 6:  # 纯色 / 空白区域，说明裁歪了
                return None
            # 统一成方图，过大时缩到 520，便于列表缩略图复用同一张文件
            side = max(crop.size)
            if side > 520:
                crop = crop.resize((520, 520), Image.LANCZOS)
            elif crop.size[0] != crop.size[1]:
                side = max(crop.size)
                crop = crop.resize((side, side), Image.LANCZOS)
            dst.parent.mkdir(parents=True, exist_ok=True)
            crop.save(dst, "PNG")
    except Exception as exc:  # noqa: BLE001
        logger.warning("裁剪商品图失败: %s", exc)
        return None
    return str(dst)


def ocr_bytes(data: bytes, suffix: str = ".png") -> tuple[str, str]:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return ocr_image(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()
