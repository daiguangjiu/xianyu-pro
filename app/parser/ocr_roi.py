"""订单截图 ROI 专项识别：按版式裁区域、放大、多阈值投票再认。

为什么整图一遍识别不够：实测真实闲鱼订单详情页截图（1080x2400 上下），
整图 OCR 对「小字 + 中英混排」的召回很差 —— 收货人姓名整行糊成 "FINASHR"、
商品总价认成 "¥9.00)¥50.00"。而订单号、地址两项早就改成「按标签定位 ->
裁剪 -> 放大 -> 单独识别」，命中率明显更高。这个模块把同样的做法补到
「金额」和「收货人/电话」上：

    y 区间        区域                整图 OCR 的实测表现
    ~500-560     收货人 + 手机号       电话准，姓名糊
    ~560-680     收货地址（1~2 行）    已有专项（ocr.address_lines）
    1000-1200    成交价/商品总价/运费  偶发乱码（值右对齐，标签在左）
    ~1280        订单编号             已有专项（ocr.order_no_near_label）

两条硬规矩（与系统里其它「宁缺勿错」的地方一致）：
  1. 定位不到锚点、裁出来的区域不像、识别结果过不了格式校验 —— 一律不动原文；
  2. 只纠正「原文明显不对」的字段：金额靠「成交价 == 商品总价」这条版式规律，
     姓名靠「像不像中文姓名」。ROI 认不出来时，最差结果是维持现状。

结果以「前置到文本头部」的方式接回去：解析层 re.search 取首个命中、
extract_kv_lines 取首个同名标签，追加在末尾不会生效。
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------- 常量与判据
_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_PHONE_HEAD_RE = re.compile(r"1[3-9]\d")
# 金额标签（按版式：值是右对齐的 ¥xx.xx，标签在左）
_MONEY_LABELS = ("商品总价", "成交价", "实付款", "售价", "合计")
# 金额只认「两位小数」的钱，孤立的整数多半是界面上的别的东西
_MONEY_NUM_RE = re.compile(r"(\d+\.\d{2})")
_MONEY_WHITELIST = "0123456789."
_NAME_RE = re.compile(r"^[\u4e00-\u9fa5]{2,6}$")
# 姓名区域里常被一起裁进来的界面文案与标签字。
# 实测踩过的坑：收货人那一行右侧就是「去发货」按钮，词框按 y 分组合行时会把它
# 并进同一行 —— 一不留神就把「去发货」「去上发贷」认成了姓名。见到这些一律丢弃。
_NAME_STOPWORDS = (
    "去发货", "发货", "联系买家", "联系卖家", "取消订单", "确认收货", "申请退款",
    "查看物流", "已付款", "请尽快", "买家", "卖家", "收货人", "收件人", "收货",
    "收件", "地址", "联系", "手机", "电话", "姓名", "客户", "先生", "女士",
    "编码", "邮编", "人",
    # 「去发货」还常被认成错字版（「去上发贷」），认词表认不全，直接按「去」拦
    "去",
)
# 多阈值：不同截图底色/字号下，哪一档最清楚不一样，多认几遍投票。
# 只取三档：每多跑一次就是一次完整的 tesseract（中文模型加载 + 大图识别），
# 实测四档时一张图要 40~60s，三档已经够投票（两票一致即采信）。
_THRESHOLDS: tuple[int | None, ...] = (None, 160, 200)
# 放大后至少这么高，小字在原始尺寸下必糊
_MIN_TEXT_HEIGHT = 34


def _ocr():
    """延迟导入 ocr：ocr 会反过来用本模块做增强，直接导入会循环。"""
    from app.parser import ocr  # noqa: PLC0415

    return ocr


def _enabled() -> bool:
    return bool(getattr(settings, "ocr_roi", True))


# ---------------------------------------------------------------- 裁剪与识别
def _crop_ocr(
    path: Path,
    box: tuple[int, int, int, int],
    scale: int = 4,
    psm: str = "7",
    lang: str | None = None,
    whitelist: str | None = None,
    threshold: int | None = None,
) -> str:
    """裁一块区域 -> 灰度 -> 自适应放大 -> 增强对比（可选二值化）-> 单独识别。

    与 ocr._crop_text 的区别：放大倍数按区域高度自适应（保证字高 >= 34px），
    且支持数字白名单与二值化阈值 —— 金额、手机号这类纯数字区用白名单更稳。
    """
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
            factor = max(int(scale or 1), 1)
            if patch.height and patch.height * factor < _MIN_TEXT_HEIGHT:
                factor = max(factor, _MIN_TEXT_HEIGHT // patch.height + 1)
            if factor > 1:
                patch = patch.resize(
                    (patch.width * factor, patch.height * factor), Image.LANCZOS
                )
            patch = ImageOps.autocontrast(patch)
            if threshold is not None:
                patch = patch.point(lambda pixel, t=threshold: 255 if pixel > t else 0)
            tmp_dir = Path(tempfile.mkdtemp(prefix="ocr_roi_"))
            target = tmp_dir / "roi.png"
            patch.save(target)
        args = ["--psm", psm]
        if whitelist:
            args += ["-c", f"tessedit_char_whitelist={whitelist}"]
        return _run_tesseract(target, lang or (settings.ocr_lang or "chi_sim+eng"), args)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ROI 区域识别失败: %s", exc)
        return ""
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _run_tesseract(target: Path, lang: str, extra: list[str]) -> str:
    exe = shutil.which("tesseract")
    if not exe:
        return ""
    cmd = [exe, str(target), "stdout", "-l", lang, *extra]
    out = subprocess.run(cmd, capture_output=True, timeout=120)
    if out.returncode != 0:
        return ""
    return out.stdout.decode("utf-8", errors="replace").strip()


def _vote(candidates: list[str], min_votes: int = 1) -> str:
    """多版本投票：出现次数多的优先，同票取更长的（信息更全）。

    min_votes：至少被几档预处理认出来才算数。姓名没有第二个数据源可以交叉
    验证（金额有「成交价 == 商品总价」这条版式规律），所以要求至少两档一致 ——
    四档里各认各的，说明这块根本没看清，宁可不要。
    """
    votes: dict[str, int] = {}
    for item in candidates:
        if item:
            votes[item] = votes.get(item, 0) + 1
    if not votes:
        return ""
    best = max(votes.items(), key=lambda kv: (kv[1], len(kv[0])))
    return best[0] if best[1] >= min_votes else ""


def _strip_noise(token: str) -> str:
    """去掉首尾粘着的标签字（「人孙本阁」->「孙本阁」）。"""
    for word in _NAME_STOPWORDS:
        while token.startswith(word) and len(token) > len(word):
            token = token[len(word) :]
        while token.endswith(word) and len(token) > len(word):
            token = token[: -len(word)]
    return token


# ---------------------------------------------------------------- 金额区 ROI
def money_roi(path: str | Path, scale: int | None = None) -> dict[str, float]:
    """在「商品总价 / 成交价」这些标签右边读金额，返回 {标签: 金额}。

    整图 OCR 里这块偶发把两个金额粘成一行（"¥9.00)¥50.00"），裁出标签右侧
    的窄条单独用数字白名单认，基本只剩那一个金额。
    """
    path = Path(path)
    ocr = _ocr()
    rows = ocr._tesseract_tsv(path)  # noqa: SLF001
    if not rows:
        return {}
    lines = ocr._group_tsv_lines(rows)  # noqa: SLF001
    factor = max(int(scale or settings.ocr_upscale or 1), 1)
    width, _height = ocr._image_size(path)  # noqa: SLF001
    if not width:
        return {}
    found: dict[str, float] = {}
    for key in sorted(lines, key=lambda k: (int(k[0]), int(k[1]), int(k[2]))):
        words = lines[key]
        joined = "".join(str(w.get("text") or "") for w in words)
        label = next((lb for lb in _MONEY_LABELS if lb in joined), None)
        if label is None:
            continue
        top = min(w["top"] for w in words) // factor
        bottom = max(w["top"] + w["height"] for w in words) // factor
        # 标签右边到行尾：金额是右对齐的，标签就在这行的左边
        label_words = [w for w in words if label in str(w.get("text") or "")]
        if label_words:
            label_right = max(w["left"] + w["width"] for w in label_words) // factor
        else:
            label_right = min(w["left"] for w in words) // factor
        # 金额是右对齐的，左半边是标签和空白 —— 裁右半就够，图越小 tesseract 越快
        left = max(label_right - 4, int(width * 0.45))
        value = _money_in_patch(path, (left, top - 6, width, bottom + 6))
        if value is not None:
            found.setdefault(label, value)
    return found


def _money_in_patch(path: Path, box: tuple[int, int, int, int]) -> float | None:
    """多阈值认几遍，投票取金额（只接受两位小数的正数）。"""
    picked: list[float] = []
    for threshold in _THRESHOLDS:
        text = _crop_ocr(
            path,
            box,
            scale=3,
            psm="7",
            lang="eng",
            whitelist=_MONEY_WHITELIST,
            threshold=threshold,
        )
        found = _MONEY_NUM_RE.search(text or "")
        if not found:
            continue
        try:
            value = round(float(found.group(1)), 2)
        except ValueError:
            continue
        if 0 < value < 100000:
            picked.append(value)
    if not picked:
        return None
    votes: dict[float, int] = {}
    for value in picked:
        votes[value] = votes.get(value, 0) + 1
    return max(votes.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def money_from_text(text: str) -> dict[str, float]:
    """从整图文本里取各金额标签的值。"""
    out: dict[str, float] = {}
    for line in (text or "").splitlines():
        for label in _MONEY_LABELS:
            if label not in line:
                continue
            found = _MONEY_NUM_RE.search(line)
            if found:
                try:
                    out.setdefault(label, round(float(found.group(1)), 2))
                except ValueError:
                    pass
            break
    return out


def _money_need_fix(values: dict[str, float], roi_value: float | None) -> tuple[bool, str]:
    """整图金额要不要用 ROI 值纠正。

    版式规律：闲鱼订单详情里「成交价」与「商品总价」是同一个数（运费另算）。
    两者都在却对不上，说明至少有一个被认错了 —— 这时才纠正，避免乱改对的。
    """
    if roi_value is None:
        return False, ""
    if not values:
        return True, "整图没认出金额"
    deal = values.get("成交价", values.get("实付款"))
    total = values.get("商品总价", values.get("售价"))
    if deal is not None and total is not None and abs(deal - total) > 0.01:
        return True, f"成交价 ¥{deal:.2f} 与商品总价 ¥{total:.2f} 对不上"
    if all(abs(value - roi_value) <= 0.01 for value in values.values()):
        return False, ""
    return True, "整图金额与专项识别不一致"


def _money_consistent(values: dict[str, float]) -> bool:
    """整图金额能不能直接信：成交价与商品总价都在且相等（闲鱼版式就是如此）。

    只有「两处对不上 / 只认出一个 / 一个都没有」时才值得再跑一次 ROI ——
    实测八张真实截图里有七张不属于这三种情况，跑了也白跑还费时间。
    """
    deal = values.get("成交价", values.get("实付款"))
    total = values.get("商品总价", values.get("售价"))
    return deal is not None and total is not None and abs(deal - total) <= 0.01


# ---------------------------------------------------------------- 收货人区 ROI
def contact_roi(path: str | Path, scale: int | None = None) -> tuple[str, str]:
    """在「手机号」那一行里读收货人姓名和手机号，返回 (姓名, 手机号)。

    手机号本身整图召回就高，这里主要是把它左边那块姓名单独放大再认 ——
    整图里姓名是最糊的一块（实测 "FINASHR"、"冰本研"）。
    """
    path = Path(path)
    ocr = _ocr()
    rows = ocr._tesseract_tsv(path)  # noqa: SLF001
    if not rows:
        return "", ""
    lines = ocr._group_tsv_lines(rows)  # noqa: SLF001
    factor = max(int(scale or settings.ocr_upscale or 1), 1)
    for key in sorted(lines, key=lambda k: (int(k[0]), int(k[1]), int(k[2]))):
        words = lines[key]
        joined = re.sub(r"\s+", "", "".join(str(w.get("text") or "") for w in words))
        if not _PHONE_RE.search(joined):
            continue
        top = min(w["top"] for w in words) // factor
        bottom = max(w["top"] + w["height"] for w in words) // factor
        line_left = min(w["left"] for w in words) // factor
        line_right = max(w["left"] + w["width"] for w in words) // factor
        # 手机号所在的词：数字常被拆成 1~2 个词，取带 1xx 开头的那一段
        phone_words = [w for w in words if _PHONE_HEAD_RE.search(str(w.get("text") or ""))]
        if not phone_words:
            continue
        phone_left = min(w["left"] for w in phone_words) // factor
        phone_right = max(w["left"] + w["width"] for w in phone_words) // factor
        phone = _phone_in_patch(path, (phone_left - 4, top - 6, phone_right + 4, bottom + 6))
        # 姓名在手机号左边。起点要跳过「收货人」这类标签，否则标签字会粘进姓名里
        #（实测认出过「人孙本阁」）；手机号右边不找 —— 那边是「去发货」按钮，
        # 词框按 y 分组合行时会并进同一行，实测把按钮文案当成了姓名。
        label_words = [
            w
            for w in words
            if any(tag in str(w.get("text") or "") for tag in ("收货", "收件", "买家"))
            and w["left"] + w["width"] <= phone_left * factor
        ]
        start = (
            max(w["left"] + w["width"] for w in label_words) // factor
            if label_words
            else line_left
        )
        name = _name_in_patch(path, (start - 6, top - 6, max(phone_left - 4, start + 8), bottom + 6))
        return name, phone
    return "", ""


def _phone_in_patch(path: Path, box: tuple[int, int, int, int]) -> str:
    picked: list[str] = []
    for threshold in _THRESHOLDS:
        text = _crop_ocr(
            path, box, scale=4, psm="7", lang="eng", whitelist="0123456789", threshold=threshold
        )
        found = _PHONE_RE.search(re.sub(r"\s+", "", text or ""))
        if found:
            picked.append(found.group(0))
    return _vote(picked)


def _name_in_patch(path: Path, box: tuple[int, int, int, int]) -> str:
    """多阈值认几遍，投票取最像中文姓名的一串汉字（至少两档一致才采信）。"""
    picked: list[str] = []
    for threshold in _THRESHOLDS:
        text = _crop_ocr(path, box, scale=4, psm="7", lang="chi_sim", threshold=threshold)
        for token in re.findall(r"[\u4e00-\u9fa5]{2,6}", text or ""):
            if any(word in token for word in _NAME_STOPWORDS):
                continue
            picked.append(_strip_noise(token))
    return _vote(picked, min_votes=2)


def name_from_text(text: str) -> str:
    """整图文本里的收货人。

    两种写法：① 带标签的「收货人：张三」；② 姓名和手机号粘成一行的
    「冰本研15022405112」—— 闲鱼截图上这条更常见，标签常常没被认出来。
    「买家」不算标签：订单状态行「买家已付款，请尽快发货」会被它当成姓名。
    """
    for line in (text or "").splitlines():
        found = re.search(r"(?:收货人|收件人)\s*[:：]?\s*([^\s\d，。、:：]{2,6})", line)
        if found:
            return found.group(1).strip()
    glued = re.search(r"([\u4e00-\u9fa5]{2,6})\s*1[3-9]\d{9}", re.sub(r"\s+", "", text or ""))
    return glued.group(1) if glued else ""


def _name_need_fix(current: str, roi_name: str) -> tuple[bool, str]:
    """整图姓名要不要纠正：只有「不像姓名」时才动（认不出、或混进了字母数字）。"""
    if not roi_name:
        return False, ""
    if not current:
        return True, "整图没认出收货人"
    if _NAME_RE.match(current):
        return False, ""
    return True, f"整图收货人「{current}」不像姓名"


# ---------------------------------------------------------------- 接回文本
def augment(path: str | Path, text: str) -> tuple[str, list[str]]:
    """把 ROI 认出的值前置到文本头部，返回 (新文本, 做了什么)。

    前置而不是追加：解析层 re.search 取首个命中、extract_kv_lines 取首个
    同名标签，追加在末尾不会生效。
    """
    if not _enabled():
        return text, []
    path = Path(path)
    prefix: list[str] = []
    notes: list[str] = []
    # 先判后跑：整图已经认对了的字段不再跑 ROI（实测正常单能省掉一半耗时）
    values = money_from_text(text)
    if not _money_consistent(values):
        try:
            money = money_roi(path)
            if money:
                roi_value = money.get("商品总价") or money.get("成交价")
                if roi_value is None:
                    roi_value = next(iter(money.values()), None)
                need, why = _money_need_fix(values, roi_value)
                if need and roi_value is not None:
                    # 成交价与商品总价在闲鱼是同一个数，两个标签都写，解析层取哪个都对
                    prefix.append(f"成交价 ¥{roi_value:.2f}")
                    prefix.append(f"商品总价 ¥{roi_value:.2f}")
                    notes.append(f"金额（ROI 已纠正，{why}）：¥{roi_value:.2f}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("金额 ROI 失败: %s", exc)
    # 姓名默认不跑：实测 12 张真实截图里 ROI 一个都没纠正对，而填错的姓名比
    # 留空更危险（留空会进「待核对」让运营补，填错可能照着错的发出去）。
    # 手机号照补：整图没认出手机号时才跑，召回高、风险低。
    current_name = name_from_text(text)
    want_name = bool(getattr(settings, "ocr_roi_name", False)) and not _NAME_RE.match(current_name)
    need_phone = not _PHONE_RE.search(text or "")
    if want_name or need_phone:
        try:
            name, phone = contact_roi(path)
            if want_name and name:
                need, why = _name_need_fix(current_name, name)
                if need:
                    prefix.append(f"收货人 {name}")
                    notes.append(f"收货人（ROI 已纠正，{why}）：{name}")
            if need_phone and phone:
                prefix.append(f"手机号 {phone}")
                notes.append(f"手机号（ROI 补缺）：{phone}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("收货人 ROI 失败: %s", exc)
    if not prefix:
        return text, []
    merged = "\n".join(prefix)
    return (f"{merged}\n{text}" if text else merged), notes
