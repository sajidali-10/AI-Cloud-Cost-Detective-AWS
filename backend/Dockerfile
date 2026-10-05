# Backend Dockerfile — Phase 0
# - Pinned base image (no `latest`).
# - Non-root user.
# - No baked-in secrets.
FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create a non-root user and a clean workdir.
RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app
WORKDIR /app

# Install OS deps needed for psycopg binary wheels (none extra on slim-bookworm),
# then Python deps in a separate layer for better caching.
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy the rest of the source.
COPY app /app/app
COPY alembic /app/alembic
COPY alembic.ini /app/alembic.ini
COPY tests /app/tests
COPY pytest.ini /app/pytest.ini

# Pre-create ``/app/.pytest_cache`` so the non-root ``app`` user can
# write pytest's runtime cache without having to ``chmod 777`` on
# ``/app``.  The parent directory ``/app`` stays root-owned so the
# running application cannot mutate its own source tree; only the
# cache subdirectory is handed over to ``app``.  pytest.ini leaves
# ``cache_dir`` at its default (``<rootdir>/.pytest_cache``) so
# local development and CI share the same configuration.
RUN mkdir -p /app/.pytest_cache \
    && chown app:app /app/.pytest_cache \
    && chmod 755 /app/.pytest_cache

# Drop privileges.
USER app

EXPOSE 8000

# Default entrypoint: uvicorn binds to all interfaces on 8000.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
