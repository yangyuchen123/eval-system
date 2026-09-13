# agent-pi:0.85.1 — pi agent runtime base image (version in tag).
# 任务 Dockerfile 第一行 FROM agent-pi:0.85.1 → Harbor 每次 trial 跳过
# apt/nvm/pi 下载（OpenRouterPi.install() 检测 `command -v pi` 短路）,
# setup 从分钟级降至秒级, 消除 deb.debian.org 502 与 NVM 下载失败两类故障源。
FROM python:3.12-slim

RUN apt-get update && apt-get install -y curl git \
    && rm -rf /var/lib/apt/lists/*

ENV NVM_DIR=/root/.nvm \
    NODE_VERSION=22
RUN curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.2/install.sh | bash \
    && bash -c ". $NVM_DIR/nvm.sh && nvm install $NODE_VERSION && nvm alias default $NODE_VERSION" \
    && bash -c ". $NVM_DIR/nvm.sh && npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.85.1 && pi --version"

# Harbor 运行时以 `. ~/.nvm/nvm.sh; pi ...` 前缀执行, 不依赖本 ENV PATH;
# 提供稳定 bin 链接便于手工排查。
RUN ln -sfn /root/.nvm/versions/node/v22.23.2 /root/.nvm/current
ENV PATH="/root/.nvm/current/bin:${PATH}"
