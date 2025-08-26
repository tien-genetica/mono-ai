from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import pytest
import pytest_asyncio
from httpx import AsyncClient
from httpx import ASGITransport


class FakeDatabase:
    def __init__(self) -> None:
        self.users: Dict[int, Dict[str, Any]] = {}
        self.refresh_tokens: Dict[int, Dict[str, Any]] = {}
        self.otp_codes: Dict[int, Dict[str, Any]] = {}

        self._next_user_id = 1
        self._next_refresh_id = 1
        self._next_otp_id = 1

    async def connect(self):
        return None

    async def disconnect(self):
        return None

    async def init_tables(self):
        return None

    async def execute(self, query: str, *args):
        q = " ".join(query.split()).lower()

        # INSERT refresh token
        if q.startswith("insert into refresh_tokens"):
            user_id, token, expires_at = args
            rid = self._next_refresh_id
            self._next_refresh_id += 1
            self.refresh_tokens[rid] = {
                "id": rid,
                "user_id": int(user_id),
                "token": token,
                "expires_at": expires_at,
                "created_at": datetime.utcnow(),
            }
            return "OK"

        # DELETE refresh token by id
        if q.startswith("delete from refresh_tokens where id"):
            rid = int(args[0])
            if rid in self.refresh_tokens:
                del self.refresh_tokens[rid]
            return "OK"

        # DELETE refresh tokens by user
        if q.startswith("delete from refresh_tokens where user_id"):
            user_id = int(args[0])
            to_delete = [rid for rid, r in self.refresh_tokens.items() if r["user_id"] == user_id]
            for rid in to_delete:
                del self.refresh_tokens[rid]
            return "OK"

        # INSERT OTP
        if q.startswith("insert into otp_codes"):
            user_id, code, typ, purpose, expires_at = args
            oid = self._next_otp_id
            self._next_otp_id += 1
            self.otp_codes[oid] = {
                "id": oid,
                "user_id": int(user_id),
                "code": code,
                "type": typ,
                "purpose": purpose,
                "expires_at": expires_at,
                "used": False,
                "created_at": datetime.utcnow(),
            }
            return "OK"

        # UPDATE OTP used
        if q.startswith("update otp_codes set used = true where id"):
            oid = int(args[0])
            if oid in self.otp_codes:
                self.otp_codes[oid]["used"] = True
            return "OK"

        # UPDATE users email/phone verified
        if q.startswith("update users set email_verified = true where id"):
            uid = int(args[0])
            if uid in self.users:
                self.users[uid]["email_verified"] = True
            return "OK"

        if q.startswith("update users set phone_verified = true where id"):
            uid = int(args[0])
            if uid in self.users:
                self.users[uid]["phone_verified"] = True
            return "OK"

        return "OK"

    async def fetchrow(self, query: str, *args):
        q = " ".join(query.split()).lower()

        # SELECT existing user by email/username/phone
        if q.startswith("select id from users where email"):
            email, username, phone = args
            for u in self.users.values():
                if (
                    (email and u["email"] == email)
                    or (username and u["username"] == username)
                    or (phone and u.get("phone") == phone)
                ):
                    return {"id": u["id"]}
            return None

        # INSERT user RETURNING *
        if q.startswith("insert into users"):
            (
                email,
                phone,
                username,
                password_hash,
                first_name,
                last_name,
                age,
                address,
                city,
                country,
                postal_code,
            ) = args
            uid = self._next_user_id
            self._next_user_id += 1
            user = {
                "id": uid,
                "email": email,
                "phone": phone,
                "username": username,
                "password_hash": password_hash,
                "first_name": first_name,
                "last_name": last_name,
                "age": age,
                "address": address,
                "city": city,
                "country": country,
                "postal_code": postal_code,
                "is_active": True,
                "is_verified": False,
                "email_verified": False,
                "phone_verified": False,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            self.users[uid] = user
            return user

        # SELECT user for login by email
        if q.startswith("select * from users where email"):
            email = args[0]
            for u in self.users.values():
                if u["email"] == email and u["is_active"]:
                    return u
            return None

        # SELECT user for login by phone
        if q.startswith("select * from users where phone"):
            phone = args[0]
            for u in self.users.values():
                if u.get("phone") == phone and u["is_active"]:
                    return u
            return None

        # SELECT user by id (auth)
        if q.startswith("select * from users where id"):
            uid = int(args[0])
            u = self.users.get(uid)
            if u and u["is_active"]:
                return u
            return None

        # SELECT recent valid otp
        if q.startswith("select * from otp_codes"):
            uid, code, typ, now_dt = args[0], args[1], args[2], args[3]
            candidates = [
                o
                for o in self.otp_codes.values()
                if o["user_id"] == int(uid)
                and o["code"] == code
                and o["type"] == typ
                and o["used"] is False
                and o["expires_at"] > now_dt
            ]
            candidates.sort(key=lambda x: x["created_at"], reverse=True)
            return candidates[0] if candidates else None

        # SELECT refresh token by token
        if q.startswith("select * from refresh_tokens where token"):
            token, now_dt = args
            for r in self.refresh_tokens.values():
                if r["token"] == token and r["expires_at"] > now_dt:
                    return r
            return None

        return None

    async def fetch(self, query: str, *args):
        # Not used in tests currently
        return []


@pytest_asyncio.fixture
async def app_and_db(monkeypatch):
    from auth_service import main as main_module
    from auth_service import auth as auth_module
    from auth_service import database as db_module

    fake = FakeDatabase()

    # Patch db singletons in all modules that reference it
    monkeypatch.setattr(db_module, "db", fake, raising=True)
    monkeypatch.setattr(main_module, "db", fake, raising=True)
    monkeypatch.setattr(auth_module, "db", fake, raising=True)

    # Stub side-effect functions imported into main module scope
    async def _ok_email(*args, **kwargs):
        return True

    async def _ok_sms(*args, **kwargs):
        return True

    monkeypatch.setattr(main_module, "send_otp_email", _ok_email, raising=True)
    monkeypatch.setattr(main_module, "send_otp_sms", _ok_sms, raising=True)
    monkeypatch.setattr(main_module, "generate_otp", lambda *a, **k: "123456")

    # Ensure startup hooks don't fail with fake DB
    await main_module.db.connect()
    await main_module.db.init_tables()

    return main_module.app, fake


@pytest_asyncio.fixture
async def client(app_and_db):
    app, _ = app_and_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def fake_db(app_and_db):
    _, fake = app_and_db
    return fake


