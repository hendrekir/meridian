"""
Meridian — auth.py v8.3
JWT creation/verification (own tokens + Clerk tokens via JWKS).
Password hashing via bcrypt.
API key generation and lookup.
"""
import os
import secrets
import hashlib
import httpx

from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

SECRET_KEY        = os.getenv("SECRET_KEY", "change-me-in-production-please")
ALGORITHM         = "HS256"
TOKEN_EXPIRE_DAYS = 30

# Clerk JWKS — used to verify tokens Clerk issues to the frontend
CLERK_JWKS_URL = os.getenv(
    "CLERK_JWKS_URL",
    "https://clerk.meridianvisual.io/.well-known/jwks.json",
)

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ── PASSWORD ──────────────────────────────────────────────────────────────

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ── OWN JWT ───────────────────────────────────────────────────────────────

def create_access_token(user_id: str, workspace_id: Optional[str] = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=TOKEN_EXPIRE_DAYS)
    payload = {
        "sub":          user_id,
        "workspace_id": workspace_id,
        "exp":          expire,
        "iss":          "meridian",
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    """
    Try our own JWT first.
    If it fails (wrong issuer / wrong key) try Clerk JWKS verification.
    Raises jwt.InvalidTokenError on total failure.
    """
    # 1. Try own token
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("iss") == "meridian":
            return payload
    except jwt.InvalidTokenError:
        pass

    # 2. Try Clerk token via JWKS
    return _decode_clerk_token(token)


def _decode_clerk_token(token: str) -> dict:
    """Fetch Clerk's JWKS and verify the token signature."""
    try:
        resp = httpx.get(CLERK_JWKS_URL, timeout=5.0)
        resp.raise_for_status()
        jwks = resp.json()
    except Exception as exc:
        raise jwt.InvalidTokenError(f"Could not fetch Clerk JWKS: {exc}") from exc

    # jwt.PyJWKClient is convenient but requires PyJWT >= 2.4 — use manual approach
    try:
        from jwt.algorithms import RSAAlgorithm
        import json as _json

        header = jwt.get_unverified_header(token)
        kid    = header.get("kid")

        key_data = None
        for k in jwks.get("keys", []):
            if k.get("kid") == kid:
                key_data = k
                break

        if key_data is None:
            raise jwt.InvalidTokenError("Clerk JWK kid not found")

        public_key = RSAAlgorithm.from_jwk(_json.dumps(key_data))
        payload    = jwt.decode(
            token,
            public_key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        # Normalize: Clerk uses "sub" as user ID  (same field we use)
        return payload
    except Exception as exc:
        raise jwt.InvalidTokenError(f"Clerk token invalid: {exc}") from exc


# ── USER LOOKUPS ──────────────────────────────────────────────────────────

def get_user_by_email(db: Session, email: str):
    from modelsv8_3 import User
    return db.query(User).filter(User.email == email.lower().strip()).first()


def get_user_by_id(db: Session, user_id: str):
    from modelsv8_3 import User
    return db.query(User).filter(User.id == user_id).first()


def get_user_by_clerk_id(db: Session, clerk_id: str):
    """Look up a user by their Clerk subject ID (stored in clerk_id column)."""
    from modelsv8_3 import User
    return db.query(User).filter(User.clerk_id == clerk_id).first()


# ── API KEYS ──────────────────────────────────────────────────────────────

def generate_api_key():
    """
    Returns (raw_key, key_hash, prefix).
    raw_key is shown ONCE to the user and never stored.
    key_hash is stored in the DB.
    prefix is stored for display (first 8 chars).
    """
    raw    = "mrd_" + secrets.token_urlsafe(32)
    prefix = raw[:12]
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed, prefix


def get_workspace_by_api_key(db: Session, raw_key: str):
    """Returns (Workspace, ApiKey) tuple or None."""
    from modelsv8_3 import ApiKey, Workspace
    from datetime import datetime
    hashed = hashlib.sha256(raw_key.encode()).hexdigest()
    key    = db.query(ApiKey).filter(
        ApiKey.key_hash == hashed,
        ApiKey.is_active == True,
    ).first()
    if not key:
        return None
    key.last_used = datetime.utcnow()
    ws = db.query(Workspace).filter(Workspace.id == key.workspace_id).first()
    return (ws, key) if ws else None
