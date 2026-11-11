# ---------- Stage 1: shrink the AWS price files into aws-cost/ ----------
FROM python:3.11-slim AS aws-prep
WORKDIR /prep
RUN pip install --no-cache-dir ijson
COPY build_aws_slim.py .
COPY aws-raw ./aws-raw
RUN python build_aws_slim.py

# ---------- Stage 2: the real image (no raw files inside) ----------
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY gcp_skus_seed.json ./
COPY --from=aws-prep /prep/aws-cost ./aws-cost

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
