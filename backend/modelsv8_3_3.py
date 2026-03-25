"""
Meridian v8.3 — modelsv8_3_3.py
SQLAlchemy ORM models for all database tables.
"""
from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Text, ForeignKey,
    Enum, JSON, UniqueConstraint, Index
)
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func
import uuid
import enum

Base = declarative_base()

def gen_id():
    return str(uuid.uuid4())

# ─── ENUMS ───────────────────────────────────────────────────────────────

class ProviderType(str, enum.Enum):
    ANTHROPIC = "anthropic"
    OPENAI    = "openai"
    GOOGLE    = "google"
    MISTRAL   = "mistral"
    OPENROUTER= "openrouter"
    COHERE    = "cohere"

class BudgetScope(str, enum.Enum):
    WORKSPACE = "workspace"
    FEATURE   = "feature"
    USER      = "user"

class BudgetPeriod(str, enum.Enum):
    DAILY   = "daily"
    WEEKLY  = "weekly"
    MONTHLY = "monthly"

class BudgetPolicy(str, enum.Enum):
    ALERT_ONLY    = "alert_only"
    SOFT_CAP      = "soft_cap"
    HARD_CAP      = "hard_cap"
    UPGRADE_PROMPT= "upgrade_prompt"

class AlertSeverity(str, enum.Enum):
    CRITICAL = "critical"
    WARNING  = "warning"
    INFO     = "info"

class AlertType(str, enum.Enum):
    BUDGET_BREACH      = "budget_breach"
    BUDGET_THRESHOLD   = "budget_threshold"
    TOKEN_SPIKE        = "token_spike"
    MARGIN_NEGATIVE    = "margin_negative"
    WALLET_LOW         = "wallet_low"
    USER_OVER_PLAN     = "user_over_plan"

class BillingSource(str, enum.Enum):
    STRIPE  = "stripe"
    MANUAL  = "manual"
    CSV     = "csv"
    CLERK   = "clerk"

class TransactionType(str, enum.Enum):
    LLM_CALL = "llm_call"
    CREDIT   = "credit"
    DEBIT    = "debit"
    FEE      = "fee"
    REFUND   = "refund"

class WorkspacePlan(str, enum.Enum):
    FREE    = "free"
    PRO     = "pro"
    SCALE   = "scale"

# ─── WORKSPACE & AUTH ────────────────────────────────────────────────────

class Workspace(Base):
    __tablename__ = "workspaces"

    id           = Column(String, primary_key=True, default=gen_id)
    name         = Column(String, nullable=False)
    slug         = Column(String, unique=True, nullable=False)
    created_at   = Column(DateTime, server_default=func.now())
    updated_at   = Column(DateTime, server_default=func.now(), onupdate=func.now())

    # Plan gating — free | pro | scale
    plan         = Column(String, default="free")
    plan_expires = Column(DateTime)                # null = no expiry / lifetime

    # Settings
    timezone     = Column(String, default="UTC")
    currency     = Column(String, default="USD")

    # Relationships
    users        = relationship("WorkspaceUser",  back_populates="workspace", cascade="all, delete")
    api_keys     = relationship("ApiKey",         back_populates="workspace", cascade="all, delete")
    providers    = relationship("Provider",       back_populates="workspace", cascade="all, delete")
    features     = relationship("Feature",        back_populates="workspace", cascade="all, delete")
    transactions = relationship("Transaction",    back_populates="workspace", cascade="all, delete")
    customers    = relationship("Customer",       back_populates="workspace", cascade="all, delete")
    budgets      = relationship("Budget",         back_populates="workspace", cascade="all, delete")
    routing_rules= relationship("RoutingRule",    back_populates="workspace", cascade="all, delete")
    wallet       = relationship("Wallet",         back_populates="workspace", uselist=False, cascade="all, delete")
    alerts       = relationship("Alert",          back_populates="workspace", cascade="all, delete")
    billing_conn = relationship("BillingConnection", back_populates="workspace", uselist=False, cascade="all, delete")


class User(Base):
    __tablename__ = "users"

    id                    = Column(String, primary_key=True, default=gen_id)
    email                 = Column(String, unique=True, nullable=False, index=True)
    password_hash         = Column(String, nullable=False)
    name                  = Column(String)
    created_at            = Column(DateTime, server_default=func.now())
    last_login            = Column(DateTime)
    email_verified        = Column(Boolean, default=False)
    # Stored so we can re-send verification without DB token table overhead
    email_verify_token    = Column(String)
    password_reset_token  = Column(String)
    clerk_id              = Column(String, unique=True, index=True)

    workspaces   = relationship("WorkspaceUser", back_populates="user", cascade="all, delete")


class WorkspaceUser(Base):
    __tablename__ = "workspace_users"

    id           = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    user_id      = Column(String, ForeignKey("users.id"), nullable=False)
    role         = Column(String, default="member")
    created_at   = Column(DateTime, server_default=func.now())

    workspace    = relationship("Workspace",     back_populates="users")
    user         = relationship("User",          back_populates="workspaces")

    __table_args__ = (UniqueConstraint("workspace_id", "user_id"),)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id           = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    name         = Column(String, nullable=False)
    key_hash     = Column(String, nullable=False, unique=True)
    key_prefix   = Column(String, nullable=False)
    created_at   = Column(DateTime, server_default=func.now())
    last_used    = Column(DateTime)
    is_active    = Column(Boolean, default=True)

    workspace    = relationship("Workspace", back_populates="api_keys")


# ─── PROVIDERS ───────────────────────────────────────────────────────────

class Provider(Base):
    __tablename__ = "providers"

    id           = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    type         = Column(Enum(ProviderType), nullable=False)
    name         = Column(String)
    api_key_enc  = Column(String)
    base_url     = Column(String)
    is_active    = Column(Boolean, default=True)
    connected_at = Column(DateTime, server_default=func.now())
    last_sync    = Column(DateTime)

    workspace    = relationship("Workspace", back_populates="providers")

    __table_args__ = (UniqueConstraint("workspace_id", "type"),)


class ModelPricing(Base):
    __tablename__ = "model_pricing"

    id                = Column(String, primary_key=True, default=gen_id)
    provider          = Column(Enum(ProviderType), nullable=False)
    model_id          = Column(String, nullable=False)
    model_display     = Column(String)
    input_cost_per_1k = Column(Float, nullable=False)
    output_cost_per_1k= Column(Float, nullable=False)
    cache_cost_per_1k = Column(Float, default=0.0)
    valid_from        = Column(DateTime, server_default=func.now())
    valid_until       = Column(DateTime)

    __table_args__ = (
        UniqueConstraint("provider", "model_id", "valid_from"),
        Index("ix_model_pricing_lookup", "provider", "model_id"),
    )


# ─── FEATURES ────────────────────────────────────────────────────────────

class Feature(Base):
    __tablename__ = "features"

    id           = Column(String, primary_key=True, default=gen_id)
    workspace_id = Column(String, ForeignKey("workspaces.id"), nullable=False)
    slug         = Column(String, nullable=False)
    name         = Column(String, nullable=False)
    description  = Column(Text)
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, server_default=func.now())

    revenue_config = Column(JSON, default=dict)

    workspace    = relationship("Workspace",    back_populates="features")
    transactions = relationship("Transaction",  back_populates="feature")
    budgets      = relationship("Budget",       back_populates="feature")

    __table_args__ = (UniqueConstraint("workspace_id", "slug"),)


# ─── CUSTOMERS ───────────────────────────────────────────────────────────

class Customer(Base):
    __tablename__ = "customers"

    id                = Column(String, primary_key=True, default=gen_id)
    workspace_id      = Column(String, ForeignKey("workspaces.id"), nullable=False)
    external_id       = Column(String, nullable=False)
    email             = Column(String)
    name              = Column(String)

    billing_source    = Column(Enum(BillingSource), default=BillingSource.MANUAL)
    stripe_customer_id= Column(String)
    stripe_price_id   = Column(String)
    plan_name         = Column(String)
    plan_price_monthly= Column(Float, default=0.0)

    first_seen        = Column(DateTime, server_default=func.now())
    last_active       = Column(DateTime)

    workspace         = relationship("Workspace",    back_populates="customers")
    transactions      = relationship("Transaction",  back_populates="customer")

    __table_args__ = (UniqueConstraint("workspace_id", "external_id"),)


class BillingConnection(Base):
    __tablename__ = "billing_connections"

    id                    = Column(String, primary_key=True, default=gen_id)
    workspace_id          = Column(String, ForeignKey("workspaces.id"), unique=True)
    source                = Column(Enum(BillingSource), nullable=False)
    stripe_secret_key_enc = Column(String)
    stripe_webhook_secret = Column(String)
    last_sync             = Column(DateTime)
    is_active             = Column(Boolean, default=True)
    created_at            = Column(DateTime, server_default=func.now())

    workspace             = relationship("Workspace", back_populates="billing_conn")


# ─── TRANSACTIONS ─────────────────────────────────────────────────────────

class Transaction(Base):
    __tablename__ = "transactions"

    id              = Column(String, primary_key=True, default=gen_id)
    workspace_id    = Column(String, ForeignKey("workspaces.id"), nullable=False, index=True)
    feature_id      = Column(String, ForeignKey("features.id"), index=True)
    customer_id     = Column(String, ForeignKey("customers.id"), index=True)

    external_user_id= Column(String, index=True)
    session_id      = Column(String, index=True)
    request_id      = Column(String, unique=True)

    type            = Column(Enum(TransactionType), default=TransactionType.LLM_CALL)
    provider        = Column(Enum(ProviderType))
    model           = Column(String)
    endpoint        = Column(String)

    input_tokens    = Column(Integer, default=0)
    output_tokens   = Column(Integer, default=0)
    cache_tokens    = Column(Integer, default=0)

    input_cost      = Column(Float, default=0.0)
    output_cost     = Column(Float, default=0.0)
    cache_cost      = Column(Float, default=0.0)
    total_cost      = Column(Float, default=0.0)

    latency_ms      = Column(Integer)
    ttfb_ms         = Column(Integer)
    error           = Column(String)

    wallet_deducted      = Column(Boolean, default=False)
    wallet_balance_after = Column(Float)

    created_at      = Column(DateTime, server_default=func.now(), index=True)
    request_metadata = Column(JSON, default=dict)

    workspace       = relationship("Workspace",  back_populates="transactions")
    feature         = relationship("Feature",    back_populates="transactions")
    customer        = relationship("Customer",   back_populates="transactions")

    __table_args__ = (
        Index("ix_tx_workspace_created", "workspace_id", "created_at"),
        Index("ix_tx_feature_created",   "feature_id",   "created_at"),
        Index("ix_tx_customer_created",  "customer_id",  "created_at"),
    )


# ─── WALLET ───────────────────────────────────────────────────────────────

class Wallet(Base):
    __tablename__ = "wallets"

    id              = Column(String, primary_key=True, default=gen_id)
    workspace_id    = Column(String, ForeignKey("workspaces.id"), unique=True)
    balance         = Column(Float, default=0.0)
    total_deposited = Column(Float, default=0.0)
    total_spent     = Column(Float, default=0.0)
    currency        = Column(String, default="USD")

    auto_refill_enabled   = Column(Boolean, default=False)
    auto_refill_threshold = Column(Float, default=10.0)
    auto_refill_amount    = Column(Float, default=100.0)

    freeze_at_zero        = Column(Boolean, default=True)
    stripe_payment_method = Column(String)

    updated_at      = Column(DateTime, server_default=func.now(), onupdate=func.now())

    workspace       = relationship("Workspace", back_populates="wallet")
    entries         = relationship("WalletEntry", back_populates="wallet", cascade="all, delete")


class WalletEntry(Base):
    __tablename__ = "wallet_entries"

    id          = Column(String, primary_key=True, default=gen_id)
    wallet_id   = Column(String, ForeignKey("wallets.id"), nullable=False, index=True)
    type        = Column(Enum(TransactionType), nullable=False)
    amount      = Column(Float, nullable=False)
    balance_after = Column(Float, nullable=False)
    description = Column(String)
    reference_id= Column(String)
    created_at  = Column(DateTime, server_default=func.now(), index=True)

    wallet      = relationship("Wallet", back_populates="entries")


# ─── BUDGETS ──────────────────────────────────────────────────────────────

class Budget(Base):
    __tablename__ = "budgets"

    id            = Column(String, primary_key=True, default=gen_id)
    workspace_id  = Column(String, ForeignKey("workspaces.id"), nullable=False)
    feature_id    = Column(String, ForeignKey("features.id"))
    name          = Column(String, nullable=False)
    scope         = Column(Enum(BudgetScope), nullable=False, default=BudgetScope.FEATURE)
    period        = Column(Enum(BudgetPeriod), nullable=False, default=BudgetPeriod.MONTHLY)
    limit_amount  = Column(Float, nullable=False)

    policy        = Column(Enum(BudgetPolicy), nullable=False, default=BudgetPolicy.ALERT_ONLY)
    alert_threshold = Column(Float, default=0.8)
    fallback_model  = Column(String)
    upgrade_url     = Column(String)

    notify_email  = Column(Boolean, default=True)
    notify_slack  = Column(Boolean, default=False)
    slack_webhook = Column(String)

    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, server_default=func.now())

    workspace     = relationship("Workspace", back_populates="budgets")
    feature       = relationship("Feature",   back_populates="budgets")


# ─── ROUTING RULES ────────────────────────────────────────────────────────

class RoutingRule(Base):
    __tablename__ = "routing_rules"

    id            = Column(String, primary_key=True, default=gen_id)
    workspace_id  = Column(String, ForeignKey("workspaces.id"), nullable=False)
    name          = Column(String, nullable=False)
    description   = Column(Text)
    priority      = Column(Integer, default=0)

    trigger       = Column(JSON, nullable=False)
    action        = Column(JSON, nullable=False)

    is_active     = Column(Boolean, default=True)
    created_at    = Column(DateTime, server_default=func.now())

    trigger_count = Column(Integer, default=0)
    last_triggered= Column(DateTime)

    workspace     = relationship("Workspace", back_populates="routing_rules")


# ─── ALERTS ───────────────────────────────────────────────────────────────

class Alert(Base):
    __tablename__ = "alerts"

    id            = Column(String, primary_key=True, default=gen_id)
    workspace_id  = Column(String, ForeignKey("workspaces.id"), nullable=False, index=True)
    type          = Column(Enum(AlertType), nullable=False)
    severity      = Column(Enum(AlertSeverity), nullable=False)
    title         = Column(String, nullable=False)
    body          = Column(Text)
    alert_metadata = Column(JSON, default=dict)

    is_read       = Column(Boolean, default=False)
    is_resolved   = Column(Boolean, default=False)
    resolved_at   = Column(DateTime)

    created_at    = Column(DateTime, server_default=func.now(), index=True)

    workspace     = relationship("Workspace", back_populates="alerts")


# ─── BENCHMARK DATA ───────────────────────────────────────────────────────

class BenchmarkSnapshot(Base):
    __tablename__ = "benchmark_snapshots"

    id            = Column(String, primary_key=True, default=gen_id)
    period_start  = Column(DateTime, nullable=False)
    period_end    = Column(DateTime, nullable=False)

    category      = Column(String)
    sample_size   = Column(Integer)
    median_margin = Column(Float)
    p25_margin    = Column(Float)
    p75_margin    = Column(Float)
    avg_cost_per_request = Column(Float)
    avg_tokens_per_request = Column(Float)

    created_at    = Column(DateTime, server_default=func.now())

    __table_args__ = (Index("ix_bench_period_category", "period_start", "category"),)
