"""
Meridian — analytics.py v8.1
All read-heavy query functions used by the API routes.
Kept separate from index.py to stay testable.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, text
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
    from modelsv8_1 import Transaction, Customer

    ps = period_start("monthly")

    # Total AI cost this month
    total_cost = (
        db.query(func.sum(Transaction.total_cost))
        .filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= ps)
        .scalar()
    ) or 0.0

    # Total revenue (sum of plan prices for active customers)
    total_rev = (
        db.query(func.sum(Customer.plan_price_monthly))
        .filter(Customer.workspace_id == workspace_id, Customer.plan_price_monthly > 0)
        .scalar()
    ) or 0.0

    margin_pct = ((total_rev - total_cost) / total_rev * 100) if total_rev > 0 else 0.0

    # Request count this month
    req_count = (
        db.query(func.count(Transaction.id))
        .filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= ps)
        .scalar()
    ) or 0

    # Avg cost per request
    avg_cost = (total_cost / req_count) if req_count else 0.0

    # Token counts
    tokens = (
        db.query(
            func.sum(Transaction.input_tokens),
            func.sum(Transaction.output_tokens),
        )
        .filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= ps)
        .first()
    )
    total_tokens = (tokens[0] or 0) + (tokens[1] or 0)

    # Customers losing money
    loss_count = (
        db.query(func.count(Customer.id))
        .filter(
            Customer.workspace_id == workspace_id,
            Customer.plan_price_monthly > 0,
        )
        .scalar()
    ) or 0

    return {
        "total_cost":      round(total_cost, 4),
        "total_revenue":   round(total_rev, 2),
        "margin_pct":      round(margin_pct, 1),
        "request_count":   req_count,
        "avg_cost_per_req": round(avg_cost, 6),
        "total_tokens":    total_tokens,
        "loss_users":      0,  # populated by customer margin view
        "period_start":    ps.isoformat(),
    }


# ── DAILY COST SERIES ─────────────────────────────────────────────────────

def get_daily_cost_series(db: Session, workspace_id: str, days: int = 30) -> list:
    from modelsv8_1 import Transaction

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
    from modelsv8_1 import Feature, Transaction

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
                    Transaction.feature_id == f.id,
                    Transaction.created_at >= ps)
            .scalar()
        ) or 0.0
        reqs = (
            db.query(func.count(Transaction.id))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.feature_id == f.id,
                    Transaction.created_at >= ps)
            .scalar()
        ) or 0

        rc = f.revenue_config or {}
        rev = float(rc.get("monthly_revenue", 0) or 0)
        margin = ((rev - cost) / rev * 100) if rev > 0 else 0.0

        result.append({
            "id":          f.id,
            "slug":        f.slug,
            "name":        f.name,
            "description": f.description,
            "cost_mtd":    round(cost, 4),
            "revenue_mtd": round(rev, 2),
            "margin_pct":  round(margin, 1),
            "request_count": reqs,
            "avg_cost":    round(cost / reqs, 6) if reqs else 0.0,
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
    from modelsv8_1 import Customer, Transaction

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
                    Transaction.customer_id == c.id,
                    Transaction.created_at >= ps)
            .scalar()
        ) or 0.0

        sessions = (
            db.query(func.count(func.distinct(Transaction.session_id)))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.customer_id == c.id,
                    Transaction.created_at >= ps)
            .scalar()
        ) or 0

        tokens_row = (
            db.query(func.sum(Transaction.input_tokens + Transaction.output_tokens))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.customer_id == c.id,
                    Transaction.created_at >= ps)
            .scalar()
        ) or 0

        rev = c.plan_price_monthly or 0.0
        gap = rev - cost
        margin = ((gap / rev) * 100) if rev > 0 else 0.0
        status = "ok" if margin >= 0 else "loss"

        if filter_status and status != filter_status:
            continue

        rows.append({
            "external_id":    c.external_id,
            "email":          c.email,
            "name":           c.name,
            "plan":           c.plan_name or "",
            "ai_cost":        round(cost, 4),
            "revenue":        round(rev, 2),
            "gap":            round(gap, 2),
            "margin_pct":     round(margin, 1),
            "session_count":  sessions,
            "avg_tokens_per_session": round(tokens_row / sessions) if sessions else 0,
            "status":         status,
            "billing_source": c.billing_source.value if c.billing_source else "manual",
        })

    total = len(rows)
    return rows[offset: offset + limit], total


# ── SPEND BY MODEL ────────────────────────────────────────────────────────

def get_spend_by_model(db: Session, workspace_id: str, days: int = 30) -> list:
    from modelsv8_1 import Transaction

    since = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            Transaction.provider,
            Transaction.model,
            func.sum(Transaction.total_cost).label("cost"),
            func.count(Transaction.id).label("requests"),
            func.sum(Transaction.input_tokens).label("input_tokens"),
            func.sum(Transaction.output_tokens).label("output_tokens"),
        )
        .filter(Transaction.workspace_id == workspace_id, Transaction.created_at >= since)
        .group_by(Transaction.provider, Transaction.model)
        .order_by(func.sum(Transaction.total_cost).desc())
        .all()
    )
    return [
        {
            "provider":      str(r.provider.value if r.provider else ""),
            "model":         r.model or "",
            "cost":          round(r.cost or 0, 4),
            "requests":      r.requests,
            "input_tokens":  r.input_tokens or 0,
            "output_tokens": r.output_tokens or 0,
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
    from modelsv8_1 import Transaction

    q = db.query(Transaction).filter(Transaction.workspace_id == workspace_id)
    if feature_id:
        q = q.filter(Transaction.feature_id == feature_id)
    total = q.count()
    rows  = q.order_by(Transaction.created_at.desc()).offset(offset).limit(limit).all()
    return [
        {
            "id":            r.id,
            "created_at":    r.created_at.isoformat(),
            "provider":      str(r.provider.value if r.provider else ""),
            "model":         r.model or "",
            "feature_id":    r.feature_id,
            "external_user_id": r.external_user_id,
            "input_tokens":  r.input_tokens,
            "output_tokens": r.output_tokens,
            "total_cost":    round(r.total_cost or 0, 6),
            "latency_ms":    r.latency_ms,
            "error":         r.error,
        }
        for r in rows
    ], total


# ── ANOMALIES ─────────────────────────────────────────────────────────────

def get_anomalies(db: Session, workspace_id: str, days: int = 7) -> list:
    from modelsv8_1 import Transaction

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

    costs = [float(r.cost or 0) for r in daily]
    avg   = sum(costs) / len(costs) if costs else 0
    std   = (sum((c - avg) ** 2 for c in costs) / len(costs)) ** 0.5 if len(costs) > 1 else 0
    threshold = avg + 2 * std

    anomalies = []
    for r, cost in zip(daily, costs):
        if cost > threshold and threshold > 0:
            anomalies.append({
                "day":        str(r.day),
                "cost":       round(cost, 4),
                "avg_cost":   round(avg, 4),
                "pct_above":  round((cost - avg) / avg * 100, 1) if avg else 0,
            })
    return anomalies
