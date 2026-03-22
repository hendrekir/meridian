"""
Meridian API — index.py v8.2
Auth: Clerk JWT (primary) + email/password JWT (fallback)
All protected routes use get_current_user which accepts Bearer JWT from either source.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uuid, secrets, time
from datetime import datetime
from typing import Optional, Annotated

import httpx
from fastapi import FastAPI, Depends, HTTPException, Header, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from databasev8_2 import get_db, create_tables
from modelsv8_2 import (
    User, Workspace, WorkspaceUser, ApiKey, Provider, ProviderType,
    Feature, Customer, Budget, BudgetPolicy, BudgetScope, BudgetPeriod,
    RoutingRule, Wallet, WalletEntry, TransactionType, Alert, BillingConnection,
    BillingSource, WorkspacePlan,
)
from authv8_2 import (
    hash_password, verify_password, decode_token,
    generate_api_key, get_workspace_by_api_key,
    create_access_token, get_user_by_email, get_user_by_id,
    get_user_by_clerk_id,
)
import analyticsv8_2 as analytics
from proxyv8_2 import evaluate_routing_rules, check_wallet, record_transaction, check_and_fire_alerts
from encryptionv8_2 import encrypt, decrypt
import email_servicev8_2 as email_service

# ── APP ───────────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="Meridian API", version="8.2.0")
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

_APP_URL = os.getenv("APP_URL", "http://localhost:8000").rstrip("/")
_EXTRA   = [o.strip() for o in os.getenv("EXTRA_ORIGINS", "").split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        _APP_URL,
        "http://localhost:3000",
        "http://localhost:5173",
        "https://accounts.meridianvisual.io",
        "https://meridianvisual.io",
        "https://www.meridianvisual.io",
    ] + _EXTRA,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Meridian-Key"],
)

# ── STARTUP ───────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    create_tables()
    _seed_pricing()

def _seed_pricing():
    from databasev8_2 import SessionLocal
    from modelsv8_2 import ModelPricing
    db = SessionLocal()
    try:
        if db.query(ModelPricing).count() > 0:
            return
        db.add_all([
            ModelPricing(provider="anthropic", model_id="claude-opus-4-6",          model_display="claude-opus-4",    input_cost_per_1k=0.015,    output_cost_per_1k=0.075,   cache_cost_per_1k=0.0015),
            ModelPricing(provider="anthropic", model_id="claude-sonnet-4-6",         model_display="claude-sonnet-4",  input_cost_per_1k=0.003,    output_cost_per_1k=0.015,   cache_cost_per_1k=0.0003),
            ModelPricing(provider="anthropic", model_id="claude-haiku-4-5-20251001", model_display="claude-haiku-4",   input_cost_per_1k=0.00025,  output_cost_per_1k=0.00125, cache_cost_per_1k=0.000025),
            ModelPricing(provider="openai",    model_id="gpt-4o",                    model_display="gpt-4o",           input_cost_per_1k=0.0025,   output_cost_per_1k=0.01,    cache_cost_per_1k=0.0),
            ModelPricing(provider="openai",    model_id="gpt-4o-mini",               model_display="gpt-4o-mini",      input_cost_per_1k=0.00015,  output_cost_per_1k=0.0006,  cache_cost_per_1k=0.0),
            ModelPricing(provider="google",    model_id="gemini-2.0-flash",          model_display="gemini-2.0-flash", input_cost_per_1k=0.000075, output_cost_per_1k=0.0003,  cache_cost_per_1k=0.0),
            ModelPricing(provider="google",    model_id="gemini-1.5-pro",            model_display="gemini-1.5-pro",   input_cost_per_1k=0.00125,  output_cost_per_1k=0.005,   cache_cost_per_1k=0.0),
        ])
        db.commit()
    finally:
        db.close()

# ── PLAN LIMITS ───────────────────────────────────────────────────────────

PLAN_LIMITS = {
    "free":  {"max_features": 3,    "routing_rules": False, "per_customer_margin": False, "api_keys": 1},
    "pro":   {"max_features": 20,   "routing_rules": True,  "per_customer_margin": True,  "api_keys": 5},
    "scale": {"max_features": 9999, "routing_rules": True,  "per_customer_margin": True,  "api_keys": 20},
}

def require_plan(ws: Workspace, feature: str):
    if not PLAN_LIMITS.get(ws.plan or "free", PLAN_LIMITS["free"]).get(feature, False):
        raise HTTPException(402, f"Upgrade to Pro to use {feature}.")

# ── AUTH DEPENDENCY ───────────────────────────────────────────────────────

def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> User:
    """
    Accepts Bearer JWT from:
      1. Clerk (RS256, verified via JWKS)
      2. Our own email/password login (HS256)
    On Clerk token: auto-provision the user if they don't exist yet.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing Authorization header")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
    except Exception:
        raise HTTPException(401, "Invalid or expired token")

    subject = payload.get("sub")
    if not subject:
        raise HTTPException(401, "Token missing sub claim")

    issuer = payload.get("iss", "")

    # Clerk token path
    if "clerk" in issuer.lower() or issuer.startswith("https://"):
        user = get_user_by_clerk_id(db, subject)
        if not user:
            # Auto-provision from Clerk claims
            email = (
                payload.get("email")
                or (payload.get("email_addresses") or [{}])[0].get("email_address", "")
            )
            if not email:
                # Clerk sometimes puts email in a different claim
                email = payload.get("primary_email_address_id", "") or f"{subject}@clerk.local"
            user = get_user_by_email(db, email)
            if not user:
                user = User(
                    email          = email.lower().strip(),
                    password_hash  = hash_password(secrets.token_urlsafe(32)),
                    name           = payload.get("full_name") or payload.get("name") or "",
                    clerk_id       = subject,
                    email_verified = True,
                )
                db.add(user)
                db.flush()
                # Auto-create workspace
                slug = f"workspace-{secrets.token_hex(4)}"
                ws   = Workspace(name="My Workspace", slug=slug, plan="free")
                db.add(ws)
                db.flush()
                db.add(WorkspaceUser(workspace_id=ws.id, user_id=user.id, role="owner"))
                db.add(Wallet(workspace_id=ws.id, balance=0.0))
                db.commit()
                try:
                    email_service.send_welcome(user.email, user.name or "", ws.id)
                except Exception:
                    pass
            else:
                # Link existing email-based account to Clerk
                if not user.clerk_id:
                    user.clerk_id = subject
                    db.commit()
        return user

    # Own JWT path
    user = get_user_by_id(db, subject)
    if not user:
        raise HTTPException(401, "User not found")
    return user


def get_workspace(workspace_id: str, user: User, db: Session) -> Workspace:
    ws = (
        db.query(Workspace).join(WorkspaceUser)
        .filter(WorkspaceUser.user_id == user.id, Workspace.id == workspace_id)
        .first()
    )
    if not ws:
        raise HTTPException(404, "Workspace not found")
    return ws


def get_proxy_workspace(
    x_meridian_key: Annotated[str | None, Header()] = None,
    db: Session = Depends(get_db),
) -> Workspace:
    if not x_meridian_key:
        raise HTTPException(401, "Missing X-Meridian-Key")
    result = get_workspace_by_api_key(db, x_meridian_key)
    if not result:
        raise HTTPException(401, "Invalid API key")
    return result[0]

# ── SCHEMAS ───────────────────────────────────────────────────────────────

class RegisterReq(BaseModel):
    email: str; password: str; name: str = ""; workspace_name: str = "My Workspace"

class LoginReq(BaseModel):
    email: str; password: str

class PasswordResetReq(BaseModel):
    email: str

class PasswordResetConfirmReq(BaseModel):
    token: str; user_id: str; new_password: str

class FeatureReq(BaseModel):
    slug: str; name: str; description: str = ""; revenue_config: dict = {}

class CustomerReq(BaseModel):
    external_id: str; email: str = ""; name: str = ""; plan_name: str = ""
    plan_price_monthly: float = 0.0; billing_source: str = "manual"

class BudgetReq(BaseModel):
    name: str; feature_id: Optional[str] = None; scope: str = "feature"
    period: str = "monthly"; limit_amount: float; policy: str = "alert_only"
    alert_threshold: float = 0.8; fallback_model: Optional[str] = None

class RuleReq(BaseModel):
    name: str; description: str = ""; priority: int = 0; trigger: dict; action: dict

class WalletReq(BaseModel):
    auto_refill_enabled: Optional[bool] = None
    auto_refill_threshold: Optional[float] = None
    auto_refill_amount: Optional[float] = None
    freeze_at_zero: Optional[bool] = None

class ProviderReq(BaseModel):
    type: str; api_key: str; base_url: Optional[str] = None

class BillingReq(BaseModel):
    source: str
    stripe_secret_key: Optional[str] = None
    stripe_webhook_secret: Optional[str] = None

class ProxyReq(BaseModel):
    feature: str; user_id: Optional[str] = None; session_id: Optional[str] = None
    request_id: Optional[str] = None; provider: str; model: str; endpoint: str = "/messages"
    input_tokens: int; output_tokens: int; cache_tokens: int = 0; latency_ms: int
    error: Optional[str] = None; metadata: dict = {}

# ── AUTH ROUTES ───────────────────────────────────────────────────────────

@app.post("/api/auth/register")
@limiter.limit("10/minute")
def register(request: Request, body: RegisterReq, db: Session = Depends(get_db)):
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if get_user_by_email(db, body.email):
        raise HTTPException(400, "Email already registered")
    user = User(
        email          = body.email.lower().strip(),
        password_hash  = hash_password(body.password),
        name           = body.name,
        email_verified = False,
    )
    db.add(user); db.flush()
    slug = body.workspace_name.lower().replace(" ", "-") + "-" + secrets.token_hex(3)
    ws   = Workspace(name=body.workspace_name, slug=slug, plan="free")
    db.add(ws); db.flush()
    db.add(WorkspaceUser(workspace_id=ws.id, user_id=user.id, role="owner"))
    db.add(Wallet(workspace_id=ws.id, balance=0.0))
    db.commit()
    try:
        email_service.send_welcome(user.email, user.name or "", ws.id)
        email_service.send_verification(user.email, user.id, user.name or "")
    except Exception:
        pass
    token = create_access_token(user.id, ws.id)
    return {"token": token, "user_id": user.id, "workspace_id": ws.id}


@app.post("/api/auth/login")
@limiter.limit("20/minute")
def login(request: Request, body: LoginReq, db: Session = Depends(get_db)):
    user = get_user_by_email(db, body.email)
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid email or password")
    user.last_login = datetime.utcnow()
    wu    = db.query(WorkspaceUser).filter(WorkspaceUser.user_id == user.id).first()
    ws_id = wu.workspace_id if wu else None
    db.commit()
    token = create_access_token(user.id, ws_id)
    return {"token": token, "user_id": user.id, "workspace_id": ws_id}


@app.get("/api/auth/me")
def me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    wss = (
        db.query(Workspace).join(WorkspaceUser)
        .filter(WorkspaceUser.user_id == user.id).all()
    )
    return {
        "id":             user.id,
        "email":          user.email,
        "name":           user.name,
        "email_verified": user.email_verified,
        "workspaces":     [{"id": w.id, "name": w.name, "slug": w.slug, "plan": w.plan or "free"} for w in wss],
    }


@app.get("/api/auth/verify-email")
def verify_email(token: str, user_id: str, db: Session = Depends(get_db)):
    user = get_user_by_id(db, user_id)
    if not user:
        raise HTTPException(404, "User not found")
    if email_service.verify_token(token, user_id, "verify"):
        user.email_verified = True
        db.commit()
        return RedirectResponse(url=f"{_APP_URL}/app?verified=1")
    raise HTTPException(400, "Invalid or expired verification link")


@app.post("/api/auth/request-password-reset")
@limiter.limit("5/minute")
def request_password_reset(request: Request, body: PasswordResetReq, db: Session = Depends(get_db)):
    user = get_user_by_email(db, body.email)
    if user:
        try:
            email_service.send_password_reset(user.email, user.id)
        except Exception:
            pass
    # Always return 200 to avoid email enumeration
    return {"ok": True}


@app.post("/api/auth/reset-password")
def reset_password(body: PasswordResetConfirmReq, db: Session = Depends(get_db)):
    user = get_user_by_id(db, body.user_id)
    if not user:
        raise HTTPException(404)
    if not email_service.verify_token(body.token, body.user_id, "reset"):
        raise HTTPException(400, "Invalid or expired reset link")
    if len(body.new_password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    user.password_hash = hash_password(body.new_password)
    db.commit()
    return {"ok": True}

# ── API KEYS ─────────────────────────────────────────────────────────────

@app.post("/api/workspaces/{workspace_id}/api-keys")
def create_api_key(workspace_id: str, name: str = "Default",
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws  = get_workspace(workspace_id, user, db)
    n   = db.query(ApiKey).filter(ApiKey.workspace_id == ws.id, ApiKey.is_active == True).count()
    lim = PLAN_LIMITS.get(ws.plan or "free", PLAN_LIMITS["free"])["api_keys"]
    if n >= lim:
        raise HTTPException(402, f"Plan allows {lim} API key(s). Upgrade for more.")
    raw, key_hash, prefix = generate_api_key()
    db.add(ApiKey(workspace_id=ws.id, name=name, key_hash=key_hash, key_prefix=prefix))
    db.commit()
    return {"key": raw, "prefix": prefix}


@app.get("/api/workspaces/{workspace_id}/api-keys")
def list_api_keys(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws   = get_workspace(workspace_id, user, db)
    keys = db.query(ApiKey).filter(ApiKey.workspace_id == ws.id, ApiKey.is_active == True).all()
    return [{"id": k.id, "name": k.name, "prefix": k.key_prefix, "last_used": k.last_used} for k in keys]

# ── SIGNAL ────────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/signal")
def get_signal(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    return analytics.get_signal(db, workspace_id)


@app.get("/api/workspaces/{workspace_id}/daily-series")
def get_daily_series(workspace_id: str, days: int = 30, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    return analytics.get_daily_cost_series(db, workspace_id, days)

# ── FEATURES ──────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/features")
def list_features(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    return analytics.get_feature_margins(db, workspace_id)


@app.post("/api/workspaces/{workspace_id}/features")
def create_feature(workspace_id: str, body: FeatureReq, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    n  = db.query(Feature).filter(Feature.workspace_id == ws.id, Feature.is_active == True).count()
    mx = PLAN_LIMITS.get(ws.plan or "free", PLAN_LIMITS["free"])["max_features"]
    if n >= mx:
        raise HTTPException(402, f"Plan allows {mx} features. Upgrade for more.")
    f = Feature(workspace_id=ws.id, slug=body.slug, name=body.name,
                description=body.description, revenue_config=body.revenue_config)
    db.add(f); db.commit()
    return {"id": f.id, "slug": f.slug, "name": f.name}


@app.patch("/api/workspaces/{workspace_id}/features/{feature_id}")
def update_feature(workspace_id: str, feature_id: str, body: dict,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    f = db.query(Feature).filter(Feature.id == feature_id, Feature.workspace_id == workspace_id).first()
    if not f:
        raise HTTPException(404)
    for k, v in body.items():
        if hasattr(f, k):
            setattr(f, k, v)
    db.commit(); return {"ok": True}

# ── CUSTOMERS ─────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/customers")
def list_customers(workspace_id: str, status: Optional[str] = None, limit: int = 50, offset: int = 0,
                   user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    require_plan(ws, "per_customer_margin")
    rows, total = analytics.get_customer_margins(db, workspace_id, filter_status=status, limit=limit, offset=offset)
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


@app.post("/api/workspaces/{workspace_id}/customers")
def create_customer(workspace_id: str, body: CustomerReq,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws  = get_workspace(workspace_id, user, db)
    src = BillingSource.STRIPE if body.billing_source == "stripe" else BillingSource.CSV if body.billing_source == "csv" else BillingSource.MANUAL
    c   = Customer(workspace_id=ws.id, external_id=body.external_id, email=body.email,
                   name=body.name, plan_name=body.plan_name,
                   plan_price_monthly=body.plan_price_monthly, billing_source=src)
    db.add(c)
    try:
        db.commit()
    except Exception:
        db.rollback(); raise HTTPException(400, "Customer already exists")
    return {"id": c.id}

# ── SPEND ─────────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/spend/models")
def spend_models(workspace_id: str, days: int = 30, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    return analytics.get_spend_by_model(db, workspace_id, days)


@app.get("/api/workspaces/{workspace_id}/spend/ledger")
def spend_ledger(workspace_id: str, limit: int = 50, offset: int = 0, feature_id: Optional[str] = None,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    rows, total = analytics.get_ledger(db, workspace_id, limit, offset, feature_id)
    return {"rows": rows, "total": total}


@app.get("/api/workspaces/{workspace_id}/spend/anomalies")
def spend_anomalies(workspace_id: str, days: int = 7, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    return analytics.get_anomalies(db, workspace_id, days)


@app.get("/api/workspaces/{workspace_id}/spend/model-comparison")
def spend_model_comparison(workspace_id: str, days: int = 30, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """What-if: how much would the same traffic cost on every supported model?"""
    get_workspace(workspace_id, user, db)
    return analytics.get_model_comparison(db, workspace_id, days)

# ── WALLET ────────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/wallet")
def get_wallet(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    w  = db.query(Wallet).filter(Wallet.workspace_id == ws.id).first()
    if not w:
        raise HTTPException(404)
    return {"balance": round(w.balance, 2), "total_deposited": round(w.total_deposited, 2),
            "total_spent": round(w.total_spent, 2), "auto_refill_enabled": w.auto_refill_enabled,
            "auto_refill_threshold": w.auto_refill_threshold, "auto_refill_amount": w.auto_refill_amount,
            "freeze_at_zero": w.freeze_at_zero}


@app.patch("/api/workspaces/{workspace_id}/wallet")
def update_wallet(workspace_id: str, body: WalletReq, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    w  = db.query(Wallet).filter(Wallet.workspace_id == ws.id).first()
    if not w:
        raise HTTPException(404)
    for k, v in body.dict(exclude_none=True).items():
        setattr(w, k, v)
    db.commit(); return {"ok": True}


@app.post("/api/workspaces/{workspace_id}/wallet/add-funds")
def add_funds(workspace_id: str, amount: float, description: str = "Manual top-up",
              user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    w  = db.query(Wallet).filter(Wallet.workspace_id == ws.id).with_for_update().first()
    if not w:
        raise HTTPException(404)
    w.balance        += amount
    w.total_deposited += amount
    db.add(WalletEntry(wallet_id=w.id, type=TransactionType.CREDIT, amount=amount,
                       balance_after=w.balance, description=description))
    db.commit(); return {"balance": round(w.balance, 2)}

# ── BUDGETS ───────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/budgets")
def list_budgets(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    from analyticsv8_2 import period_start
    from sqlalchemy import func
    from modelsv8_2 import Transaction, Budget

    ws      = get_workspace(workspace_id, user, db)
    budgets = db.query(Budget).filter(Budget.workspace_id == ws.id, Budget.is_active == True).all()
    ps      = period_start("monthly")
    out = []
    for b in budgets:
        # FIX: correct SQLAlchemy filter for optional feature_id
        q = db.query(func.sum(Transaction.total_cost)).filter(
            Transaction.workspace_id == workspace_id,
            Transaction.created_at   >= ps,
        )
        if b.feature_id:
            q = q.filter(Transaction.feature_id == b.feature_id)
        spent = q.scalar() or 0.0
        pct   = spent / b.limit_amount if b.limit_amount else 0
        out.append({
            "id": b.id, "name": b.name, "feature_id": b.feature_id,
            "scope": b.scope, "period": b.period, "limit_amount": b.limit_amount,
            "spent": round(spent, 2), "pct_used": round(pct * 100, 1),
            "policy": b.policy, "alert_threshold": b.alert_threshold,
            "fallback_model": b.fallback_model,
            "status": "breach" if pct > 1 else ("warn" if pct >= b.alert_threshold else "ok"),
        })
    return out


@app.post("/api/workspaces/{workspace_id}/budgets")
def create_budget(workspace_id: str, body: BudgetReq, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    b  = Budget(workspace_id=ws.id, name=body.name, feature_id=body.feature_id,
                scope=body.scope, period=body.period, limit_amount=body.limit_amount,
                policy=body.policy, alert_threshold=body.alert_threshold,
                fallback_model=body.fallback_model)
    db.add(b); db.commit(); return {"id": b.id}


@app.patch("/api/workspaces/{workspace_id}/budgets/{budget_id}")
def update_budget(workspace_id: str, budget_id: str, body: dict,
                  user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    b = db.query(Budget).filter(Budget.id == budget_id, Budget.workspace_id == workspace_id).first()
    if not b:
        raise HTTPException(404)
    for k, v in body.items():
        if hasattr(b, k):
            setattr(b, k, v)
    db.commit(); return {"ok": True}

# ── ROUTING RULES ─────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/routing-rules")
def list_rules(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws    = get_workspace(workspace_id, user, db); require_plan(ws, "routing_rules")
    rules = db.query(RoutingRule).filter(RoutingRule.workspace_id == ws.id).order_by(RoutingRule.priority).all()
    return [{"id": r.id, "name": r.name, "priority": r.priority, "trigger": r.trigger,
             "action": r.action, "is_active": r.is_active, "trigger_count": r.trigger_count} for r in rules]


@app.post("/api/workspaces/{workspace_id}/routing-rules")
def create_rule(workspace_id: str, body: RuleReq, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db); require_plan(ws, "routing_rules")
    r  = RoutingRule(workspace_id=ws.id, name=body.name, description=body.description,
                     priority=body.priority, trigger=body.trigger, action=body.action)
    db.add(r); db.commit(); return {"id": r.id}


@app.patch("/api/workspaces/{workspace_id}/routing-rules/{rule_id}")
def update_rule(workspace_id: str, rule_id: str, body: dict,
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db); require_plan(ws, "routing_rules")
    r  = db.query(RoutingRule).filter(RoutingRule.id == rule_id, RoutingRule.workspace_id == workspace_id).first()
    if not r:
        raise HTTPException(404)
    for k, v in body.items():
        if hasattr(r, k):
            setattr(r, k, v)
    db.commit(); return {"ok": True}

# ── ALERTS ────────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/alerts")
def list_alerts(workspace_id: str, unread_only: bool = False, limit: int = 20,
                user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    q  = db.query(Alert).filter(Alert.workspace_id == ws.id)
    if unread_only:
        q = q.filter(Alert.is_read == False)
    alerts = q.order_by(Alert.created_at.desc()).limit(limit).all()
    return [{"id": a.id, "type": a.type, "severity": a.severity, "title": a.title,
             "body": a.body, "alert_metadata": a.alert_metadata, "is_read": a.is_read,
             "is_resolved": a.is_resolved, "created_at": a.created_at.isoformat()} for a in alerts]


@app.patch("/api/workspaces/{workspace_id}/alerts/{alert_id}")
def update_alert(workspace_id: str, alert_id: str, body: dict,
                 user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    a = db.query(Alert).filter(Alert.id == alert_id, Alert.workspace_id == workspace_id).first()
    if not a:
        raise HTTPException(404)
    if body.get("is_read"):
        a.is_read = True
    if body.get("is_resolved"):
        a.is_resolved = True; a.resolved_at = datetime.utcnow()
    db.commit(); return {"ok": True}

# ── PROVIDERS ─────────────────────────────────────────────────────────────

@app.get("/api/workspaces/{workspace_id}/providers")
def list_providers(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    return [{"id": p.id, "type": p.type, "name": p.name, "is_active": p.is_active,
             "connected_at": p.connected_at.isoformat() if p.connected_at else None}
            for p in db.query(Provider).filter(Provider.workspace_id == ws.id).all()]


@app.post("/api/workspaces/{workspace_id}/providers")
def connect_provider(workspace_id: str, body: ProviderReq,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    p  = Provider(workspace_id=ws.id, type=body.type, name=body.type.capitalize(),
                  api_key_enc=encrypt(body.api_key), base_url=body.base_url, is_active=True)
    db.add(p)
    try:
        db.commit()
    except Exception:
        db.rollback()
        ex = db.query(Provider).filter(Provider.workspace_id == ws.id, Provider.type == body.type).first()
        if ex:
            ex.api_key_enc = encrypt(body.api_key); ex.is_active = True; db.commit()
    return {"ok": True}

# ── BILLING ───────────────────────────────────────────────────────────────

@app.post("/api/workspaces/{workspace_id}/billing")
def connect_billing(workspace_id: str, body: BillingReq,
                    user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws  = get_workspace(workspace_id, user, db)
    enc = encrypt(body.stripe_secret_key) if body.stripe_secret_key else None
    ex  = db.query(BillingConnection).filter(BillingConnection.workspace_id == ws.id).first()
    if ex:
        ex.source = body.source; ex.stripe_secret_key_enc = enc
        ex.stripe_webhook_secret = body.stripe_webhook_secret; ex.is_active = True
    else:
        db.add(BillingConnection(workspace_id=ws.id, source=body.source,
                                 stripe_secret_key_enc=enc,
                                 stripe_webhook_secret=body.stripe_webhook_secret))
    db.commit()
    if body.stripe_secret_key and body.source == "stripe":
        try:
            _sync_stripe(db, ws.id, body.stripe_secret_key)
        except Exception as e:
            print(f"Stripe sync failed: {e}")
    return {"ok": True}


@app.post("/api/workspaces/{workspace_id}/billing/sync")
def sync_billing(workspace_id: str, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ws = get_workspace(workspace_id, user, db)
    bc = db.query(BillingConnection).filter(BillingConnection.workspace_id == ws.id, BillingConnection.is_active == True).first()
    if not bc or not bc.stripe_secret_key_enc:
        raise HTTPException(400, "No Stripe connection")
    try:
        _sync_stripe(db, ws.id, decrypt(bc.stripe_secret_key_enc)); return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── STRIPE WEBHOOK ────────────────────────────────────────────────────────

@app.post("/webhooks/stripe/{workspace_id}")
async def stripe_webhook(workspace_id: str, request: Request, db: Session = Depends(get_db)):
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    bc  = db.query(BillingConnection).filter(BillingConnection.workspace_id == workspace_id).first()
    if not bc or not bc.stripe_webhook_secret:
        raise HTTPException(400, "Billing not configured")
    try:
        import stripe
        event = stripe.Webhook.construct_event(payload, sig, bc.stripe_webhook_secret)
    except Exception as e:
        raise HTTPException(400, str(e))
    d = event["data"]["object"]
    if event["type"] in ("customer.created", "customer.updated"):
        _upsert_stripe_customer(db, workspace_id, d)
    elif event["type"] in ("customer.subscription.created", "customer.subscription.updated"):
        _upsert_stripe_sub(db, workspace_id, d)
    elif event["type"] == "customer.subscription.deleted":
        c = db.query(Customer).filter(Customer.workspace_id == workspace_id, Customer.stripe_customer_id == d.get("customer")).first()
        if c: c.plan_price_monthly = 0.0; c.plan_name = "Cancelled"
    elif event["type"] == "invoice.paid":
        amt = d.get("amount_paid", 0) / 100
        if amt > 0:
            w = db.query(Wallet).filter(Wallet.workspace_id == workspace_id).with_for_update().first()
            if w:
                w.balance += amt; w.total_deposited += amt
                db.add(WalletEntry(wallet_id=w.id, type=TransactionType.CREDIT, amount=amt,
                                   balance_after=w.balance, description=f"Invoice {d.get('id', '')}"))
    elif event["type"] == "checkout.session.completed":
        plan = d.get("metadata", {}).get("plan", "pro")
        ws   = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if ws: ws.plan = plan.lower()
        amt = d.get("amount_total", 0) / 100
        if amt > 0:
            w = db.query(Wallet).filter(Wallet.workspace_id == workspace_id).with_for_update().first()
            if w:
                w.balance += amt; w.total_deposited += amt
                db.add(WalletEntry(wallet_id=w.id, type=TransactionType.CREDIT, amount=amt,
                                   balance_after=w.balance, description=f"Plan upgrade: {plan}"))
    elif event["type"] == "invoice.payment_failed":
        ws = db.query(Workspace).filter(Workspace.id == workspace_id).first()
        if ws and ws.plan != "free": ws.plan = "free"
    db.commit(); return {"ok": True}


def _sync_stripe(db, workspace_id, key):
    try:
        import stripe as sl; sl.api_key = key
        after = None
        while True:
            page = sl.Customer.list(limit=100, expand=["data.subscriptions"], **({"starting_after": after} if after else {}))
            for sc in page.data:
                sub = next((s for s in (sc.subscriptions.data if sc.subscriptions else []) if s.status in ("active", "trialing")), None)
                if not sub: continue
                pi    = sub.items.data[0] if sub.items.data else None
                price = (pi.price.unit_amount / 100) if pi and pi.price.unit_amount else 0.0
                pname = (pi.price.nickname or pi.price.id or "Unknown") if pi else "Unknown"
                ex    = db.query(Customer).filter(Customer.workspace_id == workspace_id, Customer.stripe_customer_id == sc.id).first()
                if ex:
                    ex.plan_name = pname; ex.plan_price_monthly = price; ex.billing_source = BillingSource.STRIPE
                else:
                    db.add(Customer(workspace_id=workspace_id, external_id=sc.id, email=sc.email or "",
                                    name=sc.name or "", stripe_customer_id=sc.id, plan_name=pname,
                                    plan_price_monthly=price, billing_source=BillingSource.STRIPE))
            db.commit()
            if not page.has_more: break
            after = page.data[-1].id
    except ImportError:
        pass
    except Exception as e:
        db.rollback(); raise e


def _upsert_stripe_customer(db, workspace_id, d):
    eid = d.get("metadata", {}).get("user_id") or d["id"]
    c   = db.query(Customer).filter(Customer.workspace_id == workspace_id, Customer.external_id == eid).first()
    if not c: c = Customer(workspace_id=workspace_id, external_id=eid); db.add(c)
    c.email = d.get("email", ""); c.name = d.get("name", ""); c.stripe_customer_id = d["id"]; c.billing_source = BillingSource.STRIPE


def _upsert_stripe_sub(db, workspace_id, sub):
    items = sub.get("items", {}).get("data", [])
    pi    = items[0] if items else None
    amt   = (pi.get("price", {}).get("unit_amount", 0) / 100) if pi else 0.0
    intv  = (pi.get("price", {}).get("recurring", {}).get("interval", "month")) if pi else "month"
    c     = db.query(Customer).filter(Customer.workspace_id == workspace_id, Customer.stripe_customer_id == sub.get("customer")).first()
    if c: c.plan_price_monthly = amt if intv == "month" else amt / 12; c.billing_source = BillingSource.STRIPE

# ── PRICING SIMULATOR ─────────────────────────────────────────────────────

@app.post("/api/workspaces/{workspace_id}/simulate-pricing")
def simulate_pricing(workspace_id: str, body: dict,
                     user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    get_workspace(workspace_id, user, db)
    from modelsv8_2 import Transaction
    from analyticsv8_2 import period_start
    from sqlalchemy import func as sqf
    pro_price    = float(body.get("pro_price", 18))
    growth_price = float(body.get("growth_price", 49))
    pro_count    = int(body.get("pro_count", 0)) or db.query(Customer).filter(Customer.workspace_id == workspace_id, Customer.plan_name.ilike("%Pro%")).count()
    growth_count = int(body.get("growth_count", 0)) or db.query(Customer).filter(Customer.workspace_id == workspace_id, Customer.plan_name.ilike("%Growth%")).count()
    churn_pct    = float(body.get("churn_pct", 5)) / 100
    ai_cost      = (db.query(sqf.sum(Transaction.total_cost)).filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= period_start("monthly")).scalar()) or 0.0
    new_rev      = pro_count * (1 - churn_pct) * pro_price + growth_count * growth_price
    new_rev      = round(new_rev, 2)
    new_margin   = round(((new_rev - ai_cost) / new_rev * 100) if new_rev > 0 else 0, 1)
    return {"new_margin_pct": new_margin, "new_mrr": new_rev,
            "recovery_monthly": round(new_rev - pro_count * pro_price - growth_count * growth_price, 2),
            "ai_cost": round(ai_cost, 2), "churned_users": round(pro_count * churn_pct),
            "assumptions": [{"label": "Churn", "value": f"{churn_pct*100:.0f}% Pro churn", "confidence": "your_input", "basis": "Your input"}]}

# ── PROXY ─────────────────────────────────────────────────────────────────

@app.post("/proxy/ingest")
def proxy_ingest(body: ProxyReq, ws: Workspace = Depends(get_proxy_workspace), db: Session = Depends(get_db)):
    f    = db.query(Feature).filter(Feature.workspace_id == ws.id, Feature.slug == body.feature).first()
    cust = db.query(Customer).filter(Customer.workspace_id == ws.id, Customer.external_id == body.user_id).first() if body.user_id else None
    tx   = record_transaction(db=db, workspace_id=ws.id, feature_id=f.id if f else None,
                              customer_id=cust.id if cust else None, external_user_id=body.user_id,
                              session_id=body.session_id, request_id=body.request_id or str(uuid.uuid4()),
                              provider=body.provider, model=body.model, endpoint=body.endpoint,
                              input_tokens=body.input_tokens, output_tokens=body.output_tokens,
                              cache_tokens=body.cache_tokens, latency_ms=body.latency_ms,
                              error=body.error, request_metadata=body.metadata)
    check_and_fire_alerts(db, ws.id, tx); db.commit()
    return {"transaction_id": tx.id, "cost": round(tx.total_cost, 6),
            "wallet_balance": round(tx.wallet_balance_after or 0, 2)}


@app.post("/proxy/check-route")
def proxy_check_route(body: dict, ws: Workspace = Depends(get_proxy_workspace), db: Session = Depends(get_db)):
    f    = db.query(Feature).filter(Feature.workspace_id == ws.id, Feature.slug == body.get("feature")).first() if body.get("feature") else None
    cust = db.query(Customer).filter(Customer.workspace_id == ws.id, Customer.external_id == body.get("user_id")).first() if body.get("user_id") else None
    ok, bal = check_wallet(db, ws.id)
    if not ok:
        return {"action": "block", "model": None, "message": f"Wallet ${bal:.2f}. Add funds."}
    action, value = evaluate_routing_rules(db, ws.id, f.id if f else None, cust.id if cust else None,
                                           int(body.get("estimated_input_tokens", 0)), body.get("model", ""))
    db.commit()
    if action == "allow":      return {"action": "allow",  "model": body.get("model", ""), "message": None}
    if action == "route":      return {"action": "route",  "model": value,                  "message": None}
    if action in ("block", "block_402"): return {"action": "block", "model": None, "message": value}
    return {"action": "allow", "model": body.get("model", ""), "message": None}

# ── HEALTH ────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "version": "8.2.0", "timestamp": datetime.utcnow().isoformat()}

# ── FRONTEND ─────────────────────────────────────────────────────────────

_FDIR = os.path.join(os.path.dirname(__file__), "..")
if not os.path.exists(os.path.join(_FDIR, "index.html")):
    _FDIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


@app.get("/", response_class=HTMLResponse)
async def serve_root():
    for p in [os.path.join(_FDIR, "..", "landing", "index.html"), os.path.join(_FDIR, "index.html")]:
        if os.path.exists(p):
            with open(p) as f: return HTMLResponse(f.read())
    return HTMLResponse("<h1>Meridian</h1>")


@app.get("/app", response_class=HTMLResponse)
@app.get("/app/{path:path}", response_class=HTMLResponse)
async def serve_app():
    p = os.path.join(_FDIR, "index.html")
    if os.path.exists(p):
        with open(p) as f: return HTMLResponse(f.read())
    return HTMLResponse("<h1>Meridian App</h1>")
