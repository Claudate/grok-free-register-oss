#!/usr/bin/env bash

ensure_runtime() {
    local lock_dir=".setup.lock"
    local acquired=0
    local attempt

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
    trap 'rmdir .setup.lock 2>/dev/null || true' EXIT INT TERM

    if [ ! -d .venv ]; then
        echo "[*] 首次运行，安装依赖..."
        if ! bash setup.sh; then
            rmdir "$lock_dir"
            trap - EXIT INT TERM
            return 1
        fi
    fi

    rmdir "$lock_dir"
    trap - EXIT INT TERM
}
