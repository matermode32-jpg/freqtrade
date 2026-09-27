FROM python:3.12-slim

WORKDIR /poster
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

# Render injects $PORT; shell form expands it. No secrets baked in.
CMD sh -c "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"
