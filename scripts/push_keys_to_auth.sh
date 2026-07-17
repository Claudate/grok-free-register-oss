#!/usr/bin/env bash
# 本机注册 → 推 SSO 源到远端 auth 机（local 模式吃 keys/）
#
# 动态公网 IP 不适合「远端 SSH 拉本机」；改由本机主动推：
#   keys/auth-sessions.jsonl
#   keys/accounts.txt
# → AUTH_HOST:REMOTE_ROOT/keys/
#
# 用法（本机项目根或任意 cwd）:
#   bash scripts/push_keys_to_auth.sh              # 推一次
#   bash scripts/push_keys_to_auth.sh --watch      # 常驻，文件变了再推
#   bash scripts/push_keys_to_auth.sh --interval 15
#
# 环境变量（可写本机 .env，本脚本会 source）:
#   AUTH_SSH_HOST=user@auth-server.example
#   AUTH_REMOTE_ROOT=/opt/grok-free-register
#   AUTH_SSH_IDENTITY=           # 可选 -i 路径
#   AUTH_PUSH_INTERVAL=20        # --watch 轮询秒
#
# 无内置默认主机/路径：必须显式设置 AUTH_SSH_HOST 与 AUTH_REMOTE_ROOT。

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 调用方已 export 的 AUTH_* 优先（.env 不得盖掉），方便并行推多台 auth
_PRE_AUTH_SSH_HOST="${AUTH_SSH_HOST-}"
_PRE_AUTH_REMOTE_ROOT="${AUTH_REMOTE_ROOT-}"
_PRE_AUTH_SSH_IDENTITY="${AUTH_SSH_IDENTITY-}"
_PRE_AUTH_PUSH_INTERVAL="${AUTH_PUSH_INTERVAL-}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

AUTH_SSH_HOST="${_PRE_AUTH_SSH_HOST:-${AUTH_SSH_HOST:-}}"
AUTH_REMOTE_ROOT="${_PRE_AUTH_REMOTE_ROOT:-${AUTH_REMOTE_ROOT:-}}"
AUTH_SSH_IDENTITY="${_PRE_AUTH_SSH_IDENTITY:-${AUTH_SSH_IDENTITY:-}}"
AUTH_PUSH_INTERVAL="${_PRE_AUTH_PUSH_INTERVAL:-${AUTH_PUSH_INTERVAL:-20}}"

if [[ -z "$AUTH_SSH_HOST" || -z "$AUTH_REMOTE_ROOT" ]]; then
  echo "push_keys_to_auth: set AUTH_SSH_HOST and AUTH_REMOTE_ROOT (env or .env)" >&2
  echo "  e.g. AUTH_SSH_HOST=user@auth-server.example AUTH_REMOTE_ROOT=/opt/grok-free-register" >&2
  exit 2
fi

WATCH=0
INTERVAL="$AUTH_PUSH_INTERVAL"
for a in "$@"; do
  case "$a" in
    --watch) WATCH=1 ;;
    --interval)
      # next arg handled below if present as --interval=N form preferred
      ;;
    --interval=*) INTERVAL="${a#--interval=}" ;;
    -h|--help)
      sed -n '2,22p' "$0"
      exit 0
      ;;
  esac
done
# support: --interval 15
prev=""
for a in "$@"; do
  if [[ "$prev" == "--interval" ]]; then
    INTERVAL="$a"
  fi
  prev="$a"
done

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15)
if [[ -n "$AUTH_SSH_IDENTITY" ]]; then
  SSH_OPTS+=(-i "$AUTH_SSH_IDENTITY")
fi

KEYS_SRC="$ROOT/keys"
REMOTE_KEYS="${AUTH_REMOTE_ROOT}/keys"
# 只推 auth 源，不推 cpa_ready/acc（远端自己整备）
FILES=(auth-sessions.jsonl accounts.txt)

log() { printf '[push_keys %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

fingerprint() {
  local f
  for f in "${FILES[@]}"; do
    if [[ -f "$KEYS_SRC/$f" ]]; then
      # size + mtime + sha256 head — cheap change detect
      stat -c '%s %Y' "$KEYS_SRC/$f" 2>/dev/null || stat -f '%z %m' "$KEYS_SRC/$f"
      sha256sum "$KEYS_SRC/$f" 2>/dev/null | awk '{print $1}'
    else
      echo "missing:$f"
    fi
  done
}

push_once() {
  local missing=0 f
  for f in "${FILES[@]}"; do
    if [[ ! -f "$KEYS_SRC/$f" ]]; then
      log "⚠ 缺 $KEYS_SRC/$f"
      missing=1
    fi
  done
  if [[ "$missing" -eq 1 ]]; then
    return 1
  fi

  # 远端原子替换：先推 .push.tmp 再 mv
  ssh "${SSH_OPTS[@]}" "$AUTH_SSH_HOST" "mkdir -p $(printf %q "$REMOTE_KEYS") && chmod 700 $(printf %q "$REMOTE_KEYS") 2>/dev/null || true"

  local remote_tmp remote_final
  for f in "${FILES[@]}"; do
    remote_tmp="${REMOTE_KEYS}/.${f}.push.tmp"
    remote_final="${REMOTE_KEYS}/${f}"
    # rsync over ssh when available; fallback scp
    if command -v rsync >/dev/null 2>&1; then
      rsync -az -e "ssh ${SSH_OPTS[*]}" \
        "$KEYS_SRC/$f" \
        "${AUTH_SSH_HOST}:${remote_tmp}"
    else
      scp "${SSH_OPTS[@]}" "$KEYS_SRC/$f" "${AUTH_SSH_HOST}:${remote_tmp}"
    fi
    ssh "${SSH_OPTS[@]}" "$AUTH_SSH_HOST" \
      "chmod 600 $(printf %q "$remote_tmp") && mv -f $(printf %q "$remote_tmp") $(printf %q "$remote_final")"
  done

  local n_sess n_acc
  n_sess=$(wc -l < "$KEYS_SRC/auth-sessions.jsonl" | tr -d ' ')
  n_acc=$(wc -l < "$KEYS_SRC/accounts.txt" | tr -d ' ')
  log "→ ${AUTH_SSH_HOST}:${REMOTE_KEYS}/  sessions=${n_sess} accounts=${n_acc}"
}

LAST_FP=""
if [[ "$WATCH" -eq 0 ]]; then
  push_once
  exit $?
fi

log "watch host=${AUTH_SSH_HOST} root=${AUTH_REMOTE_ROOT} interval=${INTERVAL}s"
while true; do
  FP="$(fingerprint)"
  if [[ "$FP" != "$LAST_FP" ]]; then
    if push_once; then
      LAST_FP="$FP"
    else
      log "推送失败，${INTERVAL}s 后重试"
    fi
  fi
  sleep "$INTERVAL"
done
