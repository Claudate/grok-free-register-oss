#!/usr/bin/env bash
# Import keys/cpa_ready (or authenticated after watchdog) into CLIProxyAPI auths/ for hot-reload.
# Prerequisite: OAuth xai-*.json already prepared (SSO alone is NOT enough).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CPA_ROOT="$(cd "$ROOT/../CLIProxyAPI" 2>/dev/null && pwd || true)"
if [[ -z "${CPA_ROOT}" || ! -d "$CPA_ROOT" ]]; then
  CPA_ROOT="${CPA_ROOT_OVERRIDE:-}"
fi
if [[ -z "${CPA_ROOT}" || ! -d "$CPA_ROOT" ]]; then
  echo "[!] set CPA_ROOT_OVERRIDE to CLIProxyAPI path" >&2
  exit 1
fi

SRC="${1:-$ROOT/keys/cpa_ready}"
if [[ ! -d "$SRC" ]]; then
  echo "[!] missing source dir: $SRC" >&2
  echo "    SSO-only keys/ cannot import. Run: bash auth-service.sh  then  python3 keys/acpa_watchdog.py --once" >&2
  exit 1
fi

count=$(find "$SRC" -maxdepth 1 -type f -name 'xai-*.json' | wc -l | tr -d ' ')
echo "[*] source=$SRC xai_json=$count"
if [[ "$count" -eq 0 ]]; then
  echo "[!] no xai-*.json to import" >&2
  exit 1
fi

cd "$CPA_ROOT"
./scripts/cpa import "$SRC" --scope recursive --allow-expired --dry-run
./scripts/cpa import "$SRC" --scope recursive --allow-expired
echo "[*] root pool xai count: $(find auths -maxdepth 1 -type f -name 'xai-*.json' | wc -l | tr -d ' ')"
./scripts/cpa status || true
