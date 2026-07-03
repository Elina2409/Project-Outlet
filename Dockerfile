# Image for the Cloud Run Job fallback (Tier 2). Runs one full scrape and
# exits; the CSV it writes is ephemeral - this path is for log-based
# debugging, persistence lives on the GitHub-runner path.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY . .

CMD ["python", "main.py"]
