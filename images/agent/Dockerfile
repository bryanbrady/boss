# The one image every boss container uses: the agent, the checks and the egress gate. It
# also has a Docker engine, which runs only in nested-Docker runs (Sysbox; see sandbox.py).
#
#   docker build -t boss-agent images/agent
#
# Pin the agent CLIs with --build-arg CLAUDE_CODE_VERSION=... OPENCODE_VERSION=...

FROM ghcr.io/astral-sh/uv:0.12.22 AS uv

FROM node:22-bookworm-slim

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash ca-certificates curl git jq procps python3 python3-venv ripgrep \
  && rm -rf /var/lib/apt/lists/*

RUN install -m 0755 -d /etc/apt/keyrings \
  && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
  && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc]" \
    "https://download.docker.com/linux/debian bookworm stable" \
    > /etc/apt/sources.list.d/docker.list \
  && apt-get update \
  && apt-get install -y --no-install-recommends \
    containerd.io docker-buildx-plugin docker-ce docker-ce-cli docker-compose-plugin iptables \
  && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

ARG CLAUDE_CODE_VERSION=latest
ARG OPENCODE_VERSION=latest
RUN npm install -g --no-audit --no-fund \
    "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "opencode-ai@${OPENCODE_VERSION}" \
  && npm cache clean --force

COPY gate.py /usr/local/bin/boss-gate
COPY entrypoint.sh /usr/local/bin/boss-entrypoint
COPY dockerd.sh /usr/local/bin/boss-dockerd

# Everything that writes goes under /tmp, the only writable path besides /work.
ENV HOME=/tmp/home \
    XDG_CACHE_HOME=/tmp/home/.cache \
    XDG_CONFIG_HOME=/tmp/home/.config \
    XDG_DATA_HOME=/tmp/home/.local/share \
    XDG_STATE_HOME=/tmp/home/.local/state \
    UV_CACHE_DIR=/tmp/home/.cache/uv \
    npm_config_cache=/tmp/home/.npm \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    OPENCODE_DISABLE_AUTOUPDATE=1 \
    OPENCODE_DISABLE_MODELS_FETCH=1 \
    OPENCODE_DISABLE_SHARE=1

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/boss-entrypoint"]
CMD ["bash"]
