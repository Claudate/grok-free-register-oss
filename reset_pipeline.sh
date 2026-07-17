#!/usr/bin/env bash
# 一键清零注册 → 认证 → CPA 整条流水线的运行时数据，方便重开。
#
# 你只清 keys/acc.md / accounts.txt 不够：
#   acpa_watchdog 会从 ~/Downloads/grok-free-register-auth/authenticated/
#   把旧号再整备进 keys/cpa_ready/，sync_acc 再写回 acc.md。
#
# 用法（本文件在项目根）:
#   bash reset_pipeline.sh            # dry-run，只列将删内容
#   bash reset_pipeline.sh --yes      # 真正删除
#   bash reset_pipeline.sh --yes --keep-register
#       # 只清认证/CPA，保留 keys 里新注册的 SSO 源
#   bash reset_pipeline.sh --yes --keep-cpa
#       # 只清认证账本/源快照，保留 cpa_ready（一般不需要）
#
# 建议先停 auth-service / register，再跑本脚本。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
KEYS="$ROOT/keys"
AUTH_DIR="${XAI_AUTH_SERVICE_LOCAL_DIR:-$HOME/Downloads/grok-free-register-auth}"

YES=0
KEEP_REGISTER=0
KEEP_CPA=0
KEEP_AUTH_SALT=1

for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --keep-register) KEEP_REGISTER=1 ;;
    --keep-cpa) KEEP_CPA=1 ;;
    --wipe-salt) KEEP_AUTH_SALT=0 ;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 2
      ;;
  esac
done

# 运行时产物清单（脚本本身永不删）
REGISTER_FILES=(
  "$KEYS/accounts.txt"
  "$KEYS/grok.txt"
  "$KEYS/auth-sessions.jsonl"
  "$KEYS/acc.md"
)
CPA_GLOBS=(
  "$KEYS/cpa_ready/xai-"*.json
  "$KEYS/cpa_ready/_state.tsv"
)
AUTH_PATHS=(
  "$AUTH_DIR/authenticated"
  "$AUTH_DIR/claimed"
  "$AUTH_DIR/enrollment-ledger.db"
  "$AUTH_DIR/source-snapshot.jsonl"
)
AUTH_SALT="$AUTH_DIR/.ledger-salt"

count_matches() {
  local n=0
  local p
  for p in "$@"; do
    # shellcheck disable=SC2086
    if compgen -G "$p" > /dev/null 2>&1; then
      while IFS= read -r -d '' f; do
        n=$((n + 1))
      done < <(find $p -type f -print0 2>/dev/null || true)
      # also count plain files matched by glob/file
      if [[ -f "$p" ]]; then
        n=$((n + 1))
      fi
    elif [[ -f "$p" ]]; then
      n=$((n + 1))
    elif [[ -d "$p" ]]; then
      while IFS= read -r -d '' f; do
        n=$((n + 1))
      done < <(find "$p" -type f -print0 2>/dev/null || true)
    fi
  done
  echo "$n"
}

size_of() {
  local p="$1"
  if [[ -e "$p" ]]; then
    du -sh "$p" 2>/dev/null | awk '{print $1}'
  else
    echo "-"
  fi
}

echo "=== grok-free-register 流水线清零 ==="
echo "ROOT     = $ROOT"
echo "KEYS     = $KEYS"
echo "AUTH_DIR = $AUTH_DIR"
echo

# 检测可能还在跑的服务
if pgrep -af 'xai_enroller|acpa_watchdog|sync_acc|grok_register.register' 2>/dev/null | grep -v "reset_pipeline\|pgrep\|grep" >/dev/null; then
  echo "[!] 检测到相关进程仍在运行:"
  pgrep -af 'xai_enroller|acpa_watchdog|sync_acc|grok_register.register' 2>/dev/null | grep -v "reset_pipeline\|pgrep" || true
  echo "    建议先 Ctrl-C 停掉 auth-service / register，再 --yes"
  echo
fi

echo "-- 将处理的目标 --"
if [[ "$KEEP_REGISTER" -eq 0 ]]; then
  for f in "${REGISTER_FILES[@]}"; do
    if [[ -e "$f" ]]; then
      echo "  [register] $(size_of "$f")  $f"
    fi
  done
else
  echo "  [register] 跳过 (--keep-register)"
fi

if [[ "$KEEP_CPA" -eq 0 ]]; then
  cpa_n=$(find "$KEYS/cpa_ready" -name 'xai-*.json' -type f 2>/dev/null | wc -l | tr -d ' ')
  echo "  [cpa]      ${cpa_n} 个 xai-*.json  @ $KEYS/cpa_ready/"
  [[ -f "$KEYS/cpa_ready/_state.tsv" ]] && echo "  [cpa]      _state.tsv"
else
  echo "  [cpa]      跳过 (--keep-cpa)"
fi

auth_n=$(find "$AUTH_DIR/authenticated" -type f 2>/dev/null | wc -l | tr -d ' ')
echo "  [auth]     authenticated/  ${auth_n} 文件  ($(size_of "$AUTH_DIR/authenticated"))"
[[ -d "$AUTH_DIR/claimed" ]] && echo "  [auth]     claimed/        $(find "$AUTH_DIR/claimed" -type f 2>/dev/null | wc -l | tr -d ' ') 文件"
[[ -f "$AUTH_DIR/enrollment-ledger.db" ]] && echo "  [auth]     enrollment-ledger.db  $(size_of "$AUTH_DIR/enrollment-ledger.db")"
[[ -f "$AUTH_DIR/source-snapshot.jsonl" ]] && echo "  [auth]     source-snapshot.jsonl  $(size_of "$AUTH_DIR/source-snapshot.jsonl")  lines=$(wc -l < "$AUTH_DIR/source-snapshot.jsonl" 2>/dev/null | tr -d ' ')"
if [[ "$KEEP_AUTH_SALT" -eq 0 && -f "$AUTH_SALT" ]]; then
  echo "  [auth]     .ledger-salt (将删，指纹会重算)"
else
  echo "  [auth]     .ledger-salt 保留"
fi
echo

if [[ "$YES" -ne 1 ]]; then
  echo "dry-run 模式，未删除任何东西。"
  echo "确认后执行:  bash reset_pipeline.sh --yes"
  exit 0
fi

rm_file() {
  local f="$1"
  if [[ -f "$f" || -L "$f" ]]; then
    rm -f -- "$f"
    echo "  removed file  $f"
  fi
}

wipe_dir_contents() {
  local d="$1"
  if [[ -d "$d" ]]; then
    # 只清内容，保留目录本身
    find "$d" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +
    echo "  wiped dir    $d/"
  fi
}

echo "-- 执行删除 --"

if [[ "$KEEP_REGISTER" -eq 0 ]]; then
  for f in "${REGISTER_FILES[@]}"; do
    rm_file "$f"
  done
  # 重建空占位，避免下游 open 报错
  : > "$KEYS/accounts.txt"
  : > "$KEYS/grok.txt"
  : > "$KEYS/auth-sessions.jsonl"
  : > "$KEYS/acc.md"
  chmod 600 "$KEYS/acc.md" "$KEYS/auth-sessions.jsonl" 2>/dev/null || true
  echo "  recreated empty accounts.txt / grok.txt / auth-sessions.jsonl / acc.md"
fi

if [[ "$KEEP_CPA" -eq 0 ]]; then
  mkdir -p "$KEYS/cpa_ready"
  find "$KEYS/cpa_ready" -type f \( -name 'xai-*.json' -o -name '_state.tsv' \) -delete 2>/dev/null || true
  echo "  wiped        $KEYS/cpa_ready/xai-*.json + _state.tsv"
fi

# 认证侧：这是旧号回灌的真正源头
mkdir -p "$AUTH_DIR/authenticated" "$AUTH_DIR/claimed"
wipe_dir_contents "$AUTH_DIR/authenticated"
wipe_dir_contents "$AUTH_DIR/claimed"
rm_file "$AUTH_DIR/enrollment-ledger.db"
rm_file "$AUTH_DIR/source-snapshot.jsonl"
# 偶发的 sqlite 旁路文件
rm_file "$AUTH_DIR/enrollment-ledger.db-wal"
rm_file "$AUTH_DIR/enrollment-ledger.db-shm"
rm_file "$AUTH_DIR/enrollment-ledger.db-journal"

if [[ "$KEEP_AUTH_SALT" -eq 0 ]]; then
  rm_file "$AUTH_SALT"
fi

# 兜底：项目内若误放了同名目录也清
for extra in "$KEYS/authenticated" "$KEYS/claimed"; do
  if [[ -d "$extra" ]]; then
    wipe_dir_contents "$extra"
  fi
done

echo
echo "-- 清零后状态 --"
echo "  accounts.txt lines = $(wc -l < "$KEYS/accounts.txt" 2>/dev/null | tr -d ' ')"
echo "  auth-sessions      = $(wc -l < "$KEYS/auth-sessions.jsonl" 2>/dev/null | tr -d ' ')"
echo "  acc.md tokens      = $(wc -l < "$KEYS/acc.md" 2>/dev/null | tr -d ' ')"
echo "  cpa_ready json     = $(find "$KEYS/cpa_ready" -name 'xai-*.json' -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "  authenticated json = $(find "$AUTH_DIR/authenticated" -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "  ledger.db          = $([[ -f "$AUTH_DIR/enrollment-ledger.db" ]] && echo present || echo gone)"
echo "  snapshot           = $([[ -f "$AUTH_DIR/source-snapshot.jsonl" ]] && echo present || echo gone)"
echo
echo "完成。重开顺序建议:"
echo "  1) bash start.sh                 # 注册出 SSO → keys/auth-sessions.jsonl + accounts.txt"
echo "  2) bash auth-service.sh          # device-flow → AUTH_DIR/authenticated + acpa→cpa_ready + sync_acc"
echo "  若只想清 CPA/认证、保留刚注的 SSO:  下次加 --keep-register"
