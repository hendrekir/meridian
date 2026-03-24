"""
Meridian — proxy.py v8.3
Core proxy logic: routing rule evaluation, wallet balance checks,
transaction recording with cost calculation, alert firing.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.orm import Session


# ── COST CALCULATION ──────────────────────────────────────────────────────

def _calc_cost(db: Session, provider: str, model: str,
               input_tokens: int, output_tokens: int, cache_tokens: int) -> Tuple[float, float, float]:
    """Returns (input_cost, output_cost, cache_cost)."""
    from modelsv8_3 import ModelPricing
    pricing = (
        db.query(ModelPricing)
        .filter(ModelPricing.provider == provider, ModelPricing.model_id == model)
        .order_by(ModelPricing.valid_from.desc())
        .first()
    )
    if not pricing:
        # Fallback: generous estimate so we never silently lose money tracking
        ic = input_tokens  / 1000 * 0.003
        oc = output_tokens / 1000 * 0.015
        cc = cache_tokens  / 1000 * 0.0003
        return ic, oc, cc

    ic = input_tokens  / 1000 * pricing.input_cost_per_1k
    oc = output_tokens / 1000 * pricing.output_cost_per_1k
    cc = cache_tokens  / 1000 * (pricing.cache_cost_per_1k or 0)
    return ic, oc, cc


# ── WALLET ────────────────────────────────────────────────────────────────

def check_wallet(db: Session, workspace_id: str) -> Tuple[bool, float]:
    """Returns (ok, balance). ok=False means the wallet is frozen at zero."""
    from modelsv8_3 import Wallet
    w = db.query(Wallet).filter(Wallet.workspace_id == workspace_id).first()
    if not w:
        return True, 0.0  # no wallet = don't block
    if w.freeze_at_zero and w.balance <= 0:
        return False, w.balance
    return True, w.balance


# ── TRANSACTION RECORDING ─────────────────────────────────────────────────

def record_transaction(
    db: Session,
    workspace_id: str,
    feature_id: Optional[str],
    customer_id: Optional[str],
    external_user_id: Optional[str],
    session_id: Optional[str],
    request_id: str,
    provider: str,
    model: str,
    endpoint: str,
    input_tokens: int,
    output_tokens: int,
    cache_tokens: int,
    latency_ms: int,
    error: Optional[str],
    request_metadata: dict,
):
    from modelsv8_3 import Transaction, TransactionType, Wallet, WalletEntry

    ic, oc, cc = _calc_cost(db, provider, model, input_tokens, output_tokens, cache_tokens)
    total = ic + oc + cc

    # Deduct from wallet
    wallet_balance_after = None
    wallet_deducted      = False
    w = db.query(Wallet).filter(Wallet.workspace_id == workspace_id).with_for_update().first()
    if w and total > 0:
        w.balance         = max(0, w.balance - total)
        w.total_spent    += total
        wallet_balance_after = w.balance
        wallet_deducted      = True
        db.add(WalletEntry(
            wallet_id     = w.id,
            type          = TransactionType.LLM_CALL,
            amount        = -total,
            balance_after = w.balance,
            description   = f"{provider}/{model} — {input_tokens}in/{output_tokens}out",
            reference_id  = request_id,
        ))

    tx = Transaction(
        workspace_id         = workspace_id,
        feature_id           = feature_id,
        customer_id          = customer_id,
        external_user_id     = external_user_id,
        session_id           = session_id,
        request_id           = request_id or str(uuid.uuid4()),
        type                 = TransactionType.LLM_CALL,
        provider             = provider,
        model                = model,
        endpoint             = endpoint,
        input_tokens         = input_tokens,
        output_tokens        = output_tokens,
        cache_tokens         = cache_tokens,
        input_cost           = ic,
        output_cost          = oc,
        cache_cost           = cc,
        total_cost           = total,
        latency_ms           = latency_ms,
        error                = error,
        wallet_deducted      = wallet_deducted,
        wallet_balance_after = wallet_balance_after,
        request_metadata     = request_metadata or {},
    )
    db.add(tx)
    db.flush()  # get tx.id before commit
    return tx


# ── ROUTING RULES ─────────────────────────────────────────────────────────

def evaluate_routing_rules(
    db: Session,
    workspace_id: str,
    feature_id: Optional[str],
    customer_id: Optional[str],
    estimated_tokens: int,
    current_model: str,
) -> Tuple[str, Optional[str]]:
    """
    Returns (action, value).
    action: 'allow' | 'route' | 'block' | 'block_402'
    value:  new model string (for 'route') or message (for 'block')
    """
    from modelsv8_3 import RoutingRule, Budget, Transaction
    from analyticsv8_3 import period_start
    from sqlalchemy import func

    rules = (
        db.query(RoutingRule)
        .filter(RoutingRule.workspace_id == workspace_id, RoutingRule.is_active == True)
        .order_by(RoutingRule.priority.asc())
        .all()
    )

    for rule in rules:
        trigger = rule.trigger or {}
        action  = rule.action  or {}

        matched = _eval_trigger(
            trigger, feature_id, customer_id, estimated_tokens, current_model, db, workspace_id
        )
        if not matched:
            continue

        rule.trigger_count += 1
        rule.last_triggered = datetime.utcnow()

        act_type = action.get("type", "allow")
        if act_type == "route":
            return "route", action.get("model", current_model)
        if act_type == "block":
            return "block", action.get("message", "Blocked by routing rule.")
        if act_type == "block_402":
            return "block_402", action.get("message", "Upgrade required.")
        # allow / unknown
        return "allow", None

    return "allow", None


def _eval_trigger(trigger: dict, feature_id, customer_id, tokens, model, db, workspace_id) -> bool:
    from modelsv8_3 import Budget, Transaction
    from analyticsv8_3 import period_start
    from sqlalchemy import func

    t = trigger.get("type")

    if t == "token_threshold":
        return tokens >= int(trigger.get("value", 0))

    if t == "model_match":
        return model == trigger.get("model", "")

    if t == "feature_match":
        return feature_id == trigger.get("feature_id")

    if t == "budget_pct":
        # Check if feature's budget usage exceeds threshold
        if not feature_id:
            return False
        b = (
            db.query(Budget)
            .filter(Budget.workspace_id == workspace_id,
                    Budget.feature_id == feature_id,
                    Budget.is_active == True)
            .first()
        )
        if not b:
            return False
        ps = period_start("monthly")
        spent = (
            db.query(func.sum(Transaction.total_cost))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.feature_id == feature_id,
                    Transaction.created_at >= ps)
            .scalar()
        ) or 0.0
        pct = spent / b.limit_amount if b.limit_amount else 0
        return pct >= float(trigger.get("value", 0.8))

    if t == "always":
        return True

    return False


# ── ALERTS ────────────────────────────────────────────────────────────────

def check_and_fire_alerts(db: Session, workspace_id: str, tx) -> None:
    """
    Post-transaction hook. Fires alerts for budget breaches, wallet low, token spikes.
    Intentionally non-blocking — errors are printed, not raised.
    """
    try:
        _check_budget_alerts(db, workspace_id, tx)
        _check_wallet_alerts(db, workspace_id)
    except Exception as e:
        print(f"[alerts] Non-fatal error: {e}")


def _check_budget_alerts(db: Session, workspace_id: str, tx) -> None:
    from modelsv8_3 import Budget, Transaction, Alert, AlertType, AlertSeverity, BudgetPolicy
    from analyticsv8_3 import period_start
    from sqlalchemy import func

    if not tx.feature_id:
        return

    budgets = (
        db.query(Budget)
        .filter(Budget.workspace_id == workspace_id,
                Budget.feature_id == tx.feature_id,
                Budget.is_active == True)
        .all()
    )

    ps = period_start("monthly")
    for b in budgets:
        spent = (
            db.query(func.sum(Transaction.total_cost))
            .filter(Transaction.workspace_id == workspace_id,
                    Transaction.feature_id == tx.feature_id,
                    Transaction.created_at >= ps)
            .scalar()
        ) or 0.0

        pct = spent / b.limit_amount if b.limit_amount else 0

        if pct >= 1.0:
            _maybe_create_alert(db, workspace_id, AlertType.BUDGET_BREACH, AlertSeverity.CRITICAL,
                                f"Budget breached: {b.name}",
                                f"Spent ${spent:.2f} of ${b.limit_amount:.2f} ({pct*100:.0f}%).",
                                {"budget_id": b.id, "pct": pct})
        elif pct >= b.alert_threshold:
            _maybe_create_alert(db, workspace_id, AlertType.BUDGET_THRESHOLD, AlertSeverity.WARNING,
                                f"Budget at {pct*100:.0f}%: {b.name}",
                                f"${spent:.2f} of ${b.limit_amount:.2f} used.",
                                {"budget_id": b.id, "pct": pct})


def _check_wallet_alerts(db: Session, workspace_id: str) -> None:
    from modelsv8_3 import Wallet, Alert, AlertType, AlertSeverity

    w = db.query(Wallet).filter(Wallet.workspace_id == workspace_id).first()
    if not w:
        return
    if w.balance < 5.0:
        _maybe_create_alert(db, workspace_id, AlertType.WALLET_LOW, AlertSeverity.WARNING,
                            "Wallet balance low",
                            f"Balance is ${w.balance:.2f}. Add funds to avoid service interruption.",
                            {"balance": w.balance})


def _maybe_create_alert(db, workspace_id, alert_type, severity, title, body, meta) -> None:
    """Create an alert only if one of the same type+title hasn't fired in the last hour."""
    from modelsv8_3 import Alert
    from datetime import timedelta

    recent_cutoff = datetime.utcnow() - timedelta(hours=1)
    exists = (
        db.query(Alert)
        .filter(Alert.workspace_id == workspace_id,
                Alert.type == alert_type,
                Alert.title == title,
                Alert.created_at >= recent_cutoff)
        .first()
    )
    if exists:
        return

    db.add(Alert(
        workspace_id   = workspace_id,
        type           = alert_type,
        severity       = severity,
        title          = title,
        body           = body,
        alert_metadata = meta,
    ))
