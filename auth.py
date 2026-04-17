"""Authentication helpers: password hashing, session management."""
import os
from typing import Optional
import bcrypt
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException
import aiosqlite

SECRET_KEY = os.getenv("SECRET_KEY", "change-me-in-production-please")
SESSION_COOKIE = "hr_intel_session"
MAX_AGE = 60 * 60 * 24 * 7  # 7 days

serializer = URLSafeTimedSerializer(SECRET_KEY)


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("utf-8"))


def create_session_token(user_id: int) -> str:
    return serializer.dumps({"user_id": user_id})


def decode_session_token(token: str) -> Optional[dict]:
    try:
        return serializer.loads(token, max_age=MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None


async def get_current_user(request: Request, db: aiosqlite.Connection) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    data = decode_session_token(token)
    if not data:
        return None
    user_id = data.get("user_id")
    async with db.execute(
        "SELECT id, email, name FROM users WHERE id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
        if row:
            return {"id": row[0], "email": row[1], "name": row[2]}
    return None


async def require_user(request: Request, db: aiosqlite.Connection) -> dict:
    user = await get_current_user(request, db)
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user


async def login_user(db: aiosqlite.Connection, email: str, password: str) -> Optional[dict]:
    async with db.execute(
        "SELECT id, email, name, password_hash FROM users WHERE email = ?", (email.lower(),)
    ) as cursor:
        row = await cursor.fetchone()
    if not row:
        return None
    if not verify_password(password, row[3]):
        return None
    return {"id": row[0], "email": row[1], "name": row[2]}


async def register_user(db: aiosqlite.Connection, email: str, password: str, name: str) -> Optional[dict]:
    import aiosqlite as _aio
    try:
        hashed = hash_password(password)
        cursor = await db.execute(
            "INSERT INTO users (email, password_hash, name) VALUES (?, ?, ?)",
            (email.lower(), hashed, name),
        )
        await db.commit()
        return {"id": cursor.lastrowid, "email": email.lower(), "name": name}
    except _aio.IntegrityError:
        return None  # email already exists
