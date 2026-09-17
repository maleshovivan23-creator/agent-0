FROM python:3.11-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY agent ./agent
COPY docs ./docs
COPY README.md ./
RUN mkdir -p data reports && useradd -m agent0 && chown -R agent0 /app
USER agent0

# Одноразовый проход: docker compose run --rm autopilot python -m agent.main autopilot --once
CMD ["python", "-m", "agent.main", "autopilot", "--once"]
