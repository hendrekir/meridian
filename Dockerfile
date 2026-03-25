FROM python:3.11-slim
WORKDIR /app
COPY backend/requirementsv8.3.3.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENTRYPOINT ["uvicorn", "app:app", "--host", "0.0.0.0"]
CMD ["--port", "8080"]
