"""Service-to-service identity synchronization endpoint."""
from __future__ import annotations

import hashlib
import hmac
import re
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.config import get_settings
from src.deps import get_db
from src.external_identity.service import IdentityConflict, sync_xianmou_identity

router = APIRouter(prefix="/openapi/v1/identities/xh-fish", tags=["openapi"])
_PHONE_RE = re.compile(r"^\+?[0-9]{6,20}$")
_MAX_CLOCK_SKEW_SECONDS = 300


class IdentitySyncRequest(BaseModel):
    external_user_id: str = Field(min_length=1, max_length=128)
    phone: str | None = Field(default=None, max_length=20)
    display_name: str = Field(default="", max_length=64)
    source_role: str = Field(min_length=1, max_length=32)


def _verify_signature(
    request: Request,
    client_id: str | None,
    timestamp: str | None,
    nonce: str | None,
    signature: str | None,
    secret: str,
    raw_body: bytes,
    signed_path: str | None = None,
) -> None:
    if not secret:
        raise HTTPException(status_code=503, detail="identity sync is not configured")
    if not all((client_id, timestamp, nonce, signature)) or client_id != "xh-fish":
        raise HTTPException(status_code=401, detail="invalid service credentials")
    if len(nonce) > 128 or len(signature) != 64:
        raise HTTPException(status_code=401, detail="invalid service credentials")
    try:
        issued_at = int(timestamp)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="invalid service credentials") from None
    if abs(int(time.time()) - issued_at) > _MAX_CLOCK_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="expired request")

    body_digest = hashlib.sha256(raw_body).hexdigest()
    path = signed_path or request.url.path
    message = "\n".join((request.method.upper(), path, timestamp, nonce, body_digest))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise HTTPException(status_code=401, detail="invalid service credentials")


@router.post("/sync")
async def sync_identity(
    payload: IdentitySyncRequest,
    request: Request,
    db: Session = Depends(get_db),
    x_client_id: str | None = Header(default=None),
    x_timestamp: str | None = Header(default=None),
    x_nonce: str | None = Header(default=None),
    x_signature: str | None = Header(default=None),
):
    raw_body = await request.body()
    _verify_signature(
        request,
        x_client_id,
        x_timestamp,
        x_nonce,
        x_signature,
        get_settings().xianmou_identity_sync_secret,
        raw_body,
    )
    if payload.phone and not _PHONE_RE.fullmatch(payload.phone):
        raise HTTPException(status_code=422, detail="invalid phone")
    try:
        identity = sync_xianmou_identity(
            db,
            external_user_id=payload.external_user_id,
            phone=payload.phone,
            display_name=payload.display_name.strip(),
            source_role=payload.source_role,
        )
        db.commit()
    except IdentityConflict as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise

    return {
        "external_identity_id": identity.id,
        "linked": identity.platform_user_id is not None,
    }
