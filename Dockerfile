FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin sync
USER 10001

ENTRYPOINT ["python", "/app/sync.py"]
