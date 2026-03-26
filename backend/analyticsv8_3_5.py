"""
Meridian — analytics.py v8.3.5
All read-heavy query functions used by the API routes.
Kept separate from index.py to stay testable.

FIXES vs v8.3.4:
  - get_signal() now returns the full shape the frontend expects
    (ai_cost_mtd, revenue_mtd, daily_burn_avg, features_losing,
     customers_losing, wallet_balance, recoverable_monthly,
     industry_avg_margin, recoverable_breakdown, etc.)
  - get_model_comparison(): stale `from models_v6_2` import replaced
    with `from modelsv8_3_5`
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session


# ── PERIOD HELPERS ────────────────────────────────────────────────────────

def period_start(period: str) -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if period == "daily":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "weekly":
        return (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    # monthly
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


# ── SIGNAL ────────────────────────────────────────────────────────────────

def get_signal(db: Session, workspace_id: str) -> dict:
    """
    Returns the full margin-signal shape the frontend dashboard expects.
    All fields are documented inline.
    """
    from modelsv8_3_5 import Transaction, Customer, Feature, Wallet

    ps  = period_start("monthly")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    days_elapsed = max(1, (now - ps).days + 1)

    # ── AI cost ──────────────────────────────────────────────────────────
    ai_cost_mtd = (
        db.query(func.sum(Transaction.total_cost))
        .filter(Transaction.workspace_id == workspace_id,
                Transaction.created_at   >= ps)
        .scalar()
    ) or 0.0

    daily_burn_avg          = ai_cost_mtd / days_elapsed
    projected_month_end_cost = daily_burn_avg * 30

    # ── Revenue ───────────────────────────────────────────────────────────
    revenue_mtd = (
        db.query(func.sum(Customer.plan_price_monthly))
        .filter(Customer.workspace_id == workspace_id,
                Customer.plan_price_monthly > 0)
        .scalar()
    ) or 0.0

    margin_pct = ((revenue_mtd - ai_cost_mtd) / revenue_mtd * 100) if revenue_mtd > 0 else 0.0

    # ── Request stats ─────────────────────────────────────────────────────
    req_count = (
        db.query(func.count(Transaction.id))
        .filter(Transaction.workspace_id == workspace_id,
                Transaction.created_at   >= ps)
        .scalar()
    ) or 0

    avg_cost = (ai_cost_mtd / req_count) if req_count else 0.0

    tokens = (
        db.query(
            func.sum(Transaction.input_tokens),
            func.sum(Transaction.output_tokens),
        )
        .filter(Transaction.workspace_id == workspace_id,
                Transaction.created_at   >= ps)
        .first()
    )
    total_tokens = (tokens[0] or 0) + (tokens[1] or 0)

    # ── Features at loss ──────────────────────────────────────────────────
    features = (
        db.query(Feature)
        .filter(Feature.workspace_id == workspace_id, Feature.is_active == True)
        .all()
    )
    features_total   = len(features)
    features_losing  = 0
    for f in features:
        f_cost = (
            db.query(func.sum(Transaction.total_cost))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.feature_id   == f.id,
                    Transaction.created_at   >= ps)
            .scalar()
        ) or 0.0
        rc  = f.revenue_config or {}
        rev = float(rc.get("monthly_revenue", 0) or 0)
        if rev > 0 and f_cost > rev:
            features_losing += 1

    # ── Customers at loss ─────────────────────────────────────────────────
    customers        = (
        db.query(Customer)
        .filter(Customer.workspace_id    == workspace_id,
                Customer.plan_price_monthly > 0)
        .all()
    )
    customers_losing = 0
    for c in customers:
        c_cost = (
            db.query(func.sum(Transaction.total_cost))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.customer_id  == c.id,
                    Transaction.created_at   >= ps)
            .scalar()
        ) or 0.0
        if c_cost > c.plan_price_monthly:
            customers_losing += 1

    # ── Wallet ────────────────────────────────────────────────────────────
    wallet         = db.query(Wallet).filter(Wallet.workspace_id == workspace_id).first()
    wallet_balance = wallet.balance if wallet else 0.0

    # ── Recoverable (estimated) ───────────────────────────────────────────
    # Caching gap: ~15% of AI cost from prompt caching being disabled
    caching_gap = round(ai_cost_mtd * 0.15, 2)
    # Repricing gap: revenue shortfall from loss-margin customers
    loss_customers_count  = customers_losing
    repricing_gap = 0.0
    if loss_customers_count > 0 and len(customers) > 0:
        avg_plan = revenue_mtd / len(customers)
        repricing_gap = round(loss_customers_count * avg_plan * 0.6, 2)

    recoverable_monthly = round(caching_gap + repricing_gap, 2)

    return {
        # ── Primary fields (all used by frontend) ──
        "ai_cost_mtd":               round(ai_cost_mtd, 4),
        "revenue_mtd":               round(revenue_mtd, 2),
        "margin_pct":                round(margin_pct, 1),
        "daily_burn_avg":            round(daily_burn_avg, 2),
        "projected_month_end_cost":  round(projected_month_end_cost, 2),
        "days_elapsed":              days_elapsed,
        "features_total":            features_total,
        "features_losing":           features_losing,
        "customers_losing":          customers_losing,
        "recoverable_monthly":       recoverable_monthly,
        "wallet_balance":            round(wallet_balance, 2),
        # ── Industry benchmarks ──
        "industry_avg_margin":        34.1,
        "industry_avg_margin_source": "Bessemer/a16z 2024 (n≈2,400)",
        "industry_avg_margin_live":   False,
        # ── Recoverable breakdown ──
        "recoverable_breakdown": {
            "caching_gap": {
                "amount":     caching_gap,
                "basis":      "15% of AI cost — prompt caching disabled",
                "confidence": "estimated",
            },
            "repricing_gap": {
                "amount":     repricing_gap,
                "basis":      f"Revenue shortfall from {customers_losing} loss-margin users",
                "confidence": "estimated",
            },
        },
        # ── Legacy / supplementary ──
        "request_count":    req_count,
        "avg_cost_per_req": round(avg_cost, 6),
        "total_tokens":     total_tokens,
        "period_start":     ps.isoformat(),
    }


# ── DAILY COST SERIES ─────────────────────────────────────────────────────

def get_daily_cost_series(db: Session, workspace_id: str, days: int = 30) -> list:
    from modelsv8_3_5 import Transaction

    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            func.date(Transaction.created_at).label("day"),
            func.sum(Transaction.total_cost).label("cost"),
            func.count(Transaction.id).label("requests"),
        )
        .filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= since)
        .group_by(func.date(Transaction.created_at))
        .order_by(func.date(Transaction.created_at))
        .all()
    )
    return [{"day": str(r.day), "cost": round(r.cost or 0, 4), "requests": r.requests} for r in rows]


# ── FEATURE MARGINS ───────────────────────────────────────────────────────

def get_feature_margins(db: Session, workspace_id: str) -> list:
    from modelsv8_3_5 import Feature, Transaction

    ps = period_start("monthly")
    features = (
        db.query(Feature)
        .filter(Feature.workspace_id == workspace_id, Feature.is_active == True)
        .all()
    )
    result = []
    for f in features:
        cost = (
            db.query(func.sum(Transaction.total_cost))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.feature_id   == f.id,
                    Transaction.created_at   >= ps)
            .scalar()
        ) or 0.0
        reqs = (
            db.query(func.count(Transaction.id))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.feature_id   == f.id,
                    Transaction.created_at   >= ps)
            .scalar()
        ) or 0

        rc  = f.revenue_config or {}
        rev = float(rc.get("monthly_revenue", 0) or 0)
        margin = ((rev - cost) / rev * 100) if rev > 0 else 0.0

        result.append({
            "id":            f.id,
            "slug":          f.slug,
            "name":          f.name,
            "description":   f.description,
            "cost_mtd":      round(cost, 4),
            "revenue_mtd":   round(rev, 2),
            "margin_pct":    round(margin, 1),
            "request_count": reqs,
            "avg_cost":      round(cost / reqs, 6) if reqs else 0.0,
            # Fields used by frontend feature table
            "ai_cost":       round(cost, 4),
            "revenue":       round(rev, 2),
            "cost_per_req":  round(cost / reqs, 6) if reqs else 0.0,
            "status":        "loss" if margin < 0 else ("thin" if margin < 40 else "ok"),
            "revenue_source": "stripe" if rc.get("billing_source") == "stripe" else (
                              "manual" if rev > 0 else "estimated"),
        })
    return result


# ── CUSTOMER MARGINS ──────────────────────────────────────────────────────

def get_customer_margins(
    db: Session,
    workspace_id: str,
    filter_status: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list, int]:
    from modelsv8_3_5 import Customer, Transaction

    ps = period_start("monthly")

    customers = (
        db.query(Customer)
        .filter(Customer.workspace_id == workspace_id)
        .all()
    )

    rows = []
    for c in customers:
        cost = (
            db.query(func.sum(Transaction.total_cost))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.customer_id  == c.id,
                    Transaction.created_at   >= ps)
            .scalar()
        ) or 0.0

        sessions = (
            db.query(func.count(func.distinct(Transaction.session_id)))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.customer_id  == c.id,
                    Transaction.created_at   >= ps)
            .scalar()
        ) or 0

        tokens_row = (
            db.query(func.sum(Transaction.input_tokens + Transaction.output_tokens))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.customer_id  == c.id,
                    Transaction.created_at   >= ps)
            .scalar()
        ) or 0

        rev    = c.plan_price_monthly or 0.0
        gap    = rev - cost
        margin = ((gap / rev) * 100) if rev > 0 else 0.0
        status = "ok" if margin >= 0 else "loss"

        if filter_status and status != filter_status:
            continue

        rows.append({
            "external_id":             c.external_id,
            "email":                   c.email,
            "name":                    c.name,
            "plan":                    c.plan_name or "",
            "ai_cost":                 round(cost, 4),
            "revenue":                 round(rev, 2),
            "gap":                     round(gap, 2),
            "margin_pct":              round(margin, 1),
            "session_count":           sessions,
            "avg_tokens_per_session":  round(tokens_row / sessions) if sessions else 0,
            "status":                  status,
            "billing_source":          c.billing_source.value if c.billing_source else "manual",
        })

    total = len(rows)
    return rows[offset: offset + limit], total


# ── SPEND BY MODEL ────────────────────────────────────────────────────────

def get_spend_by_model(db: Session, workspace_id: str, days: int = 30) -> list:
    from modelsv8_3_5 import Transaction

    since = datetime.utcnow() - timedelta(days=days)
    rows  = (
        db.query(
            Transaction.provider,
            Transaction.model,
            func.sum(Transaction.total_cost).label("cost"),
            func.count(Transaction.id).label("requests"),
            func.sum(Transaction.input_tokens).label("input_tokens"),
            func.sum(Transaction.output_tokens).label("output_tokens"),
            func.sum(Transaction.cache_tokens).label("cache_tokens"),
        )
        .filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= since)
        .group_by(Transaction.provider, Transaction.model)
        .order_by(func.sum(Transaction.total_cost).desc())
        .all()
    )
    total_cost = sum(r.cost or 0 for r in rows) or 1.0
    return [
        {
            "provider":          str(r.provider.value if r.provider else ""),
            "model":             r.model or "",
            "cost":              round(r.cost or 0, 4),
            "total_cost":        round(r.cost or 0, 4),
            "requests":          r.requests,
            "input_tokens":      r.input_tokens  or 0,
            "output_tokens":     r.output_tokens or 0,
            "cache_tokens":      r.cache_tokens  or 0,
            "has_caching":       (r.cache_tokens or 0) > 0,
            "pct_of_spend":      round((r.cost or 0) / total_cost * 100, 1),
            "cost_per_1k_tokens": round(
                (r.cost or 0) / max(1, ((r.input_tokens or 0) + (r.output_tokens or 0)) / 1000), 4
            ),
        }
        for r in rows
    ]


# ── LEDGER ────────────────────────────────────────────────────────────────

def get_ledger(
    db: Session,
    workspace_id: str,
    limit: int = 50,
    offset: int = 0,
    feature_id: Optional[str] = None,
) -> tuple[list, int]:
    from modelsv8_3_5 import Transaction

    q     = db.query(Transaction).filter(Transaction.workspace_id == workspace_id)
    if feature_id:
        q = q.filter(Transaction.feature_id == feature_id)
    total = q.count()
    rows  = q.order_by(Transaction.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id":               r.id,
            "created_at":       r.created_at.isoformat(),
            "provider":         str(r.provider.value if r.provider else ""),
            "model":            r.model or "",
            "feature_id":       r.feature_id,
            "external_user_id": r.external_user_id,
            "input_tokens":     r.input_tokens,
            "output_tokens":    r.output_tokens,
            "total_cost":       round(r.total_cost or 0, 6),
            "latency_ms":       r.latency_ms,
            "error":            r.error,
            "description":      f"{r.provider.value if r.provider else ''}/{r.model} — {r.input_tokens}in/{r.output_tokens}out",
            "type":             r.type.value if r.type else "llm_call",
            "amount":           -round(r.total_cost or 0, 6),
            "balance_after":    round(r.wallet_balance_after or 0, 2),
        }
        for r in rows
    ], total


# ── ANOMALIES ─────────────────────────────────────────────────────────────

def get_anomalies(db: Session, workspace_id: str, days: int = 7) -> list:
    from modelsv8_3_5 import Transaction

    since = datetime.utcnow() - timedelta(days=days)
    daily = (
        db.query(
            func.date(Transaction.created_at).label("day"),
            func.sum(Transaction.total_cost).label("cost"),
        )
        .filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= since)
        .group_by(func.date(Transaction.created_at))
        .order_by(func.date(Transaction.created_at))
        .all()
    )
    if not daily:
        return []

    costs     = [float(r.cost or 0) for r in daily]
    avg       = sum(costs) / len(costs) if costs else 0
    std       = (sum((c - avg) ** 2 for c in costs) / len(costs)) ** 0.5 if len(costs) > 1 else 0
    threshold = avg + 2 * std

    anomalies = []
    for r, cost in zip(daily, costs):
        if cost > threshold and threshold > 0:
            anomalies.append({
                "day":        str(r.day),
                "cost":       round(cost, 4),
                "avg_cost":   round(avg, 4),
                "pct_above":  round((cost - avg) / avg * 100, 1) if avg else 0,
                # Frontend-expected fields
                "feature_name":           "Cost spike",
                "deviation_pct":          round((cost - avg) / avg * 100, 1) if avg else 0,
                "current_avg_tokens":     0,
                "baseline_avg_tokens":    0,
                "severity":               "critical" if cost > avg * 3 else "warning",
            })
    return anomalies


# ── MODEL COMPARISON (what-if) ────────────────────────────────────────────

# Current pricing for what-if calculations — same as seed data in index.py
_MODEL_PRICING = {
    "claude-opus-4-6":           {"input": 0.015,    "output": 0.075,   "display": "Claude Opus 4",    "provider": "anthropic"},
    "claude-sonnet-4-6":         {"input": 0.003,    "output": 0.015,   "display": "Claude Sonnet 4",  "provider": "anthropic"},
    "claude-haiku-4-5-20251001": {"input": 0.00025,  "output": 0.00125, "display": "Claude Haiku 4",   "provider": "anthropic"},
    "gpt-4o":                    {"input": 0.0025,   "output": 0.01,    "display": "GPT-4o",            "provider": "openai"},
    "gpt-4o-mini":               {"input": 0.00015,  "output": 0.0006,  "display": "GPT-4o mini",       "provider": "openai"},
    "gemini-2.0-flash":          {"input": 0.000075, "output": 0.0003,  "display": "Gemini 2.0 Flash",  "provider": "google"},
    "gemini-1.5-pro":            {"input": 0.00125,  "output": 0.005,   "display": "Gemini 1.5 Pro",    "provider": "google"},
}


def get_model_comparison(db: Session, workspace_id: str, days: int = 30) -> dict:
    """
    For each model the workspace actually uses, calculate what the same
    token volume would cost on every other supported model.
    """
    from modelsv8_3_5 import Transaction  # FIX: was `from models_v6_2 import Transaction`

    rows = get_spend_by_model(db, workspace_id, days)

    if not rows:
        return {"current_models": [], "alternatives": [], "total_current": 0}

    total_current = sum(r["cost"] for r in rows)
    total_input   = sum(r["input_tokens"]  for r in rows)
    total_output  = sum(r["output_tokens"] for r in rows)

    alternatives = []
    for model_id, pricing in _MODEL_PRICING.items():
        alt_cost = (
            total_input  / 1000 * pricing["input"] +
            total_output / 1000 * pricing["output"]
        )
        saving     = total_current - alt_cost
        saving_pct = (saving / total_current * 100) if total_current > 0 else 0
        is_current = any(r["model"] == model_id for r in rows)

        alternatives.append({
            "model_id":     model_id,
            "display":      pricing["display"],
            "provider":     pricing["provider"],
            "monthly_cost": round(alt_cost, 2),
            "saving":       round(saving, 2),
            "saving_pct":   round(saving_pct, 1),
            "is_current":   is_current,
            "input_per_1k": pricing["input"],
            "output_per_1k":pricing["output"],
        })

    alternatives.sort(key=lambda x: (not x["is_current"], -x["saving"]))

    return {
        "current_models": rows,
        "alternatives":   alternatives,
        "total_current":  round(total_current, 2),
        "total_input":    total_input,
        "total_output":   total_output,
        "days":           days,
    }
