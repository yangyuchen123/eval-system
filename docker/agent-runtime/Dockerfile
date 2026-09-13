# Harbor agent runtime image: curl + nvm/node22 + pi preinstalled.
# Prebuilding removes the per-trial apt-get/nvm/pi downloads (deb.debian.org
# flakiness caused the AgentSetupTimeoutError / apt 502 failures) — install()
# is skipped once pi is present (OpenRouterPi checks `command -v pi`).
# Harbor uses this via task.toml [environment] docker_image.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl git \
    && rm -rf /var/lib/apt/lists/*

ENV NVM_DIR=/root/.nvm \
    NODE_VERSION=22
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash \
    && bash -c ". $NVM_DIR/nvm.sh && nvm install $NODE_VERSION && nvm alias default $NODE_VERSION" \
    && bash -c ". $NVM_DIR/nvm.sh && npm install -g --ignore-scripts @earendil-works/pi-coding-agent@latest && pi --version"

# nvm is only wired into interactive shells; expose it for non-login shells.
ENV PATH="/root/.nvm/versions/node/v${NODE_VERSION}/bin:${PATH}"
