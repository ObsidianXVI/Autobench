# Autobench on Cloud Run (Vral Altos): the FastAPI server + web UI + MCP, with IB Gateway (started on demand,
# services/broker/gateway.py), IBC, Xvfb and the Claude Code CLI as container dependencies. Built by
# deploy/deploy.sh from a staged context:
#   autobench/   this repo (server/, app/build/, CLAUDE.md, .claude/)    financier/   Financier's web build
# No state in the image: it is in Firestore (state_store.py) and Cloud Storage (universe snapshots, Claude home).

# --- Claude Code (pinned like the Vral Altos cloud runner) --------------------------------------------------
FROM node:22-bookworm-slim AS claude
ARG CLAUDE_CODE_VERSION=2.1.318
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force

# --- IB Gateway (standalone installer, bundled JRE) and IBC ----------------------------------------------------
FROM debian:bookworm-slim AS ibgateway
ARG IBC_VERSION=3.24.2
ARG IBGATEWAY_CHANNEL=stable
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl unzip \
 && rm -rf /var/lib/apt/lists/*
RUN curl -fsSL -o /tmp/ibgw.sh \
      "https://download2.interactivebrokers.com/installers/ibgateway/${IBGATEWAY_CHANNEL}-standalone/ibgateway-${IBGATEWAY_CHANNEL}-standalone-linux-x64.sh" \
 && chmod +x /tmp/ibgw.sh && /tmp/ibgw.sh -q -dir /opt/Jts/ibgateway/stable && rm /tmp/ibgw.sh \
 && test -d /opt/Jts/ibgateway/stable/jars
RUN curl -fsSL -o /tmp/ibc.zip "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip" \
 && mkdir -p /opt/ibc && unzip -q /tmp/ibc.zip -d /opt/ibc && rm /tmp/ibc.zip \
 && chmod +x /opt/ibc/*.sh /opt/ibc/scripts/*.sh

# --- Autobench ----------------------------------------------------------------------------------------------
FROM python:3.14-slim-bookworm
# Xvfb + the X libraries IB Gateway's Swing UI needs; procps for IBC's process handling; curl for Nasdaq (reference.py);
# imagemagick for the Gateway screen view (/api/trading/gateway/screen); xdotool types the owner's 2FA code
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb libxtst6 libxrender1 libxi6 libxext6 libfreetype6 fontconfig procps ca-certificates curl git imagemagick xdotool \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --create-home --uid 10001 autobench

COPY --from=ibgateway /opt/Jts /opt/Jts
COPY --from=ibgateway /opt/ibc /opt/ibc
COPY --from=claude /usr/local/bin/node /usr/local/bin/node
COPY --from=claude /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN BIN="$(node -p "const b=require('/usr/local/lib/node_modules/@anthropic-ai/claude-code/package.json').bin; typeof b==='string'?b:b.claude")" \
 && ln -s "/usr/local/lib/node_modules/@anthropic-ai/claude-code/${BIN}" /usr/local/bin/claude \
 && claude --version

COPY autobench /app/autobench
COPY financier /app/financier
RUN pip install --no-cache-dir -e /app/autobench/server \
 && chown -R autobench /app/autobench /opt/Jts

USER autobench
WORKDIR /app/autobench
ENV HOME=/home/autobench \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    AUTOBENCH_HOST=0.0.0.0 \
    AUTOBENCH_DB_PATH=/tmp/autobench/autobench.db \
    AUTOBENCH_FINANCIER_DIR=/app/financier \
    AUTOBENCH_WORKSPACE=/app/autobench \
    AUTOBENCH_GATEWAY=managed \
    AUTOBENCH_BROKER=ibkr \
    IBKR_HOST=127.0.0.1 \
    IBKR_PORT=4002 \
    AUTOBENCH_CLAUDE_BIN=/usr/local/bin/claude
# Cloud Run sets PORT
CMD ["sh", "-c", "exec autobench serve --host 0.0.0.0 --port ${PORT:-8080}"]
