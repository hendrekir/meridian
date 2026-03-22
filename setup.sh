#!/bin/bash
# Meridian v8.2 — local dev setup
# Run once after cloning: bash setup.sh

set -e

echo "→ Setting up Meridian v8.2..."

# Check Python
python3 --version || { echo "Python 3.11+ required"; exit 1; }

# Create .env if missing
if [ ! -f .env ]; then
  cp .env.example .env
  echo "→ Created .env from .env.example"
  echo "  !! Fill in SECRET_KEY and ENCRYPTION_KEY before starting !!"
fi

# Generate keys if placeholders still present
if grep -q "CHANGE_ME_GENERATE_A_REAL_SECRET" .env; then
  SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
  sed -i.bak "s/CHANGE_ME_GENERATE_A_REAL_SECRET/$SECRET/" .env
  echo "→ Generated SECRET_KEY"
fi

if grep -q "CHANGE_ME_GENERATE_A_FERNET_KEY" .env; then
  FERNET=$(python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
  # Fernet keys contain + and / — use | as sed delimiter
  sed -i.bak "s|CHANGE_ME_GENERATE_A_FERNET_KEY|$FERNET|" .env
  echo "→ Generated ENCRYPTION_KEY"
fi
rm -f .env.bak

# Install dependencies
cd backend
pip install -r requirementsv8.2.txt --quiet
echo "→ Python dependencies installed"
cd ..

echo ""
echo "✓ Setup complete. Start with:"
echo "  cd backend && uvicorn indexv8.2:app --reload"
echo ""
echo "  App: http://localhost:8000/app"
echo "  API docs: http://localhost:8000/docs"
echo ""
echo "  Fill in CLERK_JWKS_URL, RESEND_API_KEY, and DATABASE_URL in .env"
echo "  before deploying to Railway."
