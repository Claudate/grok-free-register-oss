#!/usr/bin/env bash

ensure_runtime() {
    local lock_dir=".setup.lock"
    local acquired=0
    local attempt
    local need_setup=0

    for attempt in {1..300}; do
        if mkdir "$lock_dir" 2>/dev/null; then
            acquired=1
            break
        fi
        sleep 0.2
    done
    if [ "$acquired" -ne 1 ]; then
        echo "[!] 另一个安装进程长时间未结束，请稍后重试。" >&2
        return 1
    fi
    # 仅清本函数的 lock；调用方（auth-service cleanup 等）的 EXIT trap 之后会重新挂上
    trap 'rmdir .setup.lock 2>/dev/null || true' EXIT INT TERM

    if [ ! -d .venv ] || [ ! -x .venv/bin/python ]; then
        need_setup=1
    elif ! .venv/bin/python -c "import cloakbrowser, playwright, dotenv, httpx, curl_cffi" >/dev/null 2>&1; then
        echo "[*] .venv 存在但核心依赖不完整，重新安装..."
        need_setup=1
    fi

    if [ "$need_setup" -eq 1 ]; then
        echo "[*] 首次运行 / 修复环境，安装依赖..."
        if ! bash setup.sh; then
            rmdir "$lock_dir" 2>/dev/null || true
            trap - EXIT INT TERM
            return 1
        fi
        if ! .venv/bin/python -c "import cloakbrowser, playwright, dotenv, httpx, curl_cffi" >/dev/null 2>&1; then
            echo "[!] 安装后仍无法 import 核心依赖，请检查 setup.sh / 网络。" >&2
            rmdir "$lock_dir" 2>/dev/null || true
            trap - EXIT INT TERM
            return 1
        fi
    fi

    rmdir "$lock_dir" 2>/dev/null || true
    trap - EXIT INT TERM
}
