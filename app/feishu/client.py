"""飞书开放平台轻量客户端（仅依赖 requests，无需官方 SDK）。"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Iterator

import requests

from ..config import settings

logger = logging.getLogger(__name__)

BASE_URL = "https://open.feishu.cn"


def _to_seconds(ts: int) -> int:
    """飞书 IM 消息列表接口的 start_time/end_time 是**秒级**；兼容调用方传入毫秒。

    实测直接传毫秒会报 [230001] The end_time is earlier than the start_time。
    """
    ts = int(ts)
    return ts // 1000 if ts > 10_000_000_000 else ts


class FeishuError(RuntimeError):
    def __init__(self, code: int, msg: str):
        super().__init__(f"[{code}] {msg}")
        self.code = code
        self.msg = msg


class FeishuClient:
    def __init__(
        self,
        app_id: str | None = None,
        app_secret: str | None = None,
        base_url: str = BASE_URL,
        timeout: int = 15,
    ):
        self.app_id = app_id or settings.feishu_app_id
        self.app_secret = app_secret or settings.feishu_app_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._token = ""
        self._token_expire = 0.0
        self._session = requests.Session()

    # ---------- 基础 ----------
    @property
    def enabled(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def _ensure_token(self, force: bool = False) -> str:
        if self._token and self._token_expire - 60 > time.time() and not force:
            return self._token
        resp = self._session.post(
            f"{self.base_url}/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": self.app_id, "app_secret": self.app_secret},
            timeout=self.timeout,
        )
        data = resp.json()
        if int(data.get("code", -1)) != 0:
            raise FeishuError(int(data.get("code", -1)), data.get("msg", "获取 token 失败"))
        self._token = data["tenant_access_token"]
        self._token_expire = time.time() + int(data.get("expire", 7200))
        return self._token

    def request(
        self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None
    ) -> dict[str, Any]:
        if not self.enabled:
            raise FeishuError(-1, "未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
        token = self._ensure_token()
        resp = self._session.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout,
        )
        data = resp.json()
        code = int(data.get("code", -1))
        if code == 99991663:  # token 失效
            self._ensure_token(force=True)
            raise FeishuError(code, data.get("msg", "token 失效，请重试"))
        if code != 0:
            raise FeishuError(code, data.get("msg", "请求失败"))
        return data.get("data", {}) or {}

    # ---------- 群 ----------
    def get_chat(self, chat_id: str) -> dict[str, Any]:
        return self.request("GET", f"/open-apis/im/v1/chats/{chat_id}")

    def get_chat_name(self, chat_id: str) -> str:
        try:
            return self.get_chat(chat_id).get("name", "") or chat_id
        except Exception:  # noqa: BLE001
            return chat_id

    def get_user_name(self, user_id: str, user_id_type: str = "open_id") -> str:
        """取发送人姓名（需 contact:user.base:readonly 权限）；取不到返回空串。

        取不到不影响入库：页面会退回显示 open_id 短串。
        """
        if not self.enabled or not user_id:
            return ""
        try:
            data = self.request(
                "GET",
                f"/open-apis/contact/v3/users/{user_id}",
                params={"user_id_type": user_id_type},
            )
        except Exception:  # noqa: BLE001
            return ""
        user = data.get("user") or {}
        return str(user.get("name") or "").strip()

    def get_chat_members(self, chat_id: str, page_size: int = 100) -> dict[str, str]:
        """群成员 open_id -> 姓名（需要 im:chat 权限，取不到返回空字典）。

        通讯录接口没权限时，这是拿到「群里员工真实姓名」的备用渠道。
        """
        if not self.enabled or not chat_id:
            return {}
        out: dict[str, str] = {}
        token = ""
        while True:
            params: dict[str, Any] = {
                "member_id_type": "open_id",
                "page_size": min(page_size, 100),
            }
            if token:
                params["page_token"] = token
            try:
                data = self.request(
                    "GET", f"/open-apis/im/v1/chats/{chat_id}/members", params=params
                )
            except Exception:  # noqa: BLE001
                return out
            for item in data.get("items", []) or []:
                member_id = str(item.get("member_id") or "")
                name = str(item.get("name") or "").strip()
                if member_id.startswith("ou_") and name:
                    out[member_id] = name
            if not data.get("has_more"):
                break
            token = data.get("page_token") or ""
            if not token:
                break
        return out

    def list_bot_chats(self, page_size: int = 50) -> list[dict[str, Any]]:
        """列出机器人所在的群（需要 im:chat 权限）。"""
        out: list[dict[str, Any]] = []
        token = ""
        while True:
            params: dict[str, Any] = {"page_size": min(page_size, 100)}
            if token:
                params["page_token"] = token
            data = self.request("GET", "/open-apis/im/v1/chats", params=params)
            out.extend(data.get("items", []) or [])
            if not data.get("has_more"):
                break
            token = data.get("page_token") or ""
            if not token:
                break
        return out

    # ---------- 消息 ----------
    def get_chat_messages(
        self,
        chat_id: str,
        start_time: int | None = None,
        end_time: int | None = None,
        page_size: int = 50,
        max_pages: int = 200,
    ) -> Iterator[dict[str, Any]]:
        """按创建时间正序迭代群消息（需 im:message 权限）。"""
        params: dict[str, Any] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "page_size": min(page_size, 50),
            "sort_type": "ByCreateTimeAsc",
        }
        if start_time:
            params["start_time"] = str(_to_seconds(start_time))
        if end_time:
            params["end_time"] = str(_to_seconds(end_time))

        pages = 0
        page_token = ""
        while pages < max_pages:
            if page_token:
                params["page_token"] = page_token
            data = self.request("GET", "/open-apis/im/v1/messages", params=params)
            for item in data.get("items", []) or []:
                yield item
            pages += 1
            if not data.get("has_more"):
                break
            page_token = data.get("page_token") or ""
            if not page_token:
                break

    def download_resource(
        self, message_id: str, file_key: str, resource_type: str = "image"
    ) -> bytes:
        """下载消息中的图片/文件资源（需 im:resource 权限）。"""
        if not self.enabled:
            raise FeishuError(-1, "未配置飞书应用凭证")
        token = self._ensure_token()
        resp = self._session.get(
            f"{self.base_url}/open-apis/im/v1/messages/{message_id}/resources/{file_key}",
            params={"type": resource_type},
            headers={"Authorization": f"Bearer {token}"},
            timeout=self.timeout * 4,
        )
        ctype = resp.headers.get("Content-Type", "")
        if "application/json" in ctype:
            data = resp.json()
            raise FeishuError(int(data.get("code", -1)), data.get("msg", "下载资源失败"))
        resp.raise_for_status()
        return resp.content

    # ---------- 发送 ----------
    def send_text(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> dict:
        return self.request(
            "POST",
            "/open-apis/im/v1/messages",
            params={"receive_id_type": receive_id_type},
            json_body={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    def reply_text(self, message_id: str, text: str) -> dict:
        """在群内直接回复某条消息（用于「已收到并处理」回执）。"""
        return self.request(
            "POST",
            f"/open-apis/im/v1/messages/{message_id}/reply",
            json_body={
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )


_client_singleton: FeishuClient | None = None


def get_client() -> FeishuClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = FeishuClient()
    return _client_singleton


def reset_client() -> None:
    """凭证在系统设置页被修改后调用：丢弃旧实例（含缓存的 token）。"""
    global _client_singleton
    _client_singleton = None


def save_bytes(data: bytes, filename: str, media_dir: Path | None = None) -> Path:
    root = Path(media_dir) if media_dir else settings.media_path
    day_dir = root / time.strftime("%Y%m")
    day_dir.mkdir(parents=True, exist_ok=True)
    target = day_dir / filename
    target.write_bytes(data)
    return target
