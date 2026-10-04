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

# Drop privileges.
USER app

EXPOSE 8000

# Default entrypoint: uvicorn binds to all interfaces on 8000.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
