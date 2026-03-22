"""
Meridian v8.2 — email_servicev8.2.py
Resend-backed transactional emails: welcome, verify, password reset, alerts.
"""
import os
import hmac
import hashlib
import time
import httpx

RESEND_API_KEY  = os.getenv("RESEND_API_KEY", "")
FROM_ADDRESS    = os.getenv("EMAIL_FROM", "Meridian <noreply@meridian.app>")
APP_URL         = os.getenv("APP_URL", "https://meridian.app")
EMAIL_SECRET    = os.getenv("SECRET_KEY", "dev-secret")   # reuse app secret for HMAC tokens


# ─── TOKEN HELPERS ────────────────────────────────────────────────────────

def _make_token(user_id: str, purpose: str, ttl_hours: int = 24) -> str:
    """HMAC-signed token: {user_id}.{expiry}.{sig}"""
    expiry = int(time.time()) + ttl_hours * 3600
    payload = f"{user_id}.{purpose}.{expiry}"
    sig = hmac.new(EMAIL_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{user_id}.{expiry}.{sig}"


def verify_token(token: str, user_id: str, purpose: str) -> bool:
    """Returns True if token is valid and not expired."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return False
        tid, expiry, sig = parts
        if tid != user_id:
            return False
        if int(expiry) < int(time.time()):
            return False
        expected = _make_token(user_id, purpose, ttl_hours=0)
        # Reconstruct with exact expiry
        payload = f"{user_id}.{purpose}.{expiry}"
        expected_sig = hmac.new(EMAIL_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        return hmac.compare_digest(sig, expected_sig)
    except Exception:
        return False


def make_verify_token(user_id: str) -> str:
    return _make_token(user_id, "verify", ttl_hours=48)


def make_reset_token(user_id: str) -> str:
    return _make_token(user_id, "reset", ttl_hours=1)


# ─── SEND HELPERS ─────────────────────────────────────────────────────────

def _send(to: str, subject: str, html: str) -> bool:
    """Send via Resend. Returns True on success."""
    if not RESEND_API_KEY:
        print(f"[email] RESEND_API_KEY not set — would send '{subject}' to {to}")
        return False
    try:
        r = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
            json={"from": FROM_ADDRESS, "to": [to], "subject": subject, "html": html},
            timeout=8.0,
        )
        if r.status_code not in (200, 201):
            print(f"[email] Resend error {r.status_code}: {r.text[:200]}")
            return False
        return True
    except Exception as e:
        print(f"[email] Send failed: {e}")
        return False


# ─── TRANSACTIONAL EMAILS ─────────────────────────────────────────────────

def send_verification(to: str, user_id: str, name: str = "") -> bool:
    token = make_verify_token(user_id)
    link  = f"{APP_URL}/api/auth/verify-email?token={token}&user_id={user_id}"
    first = name.split()[0] if name else "there"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#1a1a1a">
      <h2 style="font-size:20px;margin-bottom:8px">Verify your email</h2>
      <p style="color:#555;line-height:1.6">Hi {first}, click below to verify your Meridian account.</p>
      <a href="{link}" style="display:inline-block;margin:20px 0;padding:12px 24px;background:#e8a838;color:#1a1a1a;text-decoration:none;border-radius:6px;font-weight:600">Verify email</a>
      <p style="color:#999;font-size:12px">Link expires in 48 hours. If you didn't sign up, ignore this.</p>
    </div>"""
    return _send(to, "Verify your Meridian account", html)


def send_password_reset(to: str, user_id: str) -> bool:
    token = make_reset_token(user_id)
    link  = f"{APP_URL}/reset-password?token={token}&user_id={user_id}"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#1a1a1a">
      <h2 style="font-size:20px;margin-bottom:8px">Reset your password</h2>
      <p style="color:#555;line-height:1.6">Click below to set a new password. This link expires in 1 hour.</p>
      <a href="{link}" style="display:inline-block;margin:20px 0;padding:12px 24px;background:#e8a838;color:#1a1a1a;text-decoration:none;border-radius:6px;font-weight:600">Reset password</a>
      <p style="color:#999;font-size:12px">If you didn't request this, ignore this email.</p>
    </div>"""
    return _send(to, "Reset your Meridian password", html)


def send_wallet_low_alert(to: str, workspace_name: str, balance: float, runway_days: int) -> bool:
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#1a1a1a">
      <h2 style="font-size:20px;margin-bottom:8px;color:#e05040">⚠ Wallet balance low</h2>
      <p style="color:#555;line-height:1.6">
        Your Meridian wallet for <strong>{workspace_name}</strong> is at
        <strong>${balance:.2f}</strong> — approximately {runway_days} days of runway
        at current burn rate.
      </p>
      <a href="{APP_URL}/app" style="display:inline-block;margin:20px 0;padding:12px 24px;background:#e8a838;color:#1a1a1a;text-decoration:none;border-radius:6px;font-weight:600">Add funds →</a>
    </div>"""
    return _send(to, f"[Meridian] Wallet balance low: ${balance:.2f}", html)


def send_budget_breach_alert(to: str, workspace_name: str, feature_name: str, spent: float, limit: float) -> bool:
    pct = int(spent / limit * 100) if limit else 0
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#1a1a1a">
      <h2 style="font-size:20px;margin-bottom:8px;color:#e05040">Budget breached: {feature_name}</h2>
      <p style="color:#555;line-height:1.6">
        <strong>{workspace_name}</strong> — <strong>{feature_name}</strong> has spent
        <strong>${spent:.2f}</strong> ({pct}%) of its ${limit:.2f} monthly cap.
      </p>
      <a href="{APP_URL}/app" style="display:inline-block;margin:20px 0;padding:12px 24px;background:#e8a838;color:#1a1a1a;text-decoration:none;border-radius:6px;font-weight:600">Review budgets →</a>
    </div>"""
    return _send(to, f"[Meridian] Budget breached: {feature_name} at {pct}%", html)


def send_welcome(to: str, name: str, workspace_id: str) -> bool:
    first = name.split()[0] if name else "there"
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:520px;margin:40px auto;color:#1a1a1a">
      <h2 style="font-size:20px;margin-bottom:8px">Welcome to Meridian, {first}</h2>
      <p style="color:#555;line-height:1.6">
        Your workspace is set up. To start tracking AI margins, install the SDK and
        wrap your first LLM client — it takes about 2 minutes.
      </p>
      <a href="{APP_URL}/app" style="display:inline-block;margin:20px 0;padding:12px 24px;background:#e8a838;color:#1a1a1a;text-decoration:none;border-radius:6px;font-weight:600">Open dashboard →</a>
      <pre style="background:#f5f5f5;padding:16px;border-radius:6px;font-size:12px;color:#333">pip install meridian-sdk

from meridian import MeridianProxy
client = MeridianProxy(Anthropic(), feature="ai-chat")</pre>
    </div>"""
    return _send(to, "Welcome to Meridian — set up your first feature", html)
