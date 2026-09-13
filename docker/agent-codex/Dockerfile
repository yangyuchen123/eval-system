# agent-codex:0.154.0 — codex agent runtime base image (npm latest, parity with pi).
# npm @openai/codex@0.154.0 (2026-09-09 latest). 阿里云 debian 源加速 apt.
FROM python:3.12-slim

RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    && apt-get update && apt-get install -y curl git ripgrep \
    && rm -rf /var/lib/apt/lists/*

ENV NVM_DIR=/root/.nvm \
    NODE_VERSION=22
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash \
    && bash -c ". $NVM_DIR/nvm.sh && nvm install $NODE_VERSION && nvm alias default $NODE_VERSION" \
    && bash -c ". $NVM_DIR/nvm.sh && npm install -g --ignore-scripts @openai/codex@0.154.0 && codex --version"

RUN ln -sfn /root/.nvm/versions/node/v22.23.2 /root/.nvm/current
ENV PATH="/root/.nvm/current/bin:${PATH}"
