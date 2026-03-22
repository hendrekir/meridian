"""
Meridian — encryption.py v8.1
Symmetric encryption for provider API keys stored in the DB.
Uses Fernet (AES-128-CBC + HMAC-SHA256) via the cryptography package.

ENCRYPTION_KEY env var must be a URL-safe base64 32-byte key.
Generate one with:  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os
import base64
from cryptography.fernet import Fernet, InvalidToken

_raw_key = os.getenv("ENCRYPTION_KEY", "")

def _get_fernet() -> Fernet:
    if _raw_key:
        try:
            return Fernet(_raw_key.encode())
        except Exception:
            pass
    # Dev fallback: derive a stable key from SECRET_KEY so dev works without ENCRYPTION_KEY
    secret = os.getenv("SECRET_KEY", "dev-secret-please-change")
    derived = base64.urlsafe_b64encode(secret.encode().ljust(32)[:32])
    return Fernet(derived)


def encrypt(plain: str) -> str:
    """Encrypt a plaintext string, return base64 ciphertext."""
    if not plain:
        return ""
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt(ciphertext: str) -> str:
    """Decrypt a ciphertext string back to plaintext."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return ""
