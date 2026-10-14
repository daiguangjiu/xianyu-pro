"""语音播报：后台事件 -> 浏览器朗读。

业务口径（2026-09）：
  1 群里同步到新订单时播报「你有一笔订单要处理，请实际处理！」；
  2 供应商回单物流更新（维护/修改物流单号）时播报
    「你有一笔订单供应商已发货，请及时完成店铺发货操作！」；
  3 开关、音量、语速、音调、语言与两条文案都在系统设置页配置，写入 .env 即时生效。

服务端只负责「按当时配置渲染好文案并落一条事件」，朗读由浏览器 Web Speech 完成，
不依赖任何第三方语音服务；页面登录后轮询 /api/voice/pending 取未播报的事件。
"""
from __future__ import annotations

import logging
import json
import os
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select

from ..config import settings
from ..models import Order, VoiceEvent
from ..timeutil import fmt, now
from ..utils import media_url

logger = logging.getLogger(__name__)

# 事件种类 -> 默认文案（配置里的文案为空时兜底）
KIND_ORDER = "order"
KIND_TRACKING = "tracking"
KIND_REFUND = "refund"

DEFAULT_TEXTS = {
    KIND_ORDER: "你有一笔订单要处理，请实际处理！",
    KIND_TRACKING: "你有一笔订单供应商已发货，请及时完成店铺发货操作！",
    KIND_REFUND: "有退单订单请及时处理拦截！",
}

# 播报事件保留时长：只服务于「实时提醒」，过期即清，避免表无限增长
KEEP_HOURS = 24

# —— 自定义录音（自己录的提示音）——
# 存在 media/voice 下（docker 部署时 media 是数据卷，重建容器不丢）
CLIP_DIR_NAME = "voice"
CLIP_EXT_OK = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".webm", ".flac")
CLIP_MAX_BYTES = 10 * 1024 * 1024
# 三类播报 -> 对应的配置项
CLIP_KEYS = {
    KIND_ORDER: "voice_clip_order",
    KIND_TRACKING: "voice_clip_tracking",
    KIND_REFUND: "voice_clip_refund",
}
# —— 系统自带默认人声 ——
# 随安装包附带的三段提示音（中文女声），没自定义录音时播它，
# 播不出来（浏览器限制 / 文件缺失）再回落到浏览器合成音
DEFAULT_CLIP_FILES = {
    KIND_ORDER: "default-order.mp3",
    KIND_TRACKING: "default-tracking.mp3",
    KIND_REFUND: "default-refund.mp3",
}
STATIC_VOICE_DIR = Path(__file__).resolve().parents[1] / "static" / "voice"

# 设置页展示用的场景名
CLIP_LABELS = {
    KIND_ORDER: "新订单播报",
    KIND_TRACKING: "供应商已发货播报",
    KIND_REFUND: "退单拦截播报",
}


def clip_dir() -> Path:
    """自定义录音存放目录（位于 media 下，可直接通过 /media/voice/xxx.mp3 访问）。"""
    d = settings.media_path / CLIP_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def clip_of(kind: str) -> str:
    """该类型当前配置的录音文件名（没配返回空串）。"""
    key = CLIP_KEYS.get(kind)
    return (getattr(settings, key, "") if key else "") or ""


def clip_url(kind: str) -> str:
    """给前端播放用的地址；没配录音返回空串。"""
    name = clip_of(kind)
    if not name:
        return ""
    path = clip_dir() / Path(name).name  # 防目录穿越：只取文件名
    if not path.exists():
        return ""
    return media_url(str(path))


def set_clip(kind: str, filename: str) -> None:
    """把录音文件名写进运行时配置（持久化由调用方写 .env 完成）。"""
    key = CLIP_KEYS.get(kind)
    if key:
        setattr(settings, key, filename or "")


def _recorded_map(kind: str) -> dict[str, str]:
    """该场景已录好的声音：{文案: 文件名}。

    录音是固定一句话：只有配置的文案和念的一字不差时才播录音，改过文案就回落合成音，
    免得听到的是另一句话。一个场景可以录多个版本（manifest.json 里并列）。
    """
    try:
        data = json.loads((STATIC_VOICE_DIR / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    value = data.get(kind)
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items() if k and v}
    name = DEFAULT_CLIP_FILES.get(kind)
    return {DEFAULT_TEXTS[kind]: name} if (value and isinstance(value, str) and name) else {}


def default_clip_url(kind: str) -> str:
    """系统自带默认人声的地址；以下情况不用它（回落到浏览器合成音）：

    1 管理员在系统设置里关掉了「系统默认人声」；
    2 该场景的播报文案和录音里念的不一致（录音是固定一句话，读不了改过的文案）；
    3 声音文件缺失。
    """
    if not settings.voice_default_clip:
        return ""
    name = _recorded_map(kind).get((template_of(kind) or "").strip())
    if not name:
        return ""
    path = STATIC_VOICE_DIR / Path(name).name
    if not path.exists():
        return ""
    try:
        stamp = int(path.stat().st_mtime)
    except OSError:
        return f"/static/voice/{Path(name).name}"
    return f"/static/voice/{Path(name).name}?v={stamp}"


def env_key(kind: str) -> str:
    return {
        KIND_ORDER: "VOICE_CLIP_ORDER",
        KIND_TRACKING: "VOICE_CLIP_TRACKING",
        KIND_REFUND: "VOICE_CLIP_REFUND",
    }.get(kind, "")


def save_clip(kind: str, upload_name: str, data: bytes) -> tuple[str, str]:
    """保存上传的录音，返回 (文件名, 错误信息)；同一场景保留最新一个，旧的自动清掉。"""
    key = CLIP_KEYS.get(kind)
    if not key:
        return "", "未知的播报场景"
    ext = os.path.splitext(str(upload_name or ""))[1].lower()
    if ext not in CLIP_EXT_OK:
        return "", f"只支持 {'/'.join(e.lstrip('.') for e in CLIP_EXT_OK)} 音频文件"
    if not data:
        return "", "文件是空的，请重新选择"
    if len(data) > CLIP_MAX_BYTES:
        return "", f"文件不能超过 {CLIP_MAX_BYTES // 1024 // 1024}MB"

    old = clip_of(kind)
    # 同一场景固定文件名（替换即可），访问地址自带 mtime 版本号，浏览器不会播缓存的旧录音
    target_name = f"{kind}{ext}"
    target = clip_dir() / target_name
    try:
        target.write_bytes(data)
    except OSError as exc:  # noqa: BLE001
        return "", f"保存失败：{exc}"
    if old and old != target_name:
        try:
            (clip_dir() / Path(old).name).unlink(missing_ok=True)
        except OSError:  # noqa: BLE001  旧文件删不掉不影响新录音生效
            pass
    set_clip(kind, target_name)
    return target_name, ""


def delete_clip(kind: str) -> str:
    """删除某个场景的录音，返回提示文案（回落到合成音）。"""
    old = clip_of(kind)
    set_clip(kind, "")
    if old:
        try:
            (clip_dir() / Path(old).name).unlink(missing_ok=True)
            return "已删除录音，播报回落为浏览器合成音"
        except OSError as exc:  # noqa: BLE001
            return f"录音配置已清空（文件删除失败：{exc}）"
    return "本来就没有配置录音"


def template_of(kind: str) -> str:
    """取该类型的播报文案（配置为空时回落到默认文案）。"""
    if kind == KIND_TRACKING:
        return (settings.voice_tracking_text or "").strip() or DEFAULT_TEXTS[KIND_TRACKING]
    if kind == KIND_REFUND:
        return (settings.voice_refund_text or "").strip() or DEFAULT_TEXTS[KIND_REFUND]
    return (settings.voice_order_text or "").strip() or DEFAULT_TEXTS[KIND_ORDER]


def render(template: str, order: Order | None = None) -> str:
    """渲染播报文案：替换 {订单号} / {产品} / {供应商} / {物流单号} 等占位符。"""
    text = (template or "").strip()
    if not text or "{" not in text:
        return text

    values = {
        "订单号": "",
        "产品": "",
        "产品名称": "",
        "供应商": "",
        "收货人": "",
        "物流单号": "",
        "闲鱼号": "",
        "order_no": "",
        "product": "",
        "supplier": "",
        "receiver": "",
        "tracking_no": "",
    }
    if order is not None:
        values.update(
            {
                "订单号": order.order_no or "",
                "产品": order.product_name or "",
                "产品名称": order.product_name or "",
                "供应商": order.supplier_name or "",
                "收货人": order.receiver_name or "",
                "物流单号": order.tracking_no or "",
                "闲鱼号": order.xianyu_account or "",
                "order_no": order.order_no or "",
                "product": order.product_name or "",
                "supplier": order.supplier_name or "",
                "receiver": order.receiver_name or "",
                "tracking_no": order.tracking_no or "",
            }
        )
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return " ".join(text.split())


def emit(
    session,
    kind: str,
    order: Order | None = None,
    text: str | None = None,
    commit: bool = True,
) -> VoiceEvent | None:
    """落一条播报事件；关闭语音或文案为空时不产生事件。"""
    if not settings.voice_enabled:
        return None
    final = (text or render(template_of(kind), order) or "").strip()
    if not final:
        return None

    event = VoiceEvent(
        kind=kind,
        order_id=order.id if order is not None else None,
        order_no=(order.order_no if order is not None else "") or "",
        text=final,
        created_at=now(),
    )
    session.add(event)
    if commit:
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001  播报失败不能影响主流程
            logger.warning("语音播报事件写入失败: %s", exc)
            session.rollback()
            return None
    return event


def pending(session, since_id: int = 0, limit: int = 20) -> list[dict]:
    """取比 since_id 新的播报事件（按时间正序，保证朗读顺序）。"""
    stmt = (
        select(VoiceEvent)
        .where(VoiceEvent.id > max(int(since_id or 0), 0))
        .order_by(VoiceEvent.id.asc())
        .limit(max(int(limit or 20), 1))
    )
    rows = session.scalars(stmt).all()
    return [
        {
            "id": e.id,
            "kind": e.kind,
            "order_id": e.order_id,
            "order_no": e.order_no,
            "text": e.text,
            "created_at": fmt(e.created_at, "%Y-%m-%d %H:%M:%S") if e.created_at else "",
        }
        for e in rows
    ]


def latest_id(session) -> int:
    return int(session.scalar(select(func.max(VoiceEvent.id))) or 0)


def order_event(session, order: Order) -> VoiceEvent | None:
    """群里同步到新订单。"""
    return emit(session, KIND_ORDER, order)


def tracking_event(session, order: Order) -> VoiceEvent | None:
    """供应商回单物流更新（物流单号被维护/修改）。"""
    return emit(session, KIND_TRACKING, order)


def refund_event(session, order: Order) -> VoiceEvent | None:
    """逆向退单：群里同步了「此单退 / 退单」，需要及时拦截。"""
    return emit(session, KIND_REFUND, order)


def prune(session, keep_hours: int = KEEP_HOURS, commit: bool = True) -> int:
    """清理过期播报事件，返回删除条数。"""
    deadline = now() - timedelta(hours=max(int(keep_hours or 0), 1))
    deleted = session.query(VoiceEvent).filter(VoiceEvent.created_at < deadline).delete()
    if commit:
        try:
            session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("清理语音播报事件失败: %s", exc)
            session.rollback()
            return 0
    return int(deleted or 0)


def payload() -> dict:
    """给前端的播报参数（不含文案，文案随事件下发）。

    clips：三种场景的自定义录音地址，配了就播录音、没配才用 TTS 合成音。
    """
    return {
        "enabled": bool(settings.voice_enabled),
        "volume": round(min(max(settings.voice_volume, 0.0), 1.0), 2),
        "rate": round(min(max(settings.voice_rate, 0.1), 10.0), 2),
        "pitch": round(min(max(settings.voice_pitch, 0.0), 2.0), 2),
        "lang": settings.voice_lang or "zh-CN",
        "voice_name": settings.voice_name or "",
        "repeat": max(int(settings.voice_repeat or 1), 1),
        "clips": {
            KIND_ORDER: clip_url(KIND_ORDER),
            KIND_TRACKING: clip_url(KIND_TRACKING),
            KIND_REFUND: clip_url(KIND_REFUND),
        },
        "default_clips_enabled": bool(settings.voice_default_clip),
        "default_clips": {
            KIND_ORDER: default_clip_url(KIND_ORDER),
            KIND_TRACKING: default_clip_url(KIND_TRACKING),
            KIND_REFUND: default_clip_url(KIND_REFUND),
        },
        "templates": {
            KIND_ORDER: template_of(KIND_ORDER),
            KIND_TRACKING: template_of(KIND_TRACKING),
            KIND_REFUND: template_of(KIND_REFUND),
        },
    }
