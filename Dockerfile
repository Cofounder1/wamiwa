# Wamiwa API image. The same image runs the API and the scheduled-jobs worker (see docker-compose.yml).
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so code changes don't reinstall them.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
        > requirements.txt && pip install -r requirements.txt && rm requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

# Run as a non-root user that owns only the uploads folder.
RUN useradd --create-home --uid 1000 wamiwa && mkdir -p /app/uploads && chown wamiwa:wamiwa /app/uploads
USER wamiwa

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=4)"

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app_factory --factory --host 0.0.0.0 --port 8000 --proxy-headers"]
