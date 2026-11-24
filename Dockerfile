# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/

# PDF (WeasyPrint): pango + harfbuzz and a metric-compatible Arial (fonts-liberation2), ~40 MB.
# Claude Code CLI from Anthropic's signed apt repository (not curl|bash).
# The build fails unless the signing key has the expected fingerprint.
# Pinned (latest in the apt repo when #4 was built, the version QA ran in #1). Bumping it means
# re-running the `classify` fixtures (docs/llm.md): the JSON envelope may change between versions.
ARG CLAUDE_CODE_VERSION=2.1.311-1
ARG CLAUDE_KEY_FPR=31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg tzdata \
      libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz-subset0 fonts-liberation2 \
 && install -d -m 0755 /etc/apt/keyrings \
 && curl -fsSL https://downloads.claude.ai/keys/claude-code.asc -o /etc/apt/keyrings/claude-code.asc \
 && gpg --show-keys --with-colons /etc/apt/keyrings/claude-code.asc > /tmp/key.txt \
 && [ "$(grep -c '^pub:' /tmp/key.txt)" = 1 ] \
 && [ "$(awk -F: '$1=="fpr"{print $10; exit}' /tmp/key.txt)" = "$CLAUDE_KEY_FPR" ] \
 && rm /tmp/key.txt \
 && echo "deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main" \
      > /etc/apt/sources.list.d/claude-code.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends "claude-code${CLAUDE_CODE_VERSION:+=$CLAUDE_CODE_VERSION}" \
 && apt-get purge -y --auto-remove gnupg \
 && rm -rf /var/lib/apt/lists/*

ENV PATH=/app/.venv/bin:$PATH \
    CVBOOST_DB=/data/cvboost.sqlite \
    CVBOOST_PROFILE=/app/config/profile.yaml \
    CVBOOST_RULES=/app/config/rules.yaml \
    CVBOOST_LLM_CONFIG=/app/config/llm.yaml \
    CVBOOST_WORKER_CONFIG=/app/config/worker.yaml \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    DISABLE_AUTOUPDATER=1 \
    TZ=UTC \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

RUN useradd --uid 1000 --create-home --shell /bin/bash app \
 && mkdir /data && chown app:app /data
USER app
VOLUME /data

CMD ["cvboost", "sources"]
