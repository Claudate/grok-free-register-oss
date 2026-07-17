#!/usr/bin/env python3
"""Probe pure-HTTP signup legs against accounts.x.ai.

Usage (from repo root, with .venv):

  CLEARANCE_ENABLED=1 FLARESOLVERR_URL=http://127.0.0.1:8191 \\
    .venv/bin/python scripts/probe_http_protocol.py

  # optional:
  #   --email you@domain.com
  #   --castle TOKEN     # field3 castleRequestToken
  #   --no-prewarm       # use cache only
  #   --proxy http://127.0.0.1:40080
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe x.ai HTTP signup legs")
    parser.add_argument("--email", default="http-probe@example.com")
    parser.add_argument("--castle", default="", help="Castle request token (field3)")
    parser.add_argument("--no-prewarm", action="store_true")
    parser.add_argument("--proxy", default="", help="Override REGISTER_PROXY for this process")
    parser.add_argument("--json", action="store_true", help="Print full JSON only")
    args = parser.parse_args()

    if args.proxy:
        os.environ["REGISTER_PROXY"] = args.proxy

    # Defaults for a local clearance stack if operator forgot .env
    os.environ.setdefault("CLEARANCE_ENABLED", "1")
    os.environ.setdefault("FLARESOLVERR_URL", "http://127.0.0.1:8191")
    os.environ.setdefault(
        "CLEARANCE_URLS",
        "https://accounts.x.ai,https://x.ai,https://status.x.ai,https://console.x.ai,https://auth.x.ai",
    )

    from grok_register.http_protocol import probe_clearance_and_create
    from grok_register.clearance import format_prewarm_log, prewarm_clearance

    if not args.no_prewarm:
        pre = prewarm_clearance(force=True)
        print(format_prewarm_log(pre), flush=True)
        if pre.get("errors"):
            for err in pre["errors"]:
                print(f"[clearance] err: {err}", flush=True)

    result = probe_clearance_and_create(
        args.email,
        castle_token=args.castle,
        force_prewarm=False,  # just did force above (or skip)
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("create", {}).get("ok") or not result.get("create", {}).get("cf_blocked") else 2

    create = result["create"]
    cfg = result["config"]
    bundle = result["bundle"]
    print("── bundle ──", flush=True)
    print(
        f"  cf_clearance={bundle.get('has_cf_clearance')} "
        f"cookies={bundle.get('cookie_count')} "
        f"header={bundle.get('cookie_header_preview')!r}",
        flush=True,
    )
    print("── CreateEmailValidationCode (HTTP) ──", flush=True)
    print(
        f"  ok={create.get('ok')} http={create.get('http_status')} "
        f"grpc={create.get('grpc_status')!r} cf_blocked={create.get('cf_blocked')} "
        f"ms={create.get('elapsed_ms')} castle_sent={result.get('castle_sent')}",
        flush=True,
    )
    if create.get("error"):
        print(f"  error={create.get('error')}", flush=True)
    if create.get("body_preview"):
        print(f"  body={create.get('body_preview')[:180]!r}", flush=True)
    if create.get("headers"):
        print(f"  hdrs={create.get('headers')}", flush=True)
    print("── scrape config (HTTP) ──", flush=True)
    print(
        f"  site_key={cfg.get('site_key')!r} action_id={cfg.get('action_id')!r} "
        f"state_tree_len={cfg.get('state_tree_len')} src={cfg.get('source')}",
        flush=True,
    )

    # Verdict
    if create.get("cf_blocked"):
        print(
            "\n[判定] CF 仍硬拦 → clearance 未带到 curl_cffi / 出口与 FS 不一致 / CHIPS。",
            flush=True,
        )
        return 2
    if create.get("ok"):
        print(
            "\n[判定] Create 业务 HTTP 通了（无 Castle 或 Castle 可选）。可继续 Verify/ServerAction。",
            flush=True,
        )
        return 0
    print(
        "\n[判定] 过了 CF 但业务拒绝 → 看 grpc/body（缺 Castle / 邮域 / rate_limit）。",
        flush=True,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
