# The control plane image. One image, two commands: `api` and `worker`.
#
# It carries the docker and git CLIs because that is what the engine drives.
# It does NOT carry a Docker daemon — it talks to the host's, through the
# socket mounted into it by docker-compose.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl gnupg \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg \
        -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) \
signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        docker-ce-cli docker-buildx-plugin \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY pyproject.toml ./
COPY forge ./forge
COPY db ./db
RUN pip install --no-cache-dir .

# Root, and unusually this is correct rather than lazy: the process's whole
# job is to drive /var/run/docker.sock, and access to that socket is already
# equivalent to root on the host. Dropping to an unprivileged user here would
# be a gesture — the user would still need to be in the docker group, which
# grants the same thing.
CMD ["uvicorn", "forge.main:app", "--host", "0.0.0.0", "--port", "8000"]
