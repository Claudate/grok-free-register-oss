#!/usr/bin/env bash
# 异步 auth 后处理：acpa_watchdog（整备+探活）+ sync_acc（access_token → acc.md）
# 前置：bash auth-service.sh 已在出货 authenticated/ 写 xai-*.json
#
# 用法（项目根）:
#   bash keys/async_auth.sh            # 前台双进程，Ctrl-C 一起退
#   bash keys/async_auth.sh --once     # 各扫一轮就退
#   bash keys/async_auth.sh --watchdog-only
#   bash keys/async_auth.sh --sync-only

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"

ONCE=0
MODE=both
for a in "$@"; do
  case "$a" in
    --once) ONCE=1 ;;
    --watchdog-only) MODE=watchdog ;;
    --sync-only) MODE=sync ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
  esac
done

args=()
[[ "$ONCE" -eq 1 ]] && args+=(--once)

pids=()
cleanup() {
  for p in "${pids[@]:-}"; do
    kill "$p" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

run_wd()  { exec "$PY" "$ROOT/keys/acpa_watchdog.py" "${args[@]:-}"; }
run_sync(){ exec "$PY" "$ROOT/keys/sync_acc.py" "${args[@]:-}"; }

case "$MODE" in
  watchdog) run_wd ;;
  sync)     run_sync ;;
  both)
    if [[ "$ONCE" -eq 1 ]]; then
      "$PY" "$ROOT/keys/acpa_watchdog.py" --once
      "$PY" "$ROOT/keys/sync_acc.py" --once
      trap - INT TERM EXIT
      exit 0
    fi
    "$PY" "$ROOT/keys/acpa_watchdog.py" &
    pids+=($!)
    "$PY" "$ROOT/keys/sync_acc.py" &
    pids+=($!)
    wait
    ;;
esac
