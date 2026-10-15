from __future__ import annotations

import hashlib
import hmac
import json
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import tests
from app.models import Base, IDENTITY_STUDENT, User
from app.portals import PORTAL_SELF
from src.deps import get_db
from src.external_identity.router import router
from src.models.external_identity import ExternalIdentity


class ExternalIdentitySyncTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.secret = "test-secret"
        self.app = FastAPI()
        self.app.include_router(router)
        self.app.dependency_overrides[get_db] = self.get_db
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.engine.dispose()

    def get_db(self):
        db = self.Session()
        try:
            yield db
        finally:
            db.close()

    def post_sync(self, payload):
        body = json.dumps(payload, separators=(",", ":")).encode()
        timestamp = str(int(time.time()))
        nonce = "test-nonce"
        digest = hashlib.sha256(body).hexdigest()
        message = "\n".join(("POST", "/openapi/v1/identities/xh-fish/sync", timestamp, nonce, digest))
        signature = hmac.new(self.secret.encode(), message.encode(), hashlib.sha256).hexdigest()
        with patch("src.external_identity.router.get_settings") as settings:
            settings.return_value.xianmou_identity_sync_secret = self.secret
            return self.client.post(
                "/openapi/v1/identities/xh-fish/sync",
                content=body,
                headers={
                    "Content-Type": "application/json",
                    "X-Client-Id": "xh-fish",
                    "X-Timestamp": timestamp,
                    "X-Nonce": nonce,
                    "X-Signature": signature,
                },
            )

    def test_unmatched_phone_auto_registers_platform_user(self):
        """第一次同步时若手机号未匹配任何平台账号，应自动开号并绑定。"""
        response = self.post_sync({
            "external_user_id": "user-1",
            "phone": "13800000001",
            "display_name": "测试用户",
            "source_role": "account",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["linked"])
        with self.Session() as db:
            users = db.scalars(select(User)).all()
            self.assertEqual(len(users), 1)
            user = users[0]
            self.assertEqual(user.phone, "13800000001")
            self.assertEqual(user.display_name, "测试用户")
            self.assertEqual(user.role, "user")
            self.assertEqual(user.status, "active")
            self.assertEqual(user.portals, PORTAL_SELF)
            self.assertEqual(user.identity, IDENTITY_STUDENT)
            self.assertEqual(user.password_hash, "")
            self.assertTrue(user.username.startswith("xhm_13800000001"))
            identity = db.scalar(select(ExternalIdentity))
            self.assertIsNotNone(identity)
            self.assertEqual(identity.platform_user_id, user.id)
            self.assertEqual(identity.source_role, "account")

    def test_unmatched_phone_admin_role_keeps_admin_privilege(self):
        response = self.post_sync({
            "external_user_id": "user-admin",
            "phone": "13800000010",
            "display_name": "管理员",
            "source_role": "admin",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["linked"])
        with self.Session() as db:
            user = db.scalar(select(User))
            self.assertEqual(user.role, "admin")

    def test_sync_without_phone_still_auto_registers_user(self):
        """闲参谋未把手机号传过来时也要能完成自动开号。"""
        response = self.post_sync({
            "external_user_id": "user-nophone",
            "phone": None,
            "display_name": "无手机号用户",
            "source_role": "account",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["linked"])
        with self.Session() as db:
            user = db.scalar(select(User))
            self.assertIsNone(user.phone)
            self.assertEqual(user.display_name, "无手机号用户")
            self.assertTrue(user.username.startswith("xhm_user-nophone"))
            identity = db.scalar(select(ExternalIdentity))
            self.assertEqual(identity.platform_user_id, user.id)

    def test_auto_registration_is_idempotent_and_does_not_change_role(self):
        first = self.post_sync({
            "external_user_id": "user-idem",
            "phone": "13800000020",
            "display_name": "首次",
            "source_role": "account",
        })
        # 后续用 admin 角色同步：已存在的子账号不会被提权
        second = self.post_sync({
            "external_user_id": "user-idem",
            "phone": "13800000020",
            "display_name": "首次",
            "source_role": "admin",
        })
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        with self.Session() as db:
            self.assertEqual(len(db.scalars(select(User)).all()), 1)
            self.assertEqual(len(db.scalars(select(ExternalIdentity)).all()), 1)
            user = db.scalar(select(User))
            self.assertEqual(user.role, "user")

    def test_phone_match_links_existing_user_and_is_idempotent(self):
        with self.Session() as db:
            db.add(User(username="existing", phone="13800000002", display_name="已有用户", password_hash=""))
            db.commit()
        payload = {"external_user_id": "user-2", "phone": "13800000002", "display_name": "用户", "source_role": "admin"}
        first = self.post_sync(payload)
        second = self.post_sync(payload)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertTrue(first.json()["linked"])
        with self.Session() as db:
            self.assertEqual(len(db.scalars(select(ExternalIdentity)).all()), 1)
            self.assertEqual(len(db.scalars(select(User)).all()), 1)
