#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

. scripts/ensure_runtime.sh
ensure_runtime

# ---------------------------------------------------------------------------
# 后台拉起 CPA 整备 + acc token 汇总
#   acpa_watchdog.py  → authenticated/ → keys/cpa_ready/  (整备 cli-chat-proxy 凭据)
#   sync_acc.py       → keys/cpa_ready/ → keys/acc.md     (只抽 access_token, 一行一个)
# 两者都常驻轮询。不用 exec: exec 会替换 shell, 后台子进程变孤儿且 trap 失效。
# 脚本退出(正常 / Ctrl-C / SIGTERM)时一并回收, 避免孤儿。
# ---------------------------------------------------------------------------
WATCHDOG="keys/acpa_watchdog.py"
SYNC_ACC="keys/sync_acc.py"
WATCHDOG_PID=""
SYNC_PID=""

cleanup() {
    for pid in "${SYNC_PID:-}" "${WATCHDOG_PID:-}"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT INT TERM

mkdir -p logs

if [ -f "$WATCHDOG" ]; then
    .venv/bin/python "$WATCHDOG" </dev/null >>logs/watchdog.log 2>&1 &
    WATCHDOG_PID=$!
    echo "[auth-service] watchdog 已拉起 (pid=$WATCHDOG_PID, 日志 logs/watchdog.log)"
else
    echo "[auth-service] 未找到 $WATCHDOG, 跳过自动整备" >&2
fi

if [ -f "$SYNC_ACC" ]; then
    .venv/bin/python "$SYNC_ACC" </dev/null >>logs/sync_acc.log 2>&1 &
    SYNC_PID=$!
    echo "[auth-service] sync_acc 已拉起 (pid=$SYNC_PID, 日志 logs/sync_acc.log → keys/acc.md)"
else
    echo "[auth-service] 未找到 $SYNC_ACC, 跳过 acc.md 汇总" >&2
fi

# ---------------------------------------------------------------------------
# 前台跑认证服务 (注册出货)。退出码透传; trap cleanup 在 exit 时兜底回收后台进程。
# ---------------------------------------------------------------------------
set +e
.venv/bin/python -m xai_enroller.service "$@"
RC=$?
set -e
exit "$RC"
