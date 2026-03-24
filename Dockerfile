FROM python:3.11-slim

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ .

CMD uvicorn indexv8_3:app --host 0.0.0.0 --port $PORT
