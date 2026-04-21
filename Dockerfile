FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Athens

WORKDIR /app

# Deps first for better Docker-layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code
COPY src/ ./src/

# Railway cron runs this command daily.
# --use-cache hits the existing researched_subjects cache (fast + cheap).
CMD ["python", "-u", "src/cron.py", "--use-cache"]
