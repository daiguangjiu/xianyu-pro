"""JSON API：解析测试、手动同步、健康检查。"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..models import Order, RawMessage, SyncState
from ..timeutil import fmt
from ..parser.engine import parse_text
from ..parser.rules import get_rules
from ..services import logistics_trace
from ..services import voice as voice_service
from ..services.ingest import sync_chat

router = APIRouter(prefix="/api")


class ParseTestIn(BaseModel):
    text: str = ""
    platform: str = ""
    ocr_text: str = ""


class SyncIn(BaseModel):
    chat_ids: list[str] | None = None
    days: int = 1


def _resolve_chat_ids(session: Session, wanted: list[str] | None) -> list[str]:
    if wanted:
        return wanted
    ids = list(settings.feishu_chat_ids)
    for row in session.scalars(select(SyncState)).all():
        if row.chat_id and row.chat_id not in ids:
            ids.append(row.chat_id)
    return ids


@router.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    return {
        "ok": True,
        "feishu": settings.feishu_enabled,
        "orders": db.scalar(select(Order.id).limit(1)) is not None,
        "pending_messages": db.scalar(
            select(RawMessage.id).where(RawMessage.parse_status == "pending").limit(1)
        )
        is not None,
    }


@router.get("/logistics/trace")
def logistics_trace_api(
    mail_no: str = "",
    phone: str = "",
    force: int = 0,
    db: Session = Depends(get_db),
) -> dict:
    """物流轨迹查询（三端共用，也对外开放）。

    走的是「物流轨迹服务」：查完在本地留一份存档，一小时之内同一单号不再调菜鸟
    （``force=1`` 才是强制实时查）。将来要把这个服务拆成独立进程，调用方换这个
    HTTP 地址即可，业务代码不用动。
    """
    return logistics_trace.lookup(db, mail_no, phone=phone, force=force == 1)


@router.get("/voice/pending")
def voice_pending(since: int = 0, limit: int = 20, db: Session = Depends(get_db)) -> dict:
    """语音播报：取比 since 新的播报事件，供浏览器朗读。

    同时下发播报参数（音量 / 语速 / 音调 / 语言），配置在「系统设置」页改完即时生效。
    """
    events = voice_service.pending(db, since_id=since, limit=limit) if settings.voice_enabled else []
    return {
        "ok": True,
        "enabled": bool(settings.voice_enabled),
        "config": voice_service.payload(),
        "events": events,
        "latest_id": voice_service.latest_id(db),
    }


@router.post("/parse-test")
def parse_test(payload: ParseTestIn) -> dict[str, Any]:
    rules = get_rules()
    result = parse_text(
        payload.text,
        [payload.ocr_text] if payload.ocr_text else None,
        rules=rules,
        platform_key=payload.platform or None,
    )
    if not result.matched:
        raise HTTPException(400, "没有匹配到任何平台规则")
    fields = {}
    for key, value in result.fields.items():
        fields[key] = fmt(value) if hasattr(value, "strftime") else value
    return {
        "platform": result.platform,
        "platform_label": result.platform_label,
        "fields": fields,
        "sources": result.sources,
        "missing_required": result.missing_required,
        "confidence": result.confidence,
        "needs_review": result.needs_review,
    }


@router.post("/sync")
def sync(payload: SyncIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    ids = _resolve_chat_ids(db, payload.chat_ids)
    if not ids:
        raise HTTPException(400, "未配置任何群 chat_id，请先到「系统设置」添加")
    days = max(int(payload.days or 1), 1)
    start_ms = int((time.time() - days * 86400) * 1000)
    results = []
    for chat_id in ids:
        try:
            results.append(sync_chat(chat_id, start_ms=start_ms).as_dict())
        except Exception as exc:  # noqa: BLE001
            results.append({"chat_id": chat_id, "error": str(exc)})
    return {"ok": True, "results": results}
