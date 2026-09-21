# One image, several processes. The API and every worker pool run the same
# code; what differs is the command, which the host's service definition sets.
# Building one image keeps them on the same commit by construction — a schema
# change and the worker that depends on it cannot be deployed out of step.
#
# Deliberately NOT split into a dependency layer and a source layer. The usual
# trick (copy pyproject, install, then copy the source) relies on setuptools
# building an empty wheel when the package directory is absent, which is a
# behaviour to verify rather than assume. A slower rebuild is worth more than
# a Dockerfile whose caching is load-bearing and untested.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# psycopg comes from the binary wheel, so there is no libpq and no compiler in
# this image. api/tests/ is excluded by .dockerignore.
COPY pyproject.toml ./
COPY api/ ./api/
COPY db/ ./db/
RUN pip install --no-cache-dir .

# Nothing here needs root, and the crawler fetches attacker-chosen URLs for a
# living.
RUN useradd --create-home --uid 10001 app && chown -R app /app
USER app

EXPOSE 8000

# Overridden per service. The API is the default because it is the one that
# has to answer a health check.
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
