FROM python:3.12-slim

# Create non-root user before anything else
RUN useradd -m -u 1000 appuser

WORKDIR /app

# Install system deps: cron for nightly sync, curl for healthcheck
RUN apt-get update && apt-get install -y cron curl && rm -rf /var/lib/apt/lists/*

# Install Python dependencies + Playwright Chromium (needed for LinkedIn sync)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium --with-deps

# Copy app code
COPY *.py ./
COPY .env.example .env.example
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Data directories — owned by appuser so the app can write
RUN mkdir -p /app/data /app/pitches && chown -R appuser:appuser /app

EXPOSE 8501

HEALTHCHECK CMD curl -f http://localhost:8501/_stcore/health || exit 1

USER appuser

# Entrypoint starts cron (nightly sync) then Streamlit
CMD ["/docker-entrypoint.sh"]
