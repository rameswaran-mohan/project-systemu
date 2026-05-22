# syntax=docker/dockerfile:1
FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/rameswaran-mohan/project-systemu"
LABEL org.opencontainers.image.description="Systemu — record any computer workflow once; runs as an autonomous agent with memory + tools + supervisor"
LABEL org.opencontainers.image.licenses="MIT"

# ── System dependencies ───────────────────────────────────────────────────────
# libglib2.0 and libnss3 are needed by playwright chromium (installed below).
# gcc is needed by some Python packages that compile C extensions.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libglib2.0-0 \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libxkbcommon0 \
        libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# requirements.txt is generated on Windows and includes Windows-only packages
# (uiautomation, comtypes) that will not build on Linux. They are for the
# sharing_on *capture* feature only — the Systemu daemon does not use them.
# pynput is a capture package too; it installs fine on Linux but is unused by
# the daemon, so it is also excluded to keep the image lean.
COPY requirements.txt .
RUN grep -vE "^(uiautomation|comtypes|pynput)" requirements.txt \
    | pip install --no-cache-dir -r /dev/stdin \
 && pip install --no-cache-dir redis>=4.0 psycopg2-binary>=2.9

# v0.6.8-d: install operator-approved tool deps baked at install time.
# The installer wizard writes tools/requirements-tools.txt by scanning
# `# deps:` comments in systemu/vault/tools/implementations/*.py and
# asking the operator to approve.  The daemon later seeds the
# tool_dep_approvals table from the same file on first boot.
COPY tools/requirements-tools.txt /tmp/requirements-tools.txt
RUN pip install --no-cache-dir -r /tmp/requirements-tools.txt

# Why install redis + psycopg2-binary above (outside requirements.txt):
#   The image must support BOTH docker-local (Postgres + Huey-SQLite) and
#   docker-enterprise (Postgres + Huey-Redis) modes — same image, different
#   env vars at runtime.  Without these two installed, the worker silently
#   degrades to file-backend and docker-enterprise loses its crash safety.
#   We install here rather than via `pip install -e .[docker-enterprise]`
#   because the `pip install -e .` below uses --no-deps for layer caching.

# ── Playwright browser binaries ───────────────────────────────────────────────
# Install chromium + its OS deps. Tools that use playwright will find the
# browser already present in the image.
RUN playwright install --with-deps chromium

# ── Application code ──────────────────────────────────────────────────────────
COPY . .

# Install the sharing_on package so the CLI entry point is on PATH.
# --no-deps: all deps are already installed from requirements.txt above.
RUN pip install --no-cache-dir --no-deps -e .

# systemu.* is not declared in pyproject.toml's package find list (it is a
# sibling package, not part of the sharing_on distribution). Setting
# PYTHONPATH ensures both sharing_on and systemu are importable.
ENV PYTHONPATH=/app

# ── Runtime data separation ───────────────────────────────────────────────────
# Bake the STARTER content (shipped tools, shadows, skills, starter index
# files) into a stable read-only path that NO volume ever mounts over.
# At runtime, the entrypoint seeds $SYSTEMU_VAULT_DIR from this path on first
# boot — so the named volume only ever contains data, never Python modules.
#
# Without this separation, a `vault_data:/app/systemu/vault` mount silently
# shadows factory.py / vault.py / *.py with whatever the volume captured on
# its first creation — breaking imports forever after an image upgrade.
RUN mkdir -p /app/starter-vault \
 && for d in scrolls activities shadow_army skills tools evolutions \
              notifications elder executions; do \
        if [ -d "/app/systemu/vault/$d" ]; then \
            cp -r "/app/systemu/vault/$d" "/app/starter-vault/$d"; \
        fi; \
    done \
 && for f in /app/systemu/vault/*.jsonl /app/systemu/vault/*.md; do \
        if [ -f "$f" ]; then cp "$f" /app/starter-vault/; fi; \
    done

# Default vault path is the data-only mount point.  Compose may override
# this — older deployments that still mount the volume at /app/systemu/vault
# keep working (the entrypoint just sees existing index.json files and
# skips seeding).
ENV SYSTEMU_VAULT_DIR=/data/vault
ENV SYSTEMU_STARTER_VAULT_DIR=/app/starter-vault

# Copy the seed-on-first-boot entrypoint and make it executable.
COPY docker/entrypoint.sh /usr/local/bin/systemu-entrypoint
RUN chmod +x /usr/local/bin/systemu-entrypoint
ENTRYPOINT ["/usr/local/bin/systemu-entrypoint"]

EXPOSE 8765

# Liveness check: probe the NiceGUI dashboard HTTP endpoint.
# 40 s start period covers image startup + recovery sweep + NiceGUI initialisation.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8765/', timeout=4)" 2>/dev/null || exit 1

# Run the daemon in foreground so Docker manages the process lifecycle.
# The startup recovery sweep fires 5 s after start, healing any pipeline states
# left incomplete by a prior container run.
CMD ["sharing_on", "daemon", "start", "--foreground"]
