# Meridian v8.3 — Deployment Guide
# Replacing whatever is currently live at meridianvisual.io

## The situation
Your domain (meridianvisual.io) has a different product deployed — not this one.
This guide replaces it completely with Meridian v8.3 (the AI margin dashboard).

---

## Step 1 — Local setup (do this first, test before deploying)

```bash
# Unzip into a clean folder
unzip meridianv8.3_final.zip -d ~/projects/meridianv8.3
cd ~/projects/meridianv8.3

# Run setup (auto-generates SECRET_KEY and ENCRYPTION_KEY)
bash setup.sh

# Open in VS Code
code .

# Start locally
cd backend
uvicorn indexv8.3:app --reload --port 8000

# Visit: http://localhost:8000/app
# You should see the Meridian v8.3 margin dashboard
```

---

## Step 2 — Railway (backend)

### If you have an existing Railway project at meridianvisual.io:

1. Go to railway.app → your project
2. **Delete or disconnect** the existing service (the old code)
3. Create a **New Service** → Deploy from GitHub repo
4. Push this folder to a GitHub repo first:

```bash
cd ~/projects/meridianv8.3
git init
git add .
git commit -m "meridian v8.3"
git remote add origin https://github.com/YOUR_USERNAME/meridianv8.3.git
git push -u origin main
```

5. In Railway: New Service → GitHub → select your repo
6. Add **Postgres** plugin (Railway dashboard → + New → Database → Postgres)
   - Railway auto-injects DATABASE_URL — you don't need to set it manually
7. Set these **environment variables** in Railway → Variables:

```
APP_URL              = https://meridianvisual.io
SECRET_KEY           = (copy from your .env — setup.sh generated this)
ENCRYPTION_KEY       = (copy from your .env — setup.sh generated this)
CLERK_JWKS_URL       = https://clerk.meridianvisual.io/.well-known/jwks.json
RESEND_API_KEY       = re_xxxx (from resend.com)
EMAIL_FROM           = Meridian <noreply@meridianvisual.io>
EXTRA_ORIGINS        = https://meridianvisual.io,https://www.meridianvisual.io
```

8. Railway will build using `nixpacks.toml` automatically
9. Start command (already in `railway.json`): `cd backend && uvicorn indexv8.3:app --host 0.0.0.0 --port $PORT`

---

## Step 3 — Vercel (frontend + landing page)

The frontend and landing page are static HTML files — serve them from Vercel.
The `vercel.json` proxies all `/api/*` calls to your Railway backend.

**Before deploying, update vercel.json:**
Replace `YOUR-APP.up.railway.app` with your actual Railway URL.

```bash
# Edit vercel.json — replace the placeholder
# Find your Railway URL: railway.app → your project → Settings → Domains

# Then deploy:
cd ~/projects/meridianv8.3
npx vercel --prod
```

Vercel will:
- Serve `landing/indexv8.3.html` at `/` (the new landing page)
- Serve `frontend/indexv8.3.html` at `/app` (the dashboard)
- Proxy `/api/*` to Railway

**Point your domain:**
In Vercel → Settings → Domains → add `meridianvisual.io`
This replaces whatever was there before.

---

## Step 4 — Verify it's working

```bash
# 1. Landing page
curl https://meridianvisual.io
# Should return: "You're probably losing money on 40% of your AI users"

# 2. Health check
curl https://meridianvisual.io/api/health
# Should return: {"status":"ok","version":"8.3.0",...}

# 3. Dashboard
# Visit: https://meridianvisual.io/app
# Should show: Meridian v8.3 — AI Margin Intelligence (with Clerk sign-in)
```

---

## File structure reminder

```
meridianv8.3/
├── backend/          ← Deploy to Railway (Python/FastAPI)
│   ├── indexv8_3_2.py  ← Entry point
│   └── *.py
├── frontend/
│   └── indexv8.3.html  ← Dashboard — served at /app
├── landing/
│   └── indexv8.3.html  ← Landing page — served at /
├── sdk/              ← pip install meridian-sdk (publish to PyPI separately)
├── nixpacks.toml     ← Railway build config
├── railway.json      ← Railway deploy config
├── vercel.json       ← Vercel routing config (UPDATE the Railway URL)
└── .env.example      ← Copy to .env, fill in secrets
```

---

## Why the wrong product was showing

The domain had different code deployed (a "system skeleton" tool, not the margin dashboard).
Once you deploy v8.3 via Vercel and point meridianvisual.io at the new Vercel deployment,
the old product disappears and v8.3 takes over.

The old product is completely gone from your codebase — every file in this zip is v8.3 only.
