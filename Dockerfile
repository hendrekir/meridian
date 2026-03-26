FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY backend/requirementsv8.3.3.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
SHELL ["/bin/sh", "-c"]
CMD uvicorn app:app --host 0.0.0.0 --port $PORT
