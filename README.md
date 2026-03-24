# Meridian v8.3

AI margin intelligence — track what your LLM features actually cost per user.

## Quick start (local)

```bash
bash setup.sh
cd backend && uvicorn indexv8.3:app --reload
# → http://localhost:8000/app
```

## File map

```
meridian/
├── backend/
│   ├── indexv8.3.py          ← FastAPI app (main entry point)
│   ├── modelsv8.3.py         ← SQLAlchemy models
│   ├── databasev8.3.py       ← DB engine + session
│   ├── authv8.3.py           ← JWT + Clerk JWKS + password hashing
│   ├── analyticsv8.3.py      ← All read queries
│   ├── proxyv8.3.py          ← Routing rules, wallet, transaction recording
│   ├── encryptionv8.3.py     ← Fernet encryption for stored API keys
│   ├── email_servicev8.3.py  ← Resend transactional emails
│   └── requirementsv8.3.txt
├── frontend/
│   └── indexv8.3.html        ← Single-file frontend
├── .env.example               ← Copy to .env and fill in
├── .gitignore
├── nixpacks.toml              ← Railway build config
├── railway.json               ← Railway deploy config
├── vercel.json                ← Vercel → Railway proxy
└── setup.sh                   ← One-shot local setup
```

## Required env vars

| Variable | Where to get it |
|---|---|
| `SECRET_KEY` | `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `ENCRYPTION_KEY` | `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `DATABASE_URL` | Railway Postgres plugin (auto-injected) |
| `APP_URL` | Your Railway domain, e.g. `https://meridian.up.railway.app` |
| `CLERK_JWKS_URL` | `https://clerk.meridianvisual.io/.well-known/jwks.json` |
| `RESEND_API_KEY` | [resend.com](https://resend.com) → API Keys |
| `STRIPE_SECRET_KEY` | Stripe dashboard → Developers → API keys |

## Deploy to Railway

1. Push this repo to GitHub
2. New project in Railway → Deploy from GitHub
3. Add Postgres plugin → Railway injects `DATABASE_URL` automatically
4. Set env vars in Railway → Variables (copy from `.env.example`)
5. Railway builds via `nixpacks.toml` automatically

## Deploy frontend to Vercel

1. Update `vercel.json` — replace `YOUR-APP.up.railway.app` with your Railway URL
2. `vercel --prod` from the project root
3. Set `window.__MERIDIAN_API_URL__` in Vercel env vars if needed

## Auth flow

- **Clerk (primary)**: frontend gets a JWT from Clerk, sends it as `Authorization: Bearer <token>`
  - Backend verifies via Clerk's JWKS endpoint
  - New users are auto-provisioned with a workspace on first sign-in
- **Email/password (fallback)**: `POST /api/auth/register` and `POST /api/auth/login`
  - Returns a HS256 JWT, works the same way

## What was fixed in v8.3

- Added 5 missing backend modules (`database`, `auth`, `analytics`, `proxy`, `encryption`)
- Clerk JWKS verification — backend now correctly verifies Clerk-issued JWTs
- CORS — fixed to include production domains, not just localhost
- SQLite → Postgres — Railway `postgres://` URL rewrite handled automatically
- Budget query bug — SQLAlchemy `None` filter no longer inflates all spend figures
- Password reset — full flow: request email → verify token → set new password
- Demo mode — only activates on localhost, never silently for real users
- `clerk_id` column added to `User` model for Clerk user linking
- `alert_metadata` column renamed (was conflicting with SQLAlchemy reserved word)
