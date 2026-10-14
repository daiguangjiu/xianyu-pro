"""人工标记 -> 解析策略：把运营在消息页的人工判断沉淀成可复用的判定规则。

判定分三层，从强到弱：
  1. 样本指纹：人工标记过的同一条消息（内容一致）直接按人工结论处理 ——
     群里重复同步、别人转发同一条通告时稳定生效；
  2. 订单特征词（force）：人工确认「其实是订单」的消息里学到的话术，命中即按订单处理，
     用来补规则漏掉的模板写法（例如只写了「收货人：」「地址信息：」的简版模板）；
  3. 排除词（exclude）：人工确认「不是订单」的消息里学到的话术，命中即过滤。
     排除词只在消息本身不像订单模板（标签行不足）时才生效 —— 否则正常订单的
     备注里出现「大家注意」也会被误杀，宁缺勿错。

学习逻辑：标记一次就学，学到的是「短句 + 提示类关键词」：
  · 短句：把通告按标点切句，取 4~24 字、不含链接的句子（措辞一致时命中）；
  · 提示词：句子里命中「大家注意 / 请注意 / 通知 / 公告 …」这类提示词库时，
    把这个词也收进排除词（同类通告换个说法也能命中）。
排除词与特征词都可以在飞书消息页手动增删，页面上能看到每条策略的来源与操作人。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ParseFeedback, ParsePhrase, RawMessage
from ..parser.engine import extract_kv_lines

logger = logging.getLogger(__name__)

# 人工结论
LABEL_NOT_ORDER = "not_order"
LABEL_ORDER = "order"
LABEL_REFUND = "refund"      # 这条其实是退单（模板消息首行说「此单退」这类的漏判）
LABEL_TEXT = {
    LABEL_NOT_ORDER: "不是订单",
    LABEL_ORDER: "其实是订单",
    LABEL_REFUND: "其实是退单",
}

# 策略短语类型
KIND_EXCLUDE = "exclude"
KIND_FORCE = "force"
KIND_REFUND = "refund"       # 命中即按退单处理：不新建订单，去存量订单里找原单退掉
KIND_LABELS = {KIND_EXCLUDE: "排除词", KIND_FORCE: "订单特征词", KIND_REFUND: "退单话术"}

# 判定结果（""表示没有人工策略命中，交给规则判断）
VERDICT_EXCLUDE = "exclude"
VERDICT_ORDER = "order"
VERDICT_REFUND = "refund"

_NOISE_RE = re.compile(r"[\s，。、！？!?,.;；:：~\-—_()（）\[\]【】\"'“”‘’/\\|*#]+")
_SENTENCE_SPLIT_RE = re.compile(r"[，,。；;！!？?\n\r、]+")
_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_NUMERIC_ONLY_RE = re.compile(r"^[\d\s.\-:：/]+$")
# 「标签: 值」行：学退单话术时要跳过这种行（那是字段，不是话术）
_LABEL_LINE_RE = re.compile(r"[:：]")

# 提示类关键词：通告 / 通知口吻的固定说法，换措辞也会复用，单独收进排除词
PROMPT_WORDS = (
    "大家注意", "请注意", "注意下", "注意一下", "提醒一下", "通知一下", "通知大家",
    "公告", "新规", "禁售", "务必", "切记", "只能发", "不能发", "勿拍", "拒收勿拍",
)

MIN_PHRASE_LEN = 4
MAX_PHRASE_LEN = 24
MAX_LEARNED = 6

# 订单侧只学这些「订单结构标签」当特征：产品 / 商品 / 备注这类词聊天里也会出现，
# 当特征会把通告又判回订单（人工标记的意义就没了）
STRONG_ORDER_LABELS = frozenset(
    (
        "收货人", "收件人", "手机号码", "手机号", "联系电话", "联系方式",
        "地址信息", "收货地址", "收件地址", "详细地址", "地址",
        "售价", "价格", "金额", "订单金额", "成交价", "成本价", "成本",
        "订单号", "订单编号", "交易单号", "数量",
    )
)

# 模板标签本身不能当排除词：把「产品」「收货人」当排除词会把真订单全过滤掉
_TEMPLATE_LABEL_NOISE = frozenset(
    _NOISE_RE.sub("", w).lower()
    for w in (
        "产品", "产品名称", "商品", "名称", "规格", "型号", "售价", "金额", "备注",
        "地址", "地址信息", "时间", "下单时间", "供应商", "数量", "手机号", "手机号码",
        "收货人", "收件人", "成本", "成本价", "运费", "手续费", "订单号", "买家",
    )
)

# 短语表按秒级缓存：判定在解析主流程里被频繁调用，没必要每条消息都查库
_CACHE_TTL = 5.0
_CACHE: dict[str, object] = {"at": 0.0, "force": [], "exclude": [], "refund": []}


def invalidate() -> None:
    """人工增删策略后立刻生效（清掉进程内缓存）。"""
    _CACHE["at"] = 0.0


def normalize(text: str) -> str:
    """归一化：去掉空白与常见标点，便于短语匹配（「大家 注意」也能命中「大家注意」）。"""
    return _NOISE_RE.sub("", str(text or "")).lower()


def fingerprint(text: str) -> str:
    """内容指纹：同一条消息（含转发）认成同一份人工样本。"""
    return hashlib.sha1(normalize(text).encode("utf-8")).hexdigest()[:32]


def learn_phrases(text: str, label: str) -> list[str]:
    """从人工标记的消息里提炼可复用的话术。

    · 不是订单：短句（措辞一致时命中）+ 提示词（同类通告换措辞也能命中）；
    · 其实是订单：消息里的标签词（「收货人」「地址信息」…）—— 这些是订单结构特征；
    · 其实是退单：退单关键词（「此单退」）+ 消息首行的短句 —— 记住「这种说法是要退单」，
      下次带这句话的模板消息直接按退单处理（找原订单退掉，不新建订单）。
    """
    body = str(text or "")
    if not body.strip():
        return []
    out: list[str] = []
    if label == LABEL_ORDER:
        out.extend(w for w in extract_kv_lines(body) if w in STRONG_ORDER_LABELS)
    elif label == LABEL_REFUND:
        from . import intent as intent_service  # 局部导入：只在学退单话术时用到

        hit = intent_service.classify(body, template=True)
        if hit.keyword:
            out.append(hit.keyword)
        first = intent_service.head(body)
        # 首行是一句短话才学（「此单退」）；带冒号的是模板字段行，不算话术
        if 2 <= len(first) <= 12 and not _LABEL_LINE_RE.search(first):
            out.append(first)
    else:
        for part in _SENTENCE_SPLIT_RE.split(body):
            s = part.strip().strip("\"'“”「」 ")
            if not (MIN_PHRASE_LEN <= len(s) <= MAX_PHRASE_LEN):
                continue
            if _URL_RE.search(s) or _NUMERIC_ONLY_RE.match(s):
                continue
            out.append(s)
        out.extend(w for w in PROMPT_WORDS if w in body)

    result: list[str] = []
    for phrase in out:
        phrase = phrase.strip()
        if not phrase or phrase in result:
            continue
        if len(phrase) < 2:
            continue
        # 模板标签不能当排除词（「产品」「收货人」是订单特征，当排除词会把真订单全过滤掉）；
        # 订单侧相反，学的正是这些结构标签，所以这里只过滤排除侧
        if label != LABEL_ORDER and normalize(phrase) in _TEMPLATE_LABEL_NOISE:
            continue
        result.append(phrase)
    return result[:MAX_LEARNED]


def _cached_phrases(session: Session) -> tuple[list[str], list[str], list[str]]:
    """(订单特征词, 排除词, 退单话术)，带秒级缓存。"""
    now_ts = time.monotonic()
    if now_ts - float(_CACHE["at"] or 0.0) < _CACHE_TTL:
        return (
            list(_CACHE["force"]),  # type: ignore[arg-type]
            list(_CACHE["exclude"]),  # type: ignore[arg-type]
            list(_CACHE["refund"]),  # type: ignore[arg-type]
        )
    rows = session.scalars(
        select(ParsePhrase).where(ParsePhrase.enabled.is_(True))
    ).all()
    force = [r.phrase for r in rows if r.kind == KIND_FORCE and r.phrase]
    exclude = [r.phrase for r in rows if r.kind == KIND_EXCLUDE and r.phrase]
    refund = [r.phrase for r in rows if r.kind == KIND_REFUND and r.phrase]
    _CACHE["at"] = now_ts
    _CACHE["force"] = force
    _CACHE["exclude"] = exclude
    _CACHE["refund"] = refund
    return force, exclude, refund


def hit_phrase(session: Session, text: str, kind: str) -> str:
    """返回命中的那条策略话术（没命中返回 ""）。"""
    body = normalize(text)
    if not body:
        return ""
    force, exclude, refund = _cached_phrases(session)
    pool = {KIND_FORCE: force, KIND_EXCLUDE: exclude, KIND_REFUND: refund}.get(kind, [])
    for phrase in pool:
        norm = normalize(phrase)
        if norm and norm in body:
            return phrase
    return ""


def decide(session: Session, text: str, sender_id: str = "") -> tuple[str, str, bool]:
    """按人工策略判定这条消息，返回 (结论, 说明, 是否精确命中人工标记的同一句话)。

    结论：""（没有人工策略命中）| exclude（不是订单）| order（是订单）| refund（是退单）。
    第三项为 True 时表示「运营亲眼标过这一模一样的一条消息」，调用方应无条件采信；
    为 False 表示只是命中了学到的话术（泛化推测），只在消息不像订单模板时才采纳。
    """
    body = normalize(text)
    if not body:
        return "", "", False
    # 1) 人工标记过的同一条消息（重复同步 / 转发）：直接按人工结论走
    fp = fingerprint(text)
    feedback = session.scalar(
        select(ParseFeedback)
        .where(ParseFeedback.fingerprint == fp)
        .order_by(ParseFeedback.id.desc())
    )
    if feedback is not None:
        verdict = {
            LABEL_NOT_ORDER: VERDICT_EXCLUDE,
            LABEL_REFUND: VERDICT_REFUND,
        }.get(feedback.label, VERDICT_ORDER)
        who = feedback.operator or "未记名"
        return (
            verdict,
            f"人工标记过「{LABEL_TEXT.get(feedback.label, feedback.label)}」（{who}）",
            True,
        )
    # 2) 学到 / 手工配置的短语：退单话术最具体，先判；排除词次之；订单特征词最后
    force, exclude, refund = _cached_phrases(session)
    for phrase in refund:
        if normalize(phrase) and normalize(phrase) in body:
            return VERDICT_REFUND, f"命中人工标记的退单话术「{phrase}」", False
    for phrase in exclude:
        if normalize(phrase) and normalize(phrase) in body:
            return VERDICT_EXCLUDE, f"命中人工标记的排除词「{phrase}」", False
    for phrase in force:
        if normalize(phrase) and normalize(phrase) in body:
            return VERDICT_ORDER, f"命中人工确认的订单特征「{phrase}」", False
    return "", "", False


def _add_phrase(
    session: Session, phrase: str, kind: str, *, source: str = "learn", operator: str = ""
) -> ParsePhrase | None:
    """新增一条策略短语（已存在就返回它，不重复建）。"""
    phrase = (phrase or "").strip()
    if not phrase:
        return None
    exists = session.scalar(
        select(ParsePhrase).where(ParsePhrase.kind == kind, ParsePhrase.phrase == phrase)
    )
    if exists is not None:
        return exists
    row = ParsePhrase(phrase=phrase[:64], kind=kind, source=source, operator=operator)
    session.add(row)
    invalidate()
    return row


def add_phrase(
    session: Session, phrase: str, kind: str, *, operator: str = "", commit: bool = True
) -> tuple[bool, str]:
    """手工添加一条策略短语（消息页的「加一条」表单）。"""
    if kind not in (KIND_EXCLUDE, KIND_FORCE, KIND_REFUND):
        return False, "短语类型不对"
    phrase = (phrase or "").strip()
    if not phrase:
        return False, "请填写要说的话术"
    if kind == KIND_EXCLUDE and normalize(phrase) in _TEMPLATE_LABEL_NOISE:
        return False, "模板标签本身不能当排除词（「产品」「收货人」这类），请写更具特征的说法"
    exists = session.scalar(
        select(ParsePhrase).where(ParsePhrase.kind == kind, ParsePhrase.phrase == phrase)
    )
    if exists is not None:
        return False, f"「{phrase}」已经在{KIND_LABELS[kind]}里了"
    _add_phrase(session, phrase, kind, source="manual", operator=operator)
    if commit:
        session.commit()
    return True, f"已添加{KIND_LABELS[kind]}「{phrase}」"


def delete_phrase(session: Session, phrase_id: int, *, commit: bool = True) -> bool:
    row = session.get(ParsePhrase, phrase_id)
    if row is None:
        return False
    session.delete(row)
    invalidate()
    if commit:
        session.commit()
    return True


def delete_feedback(session: Session, feedback_id: int, *, commit: bool = True) -> bool:
    """撤销一次人工标记（样本指纹失效，短语保留可单独删）。"""
    row = session.get(ParseFeedback, feedback_id)
    if row is None:
        return False
    session.delete(row)
    invalidate()
    if commit:
        session.commit()
    return True


def record(
    session: Session,
    msg: RawMessage,
    label: str,
    *,
    operator: str = "",
    order_id: int | None = None,
    note: str = "",
    commit: bool = True,
) -> tuple[ParseFeedback, list[str]]:
    """记录一次人工标记，并把学到的话术写进策略，返回 (标记记录, 学到的话术)。"""
    text = str(getattr(msg, "raw_text", "") or "")
    phrases = learn_phrases(text, label)
    kind = {
        LABEL_NOT_ORDER: KIND_EXCLUDE,
        LABEL_REFUND: KIND_REFUND,
    }.get(label, KIND_FORCE)
    for phrase in phrases:
        _add_phrase(session, phrase, kind, source="learn", operator=operator)
    row = ParseFeedback(
        message_id=str(getattr(msg, "message_id", "") or ""),
        fingerprint=fingerprint(text),
        text=text[:2000],
        label=label,
        sender_id=str(getattr(msg, "sender_id", "") or ""),
        sender_name=str(getattr(msg, "sender_name", "") or ""),
        chat_id=str(getattr(msg, "chat_id", "") or ""),
        order_id=order_id,
        phrases=json.dumps(phrases, ensure_ascii=False),
        operator=operator,
        note=note[:255],
    )
    session.add(row)
    invalidate()
    if commit:
        session.commit()
    logger.info(
        "人工标记「%s」：消息 %s，学到 %s 个话术",
        LABEL_TEXT.get(label, label),
        row.message_id,
        len(phrases),
    )
    return row, phrases


def feedback_phrases(row: ParseFeedback) -> list[str]:
    """标记记录里学到的话术（页面展示用）。"""
    try:
        data = json.loads(row.phrases or "[]")
    except (TypeError, ValueError):
        return []
    return [str(x) for x in data if str(x).strip()]


def recent_feedbacks(session: Session, limit: int = 10) -> list[ParseFeedback]:
    stmt = select(ParseFeedback).order_by(ParseFeedback.id.desc()).limit(max(int(limit), 1))
    return list(session.scalars(stmt).all())


def list_phrases(session: Session, kind: str = "") -> list[ParsePhrase]:
    stmt = select(ParsePhrase).order_by(ParsePhrase.id.desc())
    if kind:
        stmt = stmt.where(ParsePhrase.kind == kind)
    return list(session.scalars(stmt).all())


def stats(session: Session) -> dict:
    """策略页顶部计数：人工标记条数、策略短语条数。"""
    from sqlalchemy import func

    def _count(model, *where) -> int:
        return int(session.scalar(select(func.count(model.id)).where(*where)) or 0)

    return {
        "feedback_not_order": _count(ParseFeedback, ParseFeedback.label == LABEL_NOT_ORDER),
        "feedback_order": _count(ParseFeedback, ParseFeedback.label == LABEL_ORDER),
        "feedback_refund": _count(ParseFeedback, ParseFeedback.label == LABEL_REFUND),
        "phrase_exclude": _count(ParsePhrase, ParsePhrase.kind == KIND_EXCLUDE),
        "phrase_force": _count(ParsePhrase, ParsePhrase.kind == KIND_FORCE),
        "phrase_refund": _count(ParsePhrase, ParsePhrase.kind == KIND_REFUND),
    }
