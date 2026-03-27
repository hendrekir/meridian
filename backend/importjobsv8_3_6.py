"""
Meridian v8.3.6 — importjobsv8_3_6.py
Provider key validation + usage CSV import.

Validation: makes a cheap real API call to verify each key works.
  - OpenAI    → GET /v1/models
  - Anthropic → POST /v1/messages (1 output token, haiku)
  - Google    → GET /v1beta/models?key=...
  - Mistral   → GET /v1/models
  - OpenRouter→ GET /api/v1/models
  - Cohere    → GET /v2/models

CSV import: parses provider usage exports into Transaction records.
  - OpenAI    → platform.openai.com/usage CSV export
  - Anthropic → console.anthropic.com usage CSV export
  - Generic   → any CSV with date + model + input/output token columns

request_id is deterministic per (workspace, date, provider, model) so
re-importing the same CSV is always idempotent — no duplicates.
"""
from __future__ import annotations

import csv
import io
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx
from sqlalchemy.orm import Session


# ── VALIDATION ────────────────────────────────────────────────────────────

_VALIDATE_TIMEOUT = 12.0  # seconds


async def validate_key(provider_type: str, api_key: str) -> dict:
    """
    Makes a real but cheap API call to confirm the key is accepted.
    Returns {"ok": bool, "error": str|None, "models": list[str]}.
    """
    try:
        if provider_type == "openai":
            return await _validate_openai(api_key)
        if provider_type == "anthropic":
            return await _validate_anthropic(api_key)
        if provider_type == "google":
            return await _validate_google(api_key)
        if provider_type == "mistral":
            return await _validate_mistral(api_key)
        if provider_type == "openrouter":
            return await _validate_openrouter(api_key)
        if provider_type == "cohere":
            return await _validate_cohere(api_key)
        return {"ok": False, "error": f"Unknown provider: {provider_type}", "models": []}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "models": []}


async def _validate_openai(api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as c:
        r = await c.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if r.status_code == 401:
        return {"ok": False, "error": "Invalid API key", "models": []}
    if r.status_code == 429:
        return {"ok": True, "error": None, "models": ["rate-limited-but-valid"]}
    r.raise_for_status()
    data   = r.json()
    models = sorted({m["id"] for m in data.get("data", []) if "gpt" in m["id"] or "o1" in m["id"] or "o3" in m["id"]})[:12]
    return {"ok": True, "error": None, "models": models}


async def _validate_anthropic(api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-haiku-4-5-20251001",
                "max_tokens": 1,
                "messages":   [{"role": "user", "content": "hi"}],
            },
        )
    if r.status_code == 401:
        return {"ok": False, "error": "Invalid API key", "models": []}
    if r.status_code == 400:
        # Bad request but key was accepted = key is valid
        return {"ok": True, "error": None, "models": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]}
    r.raise_for_status()
    return {"ok": True, "error": None, "models": ["claude-haiku-4-5-20251001", "claude-sonnet-4-6", "claude-opus-4-6"]}


async def _validate_google(api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as c:
        r = await c.get(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        )
    if r.status_code in (400, 401, 403):
        return {"ok": False, "error": "Invalid API key", "models": []}
    r.raise_for_status()
    data   = r.json()
    models = [m["name"].split("/")[-1] for m in data.get("models", []) if "gemini" in m.get("name", "")][:8]
    return {"ok": True, "error": None, "models": models}


async def _validate_mistral(api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as c:
        r = await c.get(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if r.status_code == 401:
        return {"ok": False, "error": "Invalid API key", "models": []}
    r.raise_for_status()
    data   = r.json()
    models = [m["id"] for m in data.get("data", [])][:8]
    return {"ok": True, "error": None, "models": models}


async def _validate_openrouter(api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as c:
        r = await c.get(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if r.status_code == 401:
        return {"ok": False, "error": "Invalid API key", "models": []}
    r.raise_for_status()
    data   = r.json()
    models = [m["id"] for m in data.get("data", [])][:8]
    return {"ok": True, "error": None, "models": models}


async def _validate_cohere(api_key: str) -> dict:
    async with httpx.AsyncClient(timeout=_VALIDATE_TIMEOUT) as c:
        r = await c.get(
            "https://api.cohere.com/v2/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
    if r.status_code == 401:
        return {"ok": False, "error": "Invalid API key", "models": []}
    r.raise_for_status()
    data   = r.json()
    models = [m["name"] for m in data.get("models", [])][:8]
    return {"ok": True, "error": None, "models": models}


# ── BACKGROUND VALIDATION TASK ────────────────────────────────────────────

async def run_validation(db: Session, workspace_id: str, provider_id: str, raw_api_key: str):
    """
    Called as a FastAPI BackgroundTask after connect_provider() returns.
    Validates the key, writes result back to provider record.
    """
    from modelsv8_3_6 import Provider

    prov = db.query(Provider).filter(Provider.id == provider_id).first()
    if not prov:
        return

    prov.import_status = "validating"
    db.commit()

    try:
        result = await validate_key(str(prov.type.value if hasattr(prov.type, "value") else prov.type), raw_api_key)
        if result["ok"]:
            prov.import_status     = "ready"
            prov.import_error      = None
            prov.validated_at      = datetime.utcnow()
            prov.available_models  = result["models"]
        else:
            prov.import_status = "error"
            prov.import_error  = result["error"]
    except Exception as e:
        prov.import_status = "error"
        prov.import_error  = str(e)[:200]

    db.commit()


# ── CSV PARSING ───────────────────────────────────────────────────────────

def _detect_format(headers: list[str]) -> str:
    """Returns 'openai' | 'anthropic' | 'generic'."""
    h = {c.lower().strip() for c in headers}
    if "usage type" in h or "snapshot_id" in h:
        return "openai"
    if "cache_read_tokens" in h or "cache_write_tokens" in h:
        return "anthropic"
    return "generic"


def _make_request_id(workspace_id: str, date: str, provider: str, model: str) -> str:
    """Deterministic request ID — re-importing the same CSV never duplicates rows."""
    key = f"{workspace_id}:{date}:{provider}:{model}"
    return "import-" + hashlib.sha256(key.encode()).hexdigest()[:32]


def parse_usage_csv(csv_text: str, provider_type: str, workspace_id: str) -> list[dict]:
    """
    Parse a provider usage CSV export into a list of transaction dicts.
    Each dict maps directly to Transaction model fields.

    Supported formats:
      OpenAI (platform.openai.com/usage → Export CSV):
        Date, Usage type, Project, Model, Input tokens, Output tokens, Requests, Cost
      Anthropic (console.anthropic.com → Usage → Export):
        date, model, input_tokens, output_tokens, cache_read_tokens,
        cache_write_tokens, requests, total_cost
      Generic (any CSV with date + model + input/output token columns)
    """
    rows_out: list[dict] = []
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    raw_headers = reader.fieldnames or []
    fmt = _detect_format(raw_headers)

    for row in reader:
        try:
            tx = _parse_row(row, fmt, provider_type, workspace_id)
            if tx:
                rows_out.append(tx)
        except Exception:
            continue  # skip malformed rows silently

    return rows_out


def _parse_row(row: dict, fmt: str, provider_type: str, workspace_id: str) -> Optional[dict]:
    """Parse a single CSV row into a transaction dict."""
    # Lowercase all keys for uniform access
    r = {k.lower().strip(): v.strip() if isinstance(v, str) else v for k, v in row.items()}

    if fmt == "openai":
        return _parse_openai_row(r, provider_type, workspace_id)
    if fmt == "anthropic":
        return _parse_anthropic_row(r, provider_type, workspace_id)
    return _parse_generic_row(r, provider_type, workspace_id)


def _parse_openai_row(r: dict, provider_type: str, workspace_id: str) -> Optional[dict]:
    date_str     = r.get("date", "")
    model        = r.get("model") or r.get("snapshot_id", "")
    usage_type   = r.get("usage type", "completion").lower()
    input_tokens = _int(r.get("input tokens") or r.get("n_context_tokens_total", 0))
    output_tokens= _int(r.get("output tokens") or r.get("n_generated_tokens_total", 0))
    requests     = _int(r.get("requests") or r.get("n_requests", 1))
    cost         = _float(r.get("cost", 0))

    if not model or not date_str or (input_tokens == 0 and output_tokens == 0):
        return None
    if usage_type not in ("completion", "chat"):
        return None  # skip embeddings, dall-e, etc.

    date_str = _normalise_date(date_str)
    if not date_str:
        return None

    return {
        "workspace_id":    workspace_id,
        "request_id":      _make_request_id(workspace_id, date_str, "openai", model),
        "provider":        "openai",
        "model":           model,
        "endpoint":        "/chat/completions",
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "cache_tokens":    0,
        "latency_ms":      0,
        "total_cost":      cost,
        "n_requests":      requests,
        "created_at":      datetime.strptime(date_str, "%Y-%m-%d"),
        "request_metadata":{"source": "csv_import", "date": date_str, "n_requests": requests},
    }


def _parse_anthropic_row(r: dict, provider_type: str, workspace_id: str) -> Optional[dict]:
    date_str      = r.get("date", "")
    model         = r.get("model", "")
    input_tokens  = _int(r.get("input_tokens", 0))
    output_tokens = _int(r.get("output_tokens", 0))
    cache_read    = _int(r.get("cache_read_tokens", 0))
    cache_write   = _int(r.get("cache_write_tokens", 0))
    requests      = _int(r.get("requests", 1))
    cost          = _float(r.get("total_cost", 0))

    if not model or not date_str or (input_tokens == 0 and output_tokens == 0):
        return None

    date_str = _normalise_date(date_str)
    if not date_str:
        return None

    return {
        "workspace_id":    workspace_id,
        "request_id":      _make_request_id(workspace_id, date_str, "anthropic", model),
        "provider":        "anthropic",
        "model":           model,
        "endpoint":        "/messages",
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "cache_tokens":    cache_read + cache_write,
        "latency_ms":      0,
        "total_cost":      cost,
        "n_requests":      requests,
        "created_at":      datetime.strptime(date_str, "%Y-%m-%d"),
        "request_metadata":{"source": "csv_import", "date": date_str, "n_requests": requests,
                            "cache_read": cache_read, "cache_write": cache_write},
    }


def _parse_generic_row(r: dict, provider_type: str, workspace_id: str) -> Optional[dict]:
    date_str      = r.get("date", "") or r.get("day", "")
    model         = r.get("model", "") or r.get("model_id", "")
    input_tokens  = _int(r.get("input_tokens", 0) or r.get("prompt_tokens", 0))
    output_tokens = _int(r.get("output_tokens", 0) or r.get("completion_tokens", 0))
    cost          = _float(r.get("cost", 0) or r.get("total_cost", 0))
    requests      = _int(r.get("requests", 1) or r.get("request_count", 1))

    if not model or not date_str or (input_tokens == 0 and output_tokens == 0):
        return None

    date_str = _normalise_date(date_str)
    if not date_str:
        return None

    return {
        "workspace_id":    workspace_id,
        "request_id":      _make_request_id(workspace_id, date_str, provider_type, model),
        "provider":        provider_type,
        "model":           model,
        "endpoint":        "/completions",
        "input_tokens":    input_tokens,
        "output_tokens":   output_tokens,
        "cache_tokens":    0,
        "latency_ms":      0,
        "total_cost":      cost,
        "n_requests":      requests,
        "created_at":      datetime.strptime(date_str, "%Y-%m-%d"),
        "request_metadata":{"source": "csv_import", "date": date_str, "n_requests": requests},
    }


# ── DB INSERT ─────────────────────────────────────────────────────────────

def import_transactions(db: Session, rows: list[dict]) -> int:
    """
    Insert parsed CSV rows as Transaction records.
    Skips rows whose request_id already exists (idempotent re-import).
    Returns count of newly inserted rows.
    """
    from modelsv8_3_6 import Transaction, TransactionType, ModelPricing

    if not rows:
        return 0

    # Fetch existing request_ids for this workspace in one query
    workspace_id  = rows[0]["workspace_id"]
    existing_ids  = {
        r[0] for r in db.query(Transaction.request_id)
        .filter(Transaction.workspace_id == workspace_id)
        .filter(Transaction.request_id.in_([r["request_id"] for r in rows]))
        .all()
    }

    inserted = 0
    for row in rows:
        if row["request_id"] in existing_ids:
            continue

        # Recalculate cost from pricing table if CSV cost is zero
        total_cost = row["total_cost"]
        if total_cost == 0:
            total_cost = _calc_cost_from_table(
                db, row["provider"], row["model"],
                row["input_tokens"], row["output_tokens"], row["cache_tokens"]
            )

        tx = Transaction(
            workspace_id     = workspace_id,
            feature_id       = None,
            customer_id      = None,
            external_user_id = None,
            session_id       = None,
            request_id       = row["request_id"],
            type             = TransactionType.LLM_CALL,
            provider         = row["provider"],
            model            = row["model"],
            endpoint         = row["endpoint"],
            input_tokens     = row["input_tokens"],
            output_tokens    = row["output_tokens"],
            cache_tokens     = row["cache_tokens"],
            input_cost       = 0.0,
            output_cost      = 0.0,
            cache_cost       = 0.0,
            total_cost       = round(total_cost, 6),
            latency_ms       = row["latency_ms"],
            error            = None,
            wallet_deducted  = False,
            created_at       = row["created_at"],
            request_metadata = row["request_metadata"],
        )
        db.add(tx)
        inserted += 1

    if inserted:
        db.commit()
    return inserted


def _calc_cost_from_table(db: Session, provider: str, model: str,
                          input_tokens: int, output_tokens: int, cache_tokens: int) -> float:
    from modelsv8_3_6 import ModelPricing
    pricing = (
        db.query(ModelPricing)
        .filter(ModelPricing.provider == provider, ModelPricing.model_id == model)
        .order_by(ModelPricing.valid_from.desc())
        .first()
    )
    if not pricing:
        return input_tokens / 1000 * 0.003 + output_tokens / 1000 * 0.015
    return (
        input_tokens  / 1000 * pricing.input_cost_per_1k +
        output_tokens / 1000 * pricing.output_cost_per_1k +
        cache_tokens  / 1000 * (pricing.cache_cost_per_1k or 0)
    )


# ── HELPERS ───────────────────────────────────────────────────────────────

def _int(v) -> int:
    try:
        return int(str(v).replace(",", "").split(".")[0]) if v else 0
    except Exception:
        return 0


def _float(v) -> float:
    try:
        return float(str(v).replace(",", "").lstrip("$").strip()) if v else 0.0
    except Exception:
        return 0.0


def _normalise_date(s: str) -> Optional[str]:
    """Try several date formats, return YYYY-MM-DD or None."""
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
