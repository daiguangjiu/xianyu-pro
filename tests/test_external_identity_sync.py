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
from app.models import Base, User
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

    def test_unmatched_phone_requests_manual_access_without_creating_user(self):
        response = self.post_sync({
            "external_user_id": "user-1",
            "phone": "13800000001",
            "display_name": "测试用户",
            "source_role": "account",
        })
        self.assertEqual(response.status_code, 403)
        body = response.json()
        self.assertFalse(body["linked"])
        self.assertTrue(body["needs_manual_request"])
        self.assertEqual(body["code"], "platform_account_not_found")
        self.assertIn("联系客服专员", body["message"])
        with self.Session() as db:
            self.assertEqual(len(db.scalars(select(User)).all()), 0)
            self.assertEqual(len(db.scalars(select(ExternalIdentity)).all()), 0)

    def test_phone_match_links_existing_platform_user_and_is_idempotent(self):
        with self.Session() as db:
            db.add(User(username="existing", phone="13800000002", display_name="已有用户", password_hash=""))
            db.commit()
        payload = {"external_user_id": "user-2", "phone": "13800000002", "display_name": "用户", "source_role": "account"}
        first = self.post_sync(payload)
        second = self.post_sync(payload)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertTrue(first.json()["linked"])
        self.assertFalse(first.json()["needs_manual_request"])
        with self.Session() as db:
            identities = db.scalars(select(ExternalIdentity)).all()
            users = db.scalars(select(User)).all()
            self.assertEqual(len(identities), 1)
            self.assertEqual(len(users), 1)
            self.assertEqual(identities[0].platform_user_id, users[0].id)

    def test_external_identity_already_linked_keeps_existing_link(self):
        with self.Session() as db:
            user = User(username="existing", phone="13800000003", display_name="已有用户", password_hash="")
            db.add(user)
            db.flush()
            db.add(ExternalIdentity(
                source_system="xh-fish",
                external_user_id="user-3",
                platform_user_id=user.id,
                phone=user.phone,
                display_name=user.display_name,
                source_role="account",
            ))
            db.commit()
        response = self.post_sync({
            "external_user_id": "user-3",
            "phone": "13800000003",
            "display_name": "已有用户",
            "source_role": "account",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["linked"])

    def test_deleted_link_without_matching_user_requires_manual_access(self):
        with self.Session() as db:
            db.add(ExternalIdentity(
                source_system="xh-fish",
                external_user_id="user-4",
                platform_user_id=9999,
                phone="13800000004",
                display_name="旧关联",
                source_role="account",
            ))
            db.commit()
        response = self.post_sync({
            "external_user_id": "user-4",
            "phone": "13800000004",
            "display_name": "旧关联",
            "source_role": "account",
        })
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json()["needs_manual_request"])
        with self.Session() as db:
            self.assertEqual(len(db.scalars(select(User)).all()), 0)
