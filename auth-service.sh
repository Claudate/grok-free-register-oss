#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

. scripts/ensure_runtime.sh
ensure_runtime

# ---------------------------------------------------------------------------
# 后台拉起 CPA 整备 + acc token 汇总
#   acpa_watchdog.py
#     authenticated/  ── bulk 整备(秒级) ──► keys/cpa_ready/xai-*.json
#                     ── 并行探活        ──► keys/cpa_ready/_state.tsv
#   sync_acc.py
#     cpa_ready + _state(alive)  ──────────► keys/acc.md  (一行一个 access_token)
#
# 两者常驻、单实例 flock（watchdog）。
# **不** 在 auth 退出时杀后台：认证 q 掉后 backlog 还要继续整备/探活，
# 否则会出现「认证成功 103 / cpa_ready 只 22」这种半截。
# 要停整备：pkill -f keys/acpa_watchdog.py ; pkill -f keys/sync_acc.py
# 或 bash keys/async_auth.sh 单独管。
# ---------------------------------------------------------------------------
WATCHDOG="keys/acpa_watchdog.py"
SYNC_ACC="keys/sync_acc.py"

mkdir -p logs

# Detach background workers so auth-service exit / shell stop (Ctrl-Z) does not
# kill them. macOS has no setsid(1); use nohup + disown as portable fallback.
run_detached() {
    # usage: run_detached <log> <cmd> [args...]
    local log="$1"
    shift
    if command -v setsid >/dev/null 2>&1; then
        setsid "$@" </dev/null >>"$log" 2>&1 &
    else
        nohup "$@" </dev/null >>"$log" 2>&1 &
        disown "$!" 2>/dev/null || true
    fi
    echo "$!"
}

# 已在跑就别再起（watchdog 自己也有 flock，双开会立刻退）
if pgrep -f "[.]venv/bin/python ${WATCHDOG}|python(3)? ${WATCHDOG}" >/dev/null 2>&1; then
    echo "[auth-service] watchdog 已在跑，跳过拉起"
elif [ -f "$WATCHDOG" ]; then
    wd_pid="$(run_detached logs/watchdog.log .venv/bin/python "$WATCHDOG")"
    echo "[auth-service] watchdog 已拉起 (pid=${wd_pid}, 日志 logs/watchdog.log；auth 退出不杀)"
else
    echo "[auth-service] 未找到 $WATCHDOG, 跳过自动整备" >&2
fi

if pgrep -f "[.]venv/bin/python ${SYNC_ACC}|python(3)? ${SYNC_ACC}" >/dev/null 2>&1; then
    echo "[auth-service] sync_acc 已在跑，跳过拉起"
elif [ -f "$SYNC_ACC" ]; then
    sa_pid="$(run_detached logs/sync_acc.log .venv/bin/python "$SYNC_ACC")"
    echo "[auth-service] sync_acc 已拉起 (pid=${sa_pid}, 日志 logs/sync_acc.log → keys/acc.md；auth 退出不杀)"
else
    echo "[auth-service] 未找到 $SYNC_ACC, 跳过 acc.md 汇总" >&2
fi

# ---------------------------------------------------------------------------
# 前台跑认证服务。退出码透传。后台整备继续跑。
# 出口走项目 .env（REGISTER_PROXY / HTTP(S)_PROXY / mihomo 等），无内置代理池。
# ---------------------------------------------------------------------------
set +e
.venv/bin/python -m xai_enroller.service "$@"
RC=$?
set -e
exit "$RC"
