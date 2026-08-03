#!/usr/bin/env python3
"""
[INPUT]: 依赖 keys/cpa_ready 的 xai-*.json 与 _state.tsv，依赖 sub2api 管理 API 的 accounts/data 导入端点
[OUTPUT]: 对外提供 cpa_ready -> sub2api-data 转换与可选导入 CLI
[POS]: scripts 的凭据出货桥接器，连接 acpa_watchdog 的 OAuth 成品与 sub2api 后台账号池
[PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parent.parent
DEFAULT_SOURCE = PROJECT / "keys" / "cpa_ready"
DEFAULT_SSO_SOURCE = PROJECT / "keys"
STATE_FILE = "_state.tsv"
CLI_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
IMPORT_PATH = "/api/v1/admin/accounts/data"
SSO_IMPORT_PATH = "/api/v1/admin/grok/sso-to-oauth"
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class ImportErrorWithBody(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert keys/cpa_ready OAuth files to sub2api-data and optionally import them."
    )
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="directory containing xai-*.json")
    parser.add_argument("--from-sso", action="store_true", help="read SSO from keys/grok.txt, accounts.txt or auth-sessions.jsonl")
    parser.add_argument("--output", help="write sub2api-data JSON to this file with 0600 permissions")
    parser.add_argument("--apply", action="store_true", help="POST the payload to sub2api")
    parser.add_argument("--all", action="store_true", help="ignore _state.tsv and include every xai-*.json")
    parser.add_argument("--include-not-alive", action="store_true", help="include files not marked alive")
    parser.add_argument("--limit", type=int, default=0, help="max accounts to include")
    parser.add_argument("--batch-size", type=int, default=200, help="accounts per import request")
    parser.add_argument("--name-prefix", default="", help="prefix for generated sub2api account names")
    parser.add_argument("--concurrency", type=int, default=1, help="sub2api account concurrency")
    parser.add_argument("--priority", type=int, default=0, help="sub2api account priority")
    parser.add_argument("--skip-default-group-bind", action="store_true", help="do not bind imported accounts to default groups")
    parser.add_argument("--no-skip-existing", action="store_true", help="do not pre-skip existing exact account names")
    parser.add_argument("--base-url", default=os.environ.get("SUB2API_BASE_URL", ""), help="sub2api base URL")
    parser.add_argument("--admin-api-key", default=os.environ.get("SUB2API_ADMIN_API_KEY", ""), help="admin API key")
    parser.add_argument("--jwt", default=os.environ.get("SUB2API_JWT", ""), help="admin JWT")
    return parser.parse_args()


def fail(message: str, code: int = 1) -> None:
    print(f"[sub2api-import] {message}", file=sys.stderr)
    raise SystemExit(code)


def load_alive_names(source: Path) -> set[str] | None:
    state = source / STATE_FILE
    if not state.exists():
        return None
    alive: set[str] = set()
    for raw in state.read_text(encoding="utf-8").splitlines():
        parts = raw.split("\t")
        if len(parts) >= 4 and parts[3] == "alive" and (source / parts[0]).exists():
            alive.add(parts[0])
    return alive


def parse_expired_unix(value: Any) -> int | None:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    normalized = raw.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def clean_map(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value not in ("", None)}


def looks_like_jwt(value: Any) -> bool:
    return isinstance(value, str) and JWT_RE.match(value.strip()) is not None


def extract_sso_from_cookies(cookies: Any) -> str | None:
    if not isinstance(cookies, list):
        return None
    fallback = None
    for cookie in cookies:
        if not isinstance(cookie, dict):
            continue
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "").strip()
        if not value:
            continue
        if name == "sso":
            return value
        if name == "sso-rw" and fallback is None:
            fallback = value
    return fallback


def collect_sso_from_json(value: Any) -> list[str]:
    found: list[str] = []
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(reversed(item))
            continue
        if not isinstance(item, dict):
            continue
        direct = item.get("sso") or item.get("sso_token")
        if looks_like_jwt(direct):
            found.append(str(direct).strip())
            continue
        cookie_sso = extract_sso_from_cookies(item.get("cookies"))
        if looks_like_jwt(cookie_sso):
            found.append(str(cookie_sso).strip())
            continue
        stack.extend(item.values())
    return found


def collect_sso_from_text(raw: str) -> tuple[list[str], dict[str, int]]:
    tokens: list[str] = []
    stats = {"lines": 0, "json": 0, "accounts": 0, "jwt": 0, "skipped": 0}
    text = raw.strip()
    if not text:
        return [], stats

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if parsed is not None:
        found = collect_sso_from_json(parsed)
        stats["json"] += len(found)
        return found, stats

    for index, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        stats["lines"] += 1
        if line.startswith("{") or line.startswith("["):
            try:
                found = collect_sso_from_json(json.loads(line))
            except json.JSONDecodeError:
                stats["skipped"] += 1
                continue
            if found:
                tokens.extend(found)
                stats["json"] += len(found)
            else:
                stats["skipped"] += 1
            continue

        if ":" in line and not looks_like_jwt(line):
            parts = line.split(":")
            if len(parts) >= 3 and looks_like_jwt(":".join(parts[2:])):
                tokens.append(":".join(parts[2:]).strip())
                stats["accounts"] += 1
                continue

        if looks_like_jwt(line):
            tokens.append(line)
            stats["jwt"] += 1
            continue

        stats["skipped"] += 1
    return tokens, stats


def default_sso_files(source: Path) -> list[Path]:
    names = ("auth-sessions.jsonl", "accounts.txt", "grok.txt")
    if source.is_file():
        return [source]
    return [source / name for name in names if (source / name).exists()]


def build_sso_payload(source: Path, *, limit: int, concurrency: int, priority: int) -> tuple[dict[str, Any], dict[str, int]]:
    tokens: list[str] = []
    stats = {"files": 0, "lines": 0, "json": 0, "accounts": 0, "jwt": 0, "skipped": 0, "duplicates": 0, "included": 0}
    for path in default_sso_files(source):
        if not path.exists():
            continue
        stats["files"] += 1
        found, file_stats = collect_sso_from_text(path.read_text(encoding="utf-8"))
        tokens.extend(found)
        for key, value in file_stats.items():
            stats[key] += value

    seen: set[str] = set()
    unique: list[str] = []
    for token in tokens:
        if token in seen:
            stats["duplicates"] += 1
            continue
        seen.add(token)
        unique.append(token)
        if limit > 0 and len(unique) >= limit:
            break

    stats["included"] = len(unique)
    payload = {
        "sso_tokens": unique,
        "credentials": {"base_url": CLI_BASE_URL},
        "concurrency": concurrency,
        "priority": priority,
    }
    return payload, stats


def account_name(record: dict[str, Any], path: Path, prefix: str) -> str:
    email = str(record.get("email") or "").strip()
    sub = str(record.get("sub") or "").strip()
    base = email or (f"grok-{sub[:8]}" if sub else path.stem)
    return f"{prefix}{base}" if prefix else base


def convert_record(path: Path, *, name_prefix: str, concurrency: int, priority: int) -> dict[str, Any]:
    record = json.loads(path.read_text(encoding="utf-8"))
    access_token = str(record.get("access_token") or "").strip()
    if not access_token:
        raise ValueError("missing access_token")
    refresh_token = str(record.get("refresh_token") or "").strip()
    expired_iso = str(record.get("expired") or "").strip()
    expires_unix = parse_expired_unix(record.get("expired"))
    base_url = CLI_BASE_URL

    credentials = clean_map(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": record.get("id_token"),
            "token_type": record.get("token_type"),
            "expires_at": expired_iso,
            "email": record.get("email"),
            "sub": record.get("sub"),
            "base_url": base_url,
        }
    )
    account = clean_map(
        {
            "name": account_name(record, path, name_prefix),
            "platform": "grok",
            "type": "oauth",
            "credentials": credentials,
            "concurrency": concurrency,
            "priority": priority,
        }
    )
    if not refresh_token and expires_unix:
        account["expires_at"] = expires_unix
        account["auto_pause_on_expired"] = True
    return account


def build_payload(
    source: Path,
    *,
    require_alive: bool,
    include_not_alive: bool,
    limit: int,
    name_prefix: str,
    concurrency: int,
    priority: int,
) -> tuple[dict[str, Any], dict[str, int]]:
    files = sorted(source.glob("xai-*.json"))
    alive_names = None if not require_alive else load_alive_names(source)
    if require_alive and alive_names is None:
        fail(f"missing {source / STATE_FILE}; run python3 keys/acpa_watchdog.py --once first or pass --all")

    accounts: list[dict[str, Any]] = []
    stats = {"files": len(files), "not_alive": 0, "bad": 0, "included": 0}
    for path in files:
        if alive_names is not None and path.name not in alive_names and not include_not_alive:
            stats["not_alive"] += 1
            continue
        try:
            accounts.append(
                convert_record(
                    path,
                    name_prefix=name_prefix,
                    concurrency=concurrency,
                    priority=priority,
                )
            )
        except (OSError, json.JSONDecodeError, ValueError):
            stats["bad"] += 1
            continue
        if limit > 0 and len(accounts) >= limit:
            break

    stats["included"] = len(accounts)
    payload = {
        "type": "sub2api-data",
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "proxies": [],
        "accounts": accounts,
    }
    return payload, stats


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)


def auth_headers(args: argparse.Namespace) -> dict[str, str]:
    if args.admin_api_key:
        return {"x-api-key": args.admin_api_key}
    if args.jwt:
        return {"Authorization": f"Bearer {args.jwt}"}
    fail("set SUB2API_ADMIN_API_KEY or SUB2API_JWT before --apply")


def api_json(args: argparse.Namespace, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    if not args.base_url:
        fail("set SUB2API_BASE_URL before --apply or existing-name checks")
    base_url = args.base_url.rstrip("/")
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json", **auth_headers(args)}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(base_url + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ImportErrorWithBody(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
    data_obj = json.loads(raw or "{}")
    if data_obj.get("code") not in (None, 0, "0"):
        raise ImportErrorWithBody(f"{method} {path} failed: {data_obj.get('message') or data_obj.get('code')}")
    return data_obj.get("data")


def list_existing_names(args: argparse.Namespace) -> set[str]:
    names: set[str] = set()
    page = 1
    while True:
        path = f"/api/v1/admin/accounts?page={page}&page_size=1000&platform=grok&type=oauth&lite=1"
        data = api_json(args, "GET", path)
        items = data.get("items") if isinstance(data, dict) else []
        if not items:
            break
        names.update(str(item.get("name") or "") for item in items if isinstance(item, dict))
        total = int(data.get("total") or 0)
        if len(names) >= total:
            break
        page += 1
    return names


def chunked(items: list[dict[str, Any]], size: int) -> list[list[dict[str, Any]]]:
    size = max(size, 1)
    return [items[index : index + size] for index in range(0, len(items), size)]


def import_payload(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    accounts = list(payload["accounts"])
    skipped_existing = 0
    if not args.no_skip_existing:
        existing = list_existing_names(args)
        before = len(accounts)
        accounts = [account for account in accounts if account["name"] not in existing]
        skipped_existing = before - len(accounts)

    result = {"account_created": 0, "account_failed": 0, "skipped_existing": skipped_existing, "errors": []}
    for batch in chunked(accounts, args.batch_size):
        data = {**payload, "accounts": batch}
        body = {"data": data, "skip_default_group_bind": args.skip_default_group_bind}
        imported = api_json(args, "POST", IMPORT_PATH, body)
        result["account_created"] += int(imported.get("account_created") or 0)
        result["account_failed"] += int(imported.get("account_failed") or 0)
        result["errors"].extend(imported.get("errors") or [])
    return result


def import_sso_payload(args: argparse.Namespace, payload: dict[str, Any]) -> dict[str, Any]:
    tokens = list(payload["sso_tokens"])
    result = {"account_created": 0, "account_failed": 0, "errors": []}
    for batch in chunked([{"token": token} for token in tokens], args.batch_size):
        body = {key: value for key, value in payload.items() if key != "sso_tokens"}
        body["sso_tokens"] = [item["token"] for item in batch]
        imported = api_json(args, "POST", SSO_IMPORT_PATH, body)
        created = imported.get("created") if isinstance(imported, dict) else []
        failed = imported.get("failed") if isinstance(imported, dict) else []
        result["account_created"] += len(created or [])
        result["account_failed"] += len(failed or [])
        for item in failed or []:
            result["errors"].append(
                {
                    "index": item.get("index"),
                    "name": item.get("name"),
                    "email": item.get("email"),
                    "message": item.get("error"),
                }
            )
    return result


def main() -> int:
    args = parse_args()
    source_text = args.source
    if args.from_sso and source_text == str(DEFAULT_SOURCE):
        source_text = str(DEFAULT_SSO_SOURCE)
    source = Path(source_text).expanduser().resolve()
    if not source.exists():
        fail(f"missing source dir: {source}")
    if args.concurrency < 0 or args.priority < 0 or args.batch_size <= 0:
        fail("concurrency, priority and batch-size must be positive")

    if args.from_sso:
        payload, stats = build_sso_payload(
            source,
            limit=args.limit,
            concurrency=args.concurrency,
            priority=args.priority,
        )
        summary = {"source": str(source), "mode": "sso-to-oauth", **stats}
        print(json.dumps(summary, ensure_ascii=False, indent=2))

        if args.output:
            write_payload(Path(args.output).expanduser().resolve(), payload)
            print(f"[sub2api-import] wrote sso-to-oauth payload: {args.output}")

        if not args.apply:
            print("[sub2api-import] dry-run only; pass --apply to convert SSO in sub2api")
            return 0
        if not payload["sso_tokens"]:
            fail("no SSO tokens to import")
        start = time.time()
        result = import_sso_payload(args, payload)
        result["elapsed_sec"] = round(time.time() - start, 3)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["account_failed"] == 0 else 1

    if not source.is_dir():
        fail(f"source must be a directory for cpa_ready mode: {source}")

    payload, stats = build_payload(
        source,
        require_alive=not args.all,
        include_not_alive=args.include_not_alive,
        limit=args.limit,
        name_prefix=args.name_prefix,
        concurrency=args.concurrency,
        priority=args.priority,
    )
    summary = {"source": str(source), **stats}
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    if args.output:
        write_payload(Path(args.output).expanduser().resolve(), payload)
        print(f"[sub2api-import] wrote payload: {args.output}")

    if not args.apply:
        print("[sub2api-import] dry-run only; pass --apply to import")
        return 0

    if not payload["accounts"]:
        fail("no accounts to import")
    start = time.time()
    result = import_payload(args, payload)
    result["elapsed_sec"] = round(time.time() - start, 3)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["account_failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
