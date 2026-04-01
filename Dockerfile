FROM python:3.12-slim

WORKDIR /app

# Install system deps: cron for nightly sync, curl for healthcheck
RUN apt-get update && apt-get install -y cron curl && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY *.py ./
COPY .env.example .env.example
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Data directories (mounted as volumes at runtime)
RUN mkdir -p /app/data /app/pitches

EXPOSE 8501

HEALTHCHECK CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Entrypoint starts cron (nightly sync) then Streamlit
CMD ["/docker-entrypoint.sh"]
