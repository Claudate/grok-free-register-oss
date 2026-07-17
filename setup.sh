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
    sudo yum install -y -q \
        python3 python3-pip \
        atk cups-libs libdrm libXcomposite libXdamage libXfixes libXrandr \
        mesa-libgbm pango cairo alsa-lib nspr nss libxshmfence \
        2>/dev/null || true
else
    echo "[1/4] 未知系统，跳过系统依赖（如 Chrome 启动失败请手动安装）"
fi

# Python 虚拟环境
echo "[2/4] 创建 Python 环境..."
python3 -m venv .venv
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
