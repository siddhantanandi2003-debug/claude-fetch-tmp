FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb xauth ca-certificates fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir patchright

# installs chromium + OS dependencies (skip browser download of apt deps already present)
RUN patchright install --with-deps chromium

WORKDIR /work
