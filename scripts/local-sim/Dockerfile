# ARC-Bench 生产 Runner 基础镜像(本地重建版)。
# 真实源码在 arc-bench 网站仓库(私有,本机无 checkout);本 Dockerfile 按
# hackathon-local-simulation/README.md §4 环境表逐项复刻:
#   基础镜像 playwright/python v1.57.0-noble、Node 20.19.3、
#   JS @playwright/test 1.57.0、git/lsof/build-essential/pkg-config/libssl-dev/curl、
#   常用 Python 包(OpenAI/Anthropic/Pydantic/PyYAML/HTTPX/Requests/Tenacity/Rich/
#   JSON Schema/Jinja2),外加 run_submission.py 重建版与 smoke_test.py。
# 正式评测是 x86_64:构建时用 --platform linux/amd64(见 build.sh)。
ARG BASE_IMAGE=mcr.microsoft.com/playwright/python:v1.57.0-noble
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PATH=/usr/local/node/bin:/opt/arcbench/bin:$PATH

# 常用命令与编译工具(环境表);apt 走国内镜像,失败自动回退默认源。
RUN set -eux; \
    sed -i 's|http://archive.ubuntu.com|https://mirrors.aliyun.com|g; s|http://security.ubuntu.com|https://mirrors.aliyun.com|g; s|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' /etc/apt/sources.list 2>/dev/null || true; \
    find /etc/apt/sources.list.d -name '*.sources' -exec sed -i 's|http://archive.ubuntu.com|https://mirrors.aliyun.com|g; s|http://security.ubuntu.com|https://mirrors.aliyun.com|g; s|http://archive.ubuntu.com/ubuntu|https://mirrors.aliyun.com/ubuntu|g' {} + 2>/dev/null || true; \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential pkg-config libssl-dev git lsof curl xz-utils ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node 20.19.3(平台同版本;按架构取包,构建机 amd64 时与正式评测一致)。
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) narch=x64 ;; \
      arm64) narch=arm64 ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 3 --connect-timeout 30 \
      "https://registry.npmmirror.com/-/binary/node/v20.19.3/node-v20.19.3-linux-${narch}.tar.xz" \
      -o /tmp/node.tar.xz; \
    mkdir -p /usr/local/node; \
    tar -xJf /tmp/node.tar.xz -C /usr/local/node --strip-components=1; \
    rm /tmp/node.tar.xz; \
    node -v; npm -v

# 环境表列出的常用 Python 包(pypi 走 aliyun 镜像,清华源容器内不可达);
# 上游 python 基础镜像只带浏览器不带 playwright 包,按环境表补齐 1.57.0。
RUN python3 -m pip install --break-system-packages --no-cache-dir \
        -i https://mirrors.aliyun.com/pypi/simple/ \
        playwright==1.57.0 \
        openai anthropic pydantic pyyaml httpx requests tenacity rich jsonschema jinja2

# JS Playwright 全局安装(容器内离线可用);tests 目录由 runner npm link 接上。
RUN npm install -g @playwright/test@1.57.0 --registry=https://registry.npmmirror.com \
    && playwright --version

# 绑定挂载进来的 workspace 属主是宿主 uid:git 拒绝"dubious ownership"。
RUN git config --system --add safe.directory '*' \
    && git config --system user.name "ARC Runner" \
    && git config --system user.email "arc-runner@local.invalid"

COPY run_submission.py smoke_test.py /opt/arcbench/

WORKDIR /workspace
CMD ["python3", "/opt/arcbench/run_submission.py"]
