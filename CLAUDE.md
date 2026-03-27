# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Meridian is an AI margin intelligence SaaS platform that tracks LLM feature costs per user. It consists of a FastAPI backend, single-file HTML frontends (no build step), and a Python SDK.

## Architecture

**Monorepo with four components:**
- `backend/` — FastAPI REST API (Python 3.11, SQLAlchemy, PostgreSQL)
- `frontend/index.html` — Dashboard SPA (vanilla HTML/CSS/JS, Clerk auth)
- `landing/index.html` — Marketing landing page
- `sdk/` — Python SDK package (`meridian-sdk`) for LLM proxy integration

**Entry point:** `app.py` dynamically loads `backend/indexv8_3_5.py` via `importlib`.

**Versioning convention:** Backend modules use `v8_3_5` suffix (e.g., `modelsv8_3_5.py`, `authv8_3_5.py`). When creating new versions, update `app.py` to point to the new index file and keep imports consistent across all backend modules.

**Key backend modules:**
- `indexv8_3_5.py` — FastAPI app, all route definitions, auth middleware
- `modelsv8_3_5.py` — SQLAlchemy ORM models (User, Workspace, Transaction, Budget, etc.)
- `databasev8_3_5.py` — DB engine, session factory, table creation
- `authv8_3_5.py` — JWT creation, Clerk JWKS verification, API key generation
- `analyticsv8_3_5.py` — Read queries (margin signals, ledgers, anomalies)
- `proxyv8_3_5.py` — SDK proxy logic: routing, wallet checks, cost calculation
- `encryptionv8_3_5.py` — Fernet encryption for stored provider API keys
- `email_servicev8_3_5.py` — Resend transactional emails

**Auth:** Dual-mode — Clerk JWKS (primary, RS256) + email/password JWT (fallback, HS256). `decode_token()` tries HS256 first, then Clerk RS256.

**Frontend:** No npm, no build tooling. Single HTML files served statically by FastAPI at `/app` and `/`.

## Development Commands

```bash
# First-time setup
bash setup.sh

# Run locally (uses SQLite by default, Postgres via DATABASE_URL)
cd backend && uvicorn indexv8_3_5:app --reload
# App: http://localhost:8000/app
# API docs: http://localhost:8000/docs

# Install backend dependencies
pip install -r backend/requirementsv8_3_5.txt

# Run the production entry point locally
uvicorn app:app --host 0.0.0.0 --port 8000
```

There is no test suite, linter, or formatter configured in this repo.

## Deployment

- **Backend:** Railway via Dockerfile (Python 3.11-slim, uvicorn, `$PORT` from env)
- **Frontend routing:** Vercel proxies API/webhook/proxy routes to Railway; serves HTML directly
- **Config files:** `Dockerfile`, `nixpacks.toml`, `railway.json`, `vercel.json`

## Key Environment Variables

`SECRET_KEY`, `ENCRYPTION_KEY`, `DATABASE_URL`, `APP_URL`, `CLERK_JWKS_URL`, `RESEND_API_KEY`, `STRIPE_SECRET_KEY`. See `.env.example` or `setup.sh` for generation commands.

## Important Patterns

- Database URL rewrite: `postgres://` is automatically rewritten to `postgresql://` for SQLAlchemy compatibility (Railway uses the former)
- Old version files (v8_3_3 and earlier) are kept alongside current v8_3_5 files — do not delete them
- The SDK (`sdk/meridian_sdk/`) has zero external dependencies and uses a background thread for fire-and-forget usage ingestion
- Model pricing is seeded at startup in `indexv8_3_5.py` — update there when adding new LLM models
