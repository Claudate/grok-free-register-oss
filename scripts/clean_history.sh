#!/usr/bin/env bash
# 快速清历史归档（不碰现网号池）。
#
# 只删这些「历史」：
#   keys/cpa_ready/cpa_ready_*.zip   导出包
#   keys/cpa_ready/_discarded/**     已丢弃号
#   keys/*.bak* / keys/*~ / keys/__pycache__
#   keys/async_auth.log / logs/*
#   keys/212.zip 等杂包
#
# 默认保留：
#   keys/cpa_ready/xai-*.json
#   keys/cpa_ready/_state.tsv
#   keys/acc.md / accounts.txt / auth-sessions.jsonl
#   AUTH_DIR/authenticated 现网出货
#
# 用法（项目根）:
#   bash scripts/clean_history.sh              # dry-run
#   bash scripts/clean_history.sh --yes        # 真删
#   bash scripts/clean_history.sh --yes --deep # 再清 source-snapshot / 旧 .bak 脚本
#
# 全量清零号池请用: bash reset_pipeline.sh --yes

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KEYS="$ROOT/keys"
CPA="$KEYS/cpa_ready"
AUTH_DIR="${XAI_AUTH_SERVICE_LOCAL_DIR:-${XAI_ENROLLER_LOCAL_AUTH_DIR:-$HOME/Downloads/grok-free-register-auth}}"
LOGS="$ROOT/logs"

YES=0
DEEP=0

for arg in "$@"; do
  case "$arg" in
    --yes|-y) YES=1 ;;
    --deep) DEEP=1 ;;
    -h|--help)
      sed -n '2,24p' "$0"
      exit 0
      ;;
    *)
      echo "未知参数: $arg" >&2
      exit 2
      ;;
  esac
done

size_of() {
  local p="$1"
  if [[ -e "$p" ]]; then
    du -sh "$p" 2>/dev/null | awk '{print $1}'
  else
    echo "-"
  fi
}

list_targets() {
  # zips / archives under cpa_ready and keys
  find "$CPA" -maxdepth 1 -type f \( -name 'cpa_ready_*.zip' -o -name '*.zip' -o -name '*.tar' -o -name '*.tar.gz' -o -name '*.tgz' \) 2>/dev/null || true
  find "$KEYS" -maxdepth 1 -type f \( -name '*.zip' -o -name '*.tar' -o -name '*.tar.gz' -o -name '*.tgz' \) 2>/dev/null || true
  # discarded
  if [[ -d "$CPA/_discarded" ]]; then
    find "$CPA/_discarded" -mindepth 1 2>/dev/null || true
  fi
  # logs / pyc / bak
  find "$KEYS" -maxdepth 1 -type f \( -name '*.log' -o -name '*.bak' -o -name '*.bak.*' -o -name '*~' \) 2>/dev/null || true
  find "$KEYS" -maxdepth 1 -type d -name '__pycache__' 2>/dev/null || true
  if [[ -d "$LOGS" ]]; then
    find "$LOGS" -type f 2>/dev/null || true
  fi
  if [[ "$DEEP" -eq 1 ]]; then
    # deep: old snapshot growth + script backups; still keep live pool
    [[ -f "$AUTH_DIR/source-snapshot.jsonl" ]] && echo "$AUTH_DIR/source-snapshot.jsonl"
    find "$KEYS" -maxdepth 1 -type f -name 'acpa_watchdog.py.bak*' 2>/dev/null || true
  fi
}

mapfile -t TARGETS < <(list_targets | awk 'NF' | sort -u)

echo "=== grok-free-register 快速清历史 ==="
echo "ROOT     = $ROOT"
echo "KEYS     = $KEYS"
echo "AUTH_DIR = $AUTH_DIR"
echo "MODE     = $([[ $YES -eq 1 ]] && echo APPLY || echo dry-run)$([[ $DEEP -eq 1 ]] && echo ' +deep' || true)"
echo

if [[ "${#TARGETS[@]}" -eq 0 ]]; then
  echo "没有可清的历史文件。"
  exit 0
fi

echo "-- 将处理 --"
total=0
for f in "${TARGETS[@]}"; do
  if [[ -e "$f" ]]; then
    echo "  $(size_of "$f")  $f"
    total=$((total + 1))
  fi
done
echo "  共 $total 项"
echo

# live pool summary (never touched)
echo "-- 现网号池（保留）--"
echo "  cpa xai-*.json = $(find "$CPA" -maxdepth 1 -name 'xai-*.json' -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "  acc.md lines   = $(wc -l < "$KEYS/acc.md" 2>/dev/null | tr -d ' ' || echo 0)"
echo "  sessions       = $(wc -l < "$KEYS/auth-sessions.jsonl" 2>/dev/null | tr -d ' ' || echo 0)"
echo "  authenticated  = $(find "$AUTH_DIR/authenticated" -type f 2>/dev/null | wc -l | tr -d ' ' || echo 0)"
echo

if [[ "$YES" -ne 1 ]]; then
  echo "dry-run。确认后:  bash scripts/clean_history.sh --yes"
  echo "更深一层(含 snapshot):  bash scripts/clean_history.sh --yes --deep"
  exit 0
fi

echo "-- 执行删除 --"
for f in "${TARGETS[@]}"; do
  if [[ -d "$f" ]]; then
    rm -rf -- "$f"
    echo "  removed dir   $f"
  elif [[ -e "$f" ]]; then
    rm -f -- "$f"
    echo "  removed file  $f"
  fi
done

# keep empty discarded dir
mkdir -p "$CPA/_discarded" 2>/dev/null || true
mkdir -p "$LOGS" 2>/dev/null || true

echo
echo "-- 清后 --"
echo "  cpa zip left   = $(find "$CPA" -maxdepth 1 -name '*.zip' -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "  discarded files= $(find "$CPA/_discarded" -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "  cpa xai-*.json = $(find "$CPA" -maxdepth 1 -name 'xai-*.json' -type f 2>/dev/null | wc -l | tr -d ' ')"
echo "  keys size      = $(size_of "$KEYS")"
echo "完成。"
