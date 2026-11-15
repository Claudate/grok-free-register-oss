#!/bin/bash
# Grok Free Register — 一键安装脚本
# 用法: bash setup.sh

set -e

echo "=== Grok Free Register 安装 ==="

# 检测系统
if [ -f /etc/debian_version ]; then
    echo "[1/4] 安装系统依赖 (Debian/Ubuntu)..."
    sudo apt update -qq
    sudo apt install -y -qq \
        python3 python3-pip python3-venv \
        libatk1.0-0t64 libatk-bridge2.0-0t64 libcups2t64 \
        libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
        libxfixes3 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2t64 libnspr4 libnss3 libxshmfence1 \
        2>/dev/null || true
    # 兼容旧版 Ubuntu
    sudo apt install -y -qq libatk1.0-0 libatk-bridge2.0-0 libcups2 libasound2 2>/dev/null || true
elif [ -f /etc/redhat-release ]; then
    echo "[1/4] 安装系统依赖 (RHEL/CentOS)..."
    # libxkbcommon 是 CloakBrowser/Chromium 必需；旧版 setup 漏了会导致
    # error while loading shared libraries: libxkbcommon.so.0
    sudo yum install -y -q \
        python3 python3-pip \
        atk at-spi2-atk cups-libs libdrm libxkbcommon \
        libXcomposite libXdamage libXfixes libXrandr libX11 libXext libXcursor \
        mesa-libgbm pango cairo alsa-lib nspr nss libxshmfence \
        libxcb libXrender gtk3 \
        2>/dev/null || true
    sudo yum install -y -q libxkbcommon-x11 2>/dev/null || true
else
    echo "[1/4] 未知系统，跳过系统依赖（如 Chrome 启动失败请手动安装）"
fi

# Python 版本门禁：类型注解 / asyncio 写法要求 3.10+
PY_VER=$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "${PY_MAJOR:-0}" -lt 3 ] || { [ "${PY_MAJOR:-0}" -eq 3 ] && [ "${PY_MINOR:-0}" -lt 10 ]; }; then
    echo "❌ 需要 Python >= 3.10，当前 python3=$PY_VER"
    echo "   RHEL/CentOS 示例: dnf install -y python3.11 python3.11-pip && python3.11 -m venv .venv"
    exit 1
fi
echo "    python3=$PY_VER OK"

# Python 虚拟环境
echo "[2/4] 创建 Python 环境..."
PY=python3
command -v python3.12 >/dev/null 2>&1 && PY=python3.12
command -v python3.11 >/dev/null 2>&1 && PY=python3.11
$PY -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# 本项目直接由 Playwright 启动二进制，因此显式准备 CloakBrowser Chromium。
echo "[3/4] 下载 CloakBrowser Chromium..."
.venv/bin/python -m cloakbrowser install

# 创建输出目录
mkdir -p keys

echo "[4/4] 安装完成！"
echo ""
echo "运行注册服务: bash start.sh"
echo "运行自建邮箱服务: bash start.sh --email-service"
echo "运行认证服务: bash auth-service.sh"
