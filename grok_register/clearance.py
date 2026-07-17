"""Cloudflare clearance prewarm via FlareSolverr.

This module does NOT start containers. Deploy the stack under ``clearance/``:

    cd clearance && docker compose up -d

Default API: ``http://127.0.0.1:8191`` (host bind from that compose).

Proxy roles (do not mix):
  - REGISTER_PROXY / HTTP_PROXY / HTTPS_PROXY
      → grok-free-register Playwright + httpx egress (this process)
  - CLEARANCE_PROXY
      → optional proxy *inside* FlareSolverr's browser (must be reachable
        from the FS *container*, e.g. http://privoxy:8118 on the compose net)
  - FLARESOLVERR_URL
      → host-side URL to reach FS (http://127.0.0.1:8191)

Clearance cookies are only valid on the same egress path they were minted on.
If REGISTER_PROXY points at host Privoxy→WARP, set CLEARANCE_PROXY to the
docker-internal privoxy URL so FS solves on the same WARP exit.

Default prewarm hosts: accounts.x.ai, x.ai, status.x.ai, console.x.ai, auth.x.ai.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import unquote, urlparse

# CF-fronted x.ai family roots (no path suffix; path pages share host clearance)
DEFAULT_CLEARANCE_URLS = (
    "https://accounts.x.ai",
    "https://x.ai",
    "https://status.x.ai",
    "https://console.x.ai",
    "https://auth.x.ai",
)

_lock = threading.RLock()
_cache: dict[str, Any] = {
    "cookies": [],          # Playwright cookie dicts
    "user_agent": "",
    "fetched_at": 0.0,
    "hosts": [],
    "last_error": "",
}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = str(os.environ.get(name, "")).strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except (TypeError, ValueError):
        return default


def clearance_enabled() -> bool:
    return _env_bool("CLEARANCE_ENABLED", False)


def flaresolverr_url() -> str:
    return (os.environ.get("FLARESOLVERR_URL") or "http://127.0.0.1:8191").strip().rstrip("/")


def clearance_timeout_sec() -> int:
    return max(10, _env_int("CLEARANCE_TIMEOUT_SEC", 60))


def clearance_refresh_sec() -> int:
    return max(60, _env_int("CLEARANCE_REFRESH_SEC", 3000))


def clearance_urls() -> list[str]:
    raw = (os.environ.get("CLEARANCE_URLS") or "").strip()
    if not raw:
        return list(DEFAULT_CLEARANCE_URLS)
    urls = []
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "://" not in item:
            item = "https://" + item
        # strip path/query — host-level prewarm only
        parsed = urlparse(item)
        if parsed.scheme and parsed.netloc:
            urls.append(f"{parsed.scheme}://{parsed.netloc}")
        else:
            urls.append(item.rstrip("/"))
    # de-dupe preserve order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out or list(DEFAULT_CLEARANCE_URLS)


def register_proxy_url() -> str:
    """Egress proxy for *this* process (Playwright / httpx)."""
    for key in ("REGISTER_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def clearance_proxy_url() -> str:
    """Proxy for FlareSolverr browser (container-reachable). Empty = FS direct egress."""
    explicit = (os.environ.get("CLEARANCE_PROXY") or "").strip()
    if explicit:
        return explicit
    # Do NOT auto-forward host REGISTER_PROXY into FS — 127.0.0.1 inside
    # the container is not the host. Operator must set CLEARANCE_PROXY when
    # minting on WARP (e.g. http://privoxy:8118).
    return ""


def playwright_proxy_settings() -> dict[str, str] | None:
    """Playwright launch/context proxy dict, or None for direct.

    Chromium often rejects user:pass embedded in server URL with
    net::ERR_INVALID_AUTH_CREDENTIALS. Split credentials into
    username/password fields instead.
    """
    url = register_proxy_url()
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.hostname:
        # Non-URL form; pass through and let Playwright report errors.
        return {"server": url}
    port = f":{parsed.port}" if parsed.port else ""
    server = f"{parsed.scheme}://{parsed.hostname}{port}"
    settings: dict[str, str] = {"server": server}
    if parsed.username is not None:
        # urlparse keeps percent-encoding; unquote for real credentials.
        settings["username"] = unquote(parsed.username)
        settings["password"] = unquote(parsed.password or "")
    return settings


def httpx_proxy_mounts() -> str | None:
    """Single proxy URL for httpx.AsyncClient(proxy=...), or None."""
    return register_proxy_url() or None


def _host_from_url(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _fs_post(payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    endpoint = f"{flaresolverr_url()}/v1"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def _cookie_to_playwright(item: dict[str, Any], fallback_host: str) -> dict[str, Any] | None:
    name = str(item.get("name") or "").strip()
    if not name:
        return None
    value = str(item.get("value") or "")
    domain = str(item.get("domain") or "").strip()
    if not domain:
        domain = fallback_host
    if domain and not domain.startswith(".") and domain.count(".") >= 1:
        # keep host-only cookies host-only; CF often uses .x.ai
        pass
    path = str(item.get("path") or "/") or "/"
    cookie: dict[str, Any] = {
        "name": name,
        "value": value,
        "domain": domain,
        "path": path,
    }
    if "httpOnly" in item:
        cookie["httpOnly"] = bool(item.get("httpOnly"))
    if "secure" in item:
        cookie["secure"] = bool(item.get("secure"))
    else:
        cookie["secure"] = True
    same_site = item.get("sameSite") or item.get("same_site")
    if same_site:
        # Playwright: Strict | Lax | None
        normalized = str(same_site).capitalize()
        if normalized.lower() == "none":
            normalized = "None"
        if normalized in {"Strict", "Lax", "None"}:
            cookie["sameSite"] = normalized
    expires = item.get("expires")
    if expires is not None:
        try:
            exp = float(expires)
            # FS may return ms or session -1
            if exp > 0:
                if exp > 1e12:
                    exp = exp / 1000.0
                cookie["expires"] = exp
        except (TypeError, ValueError):
            pass
    return cookie


def _merge_cookies(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    index: dict[tuple[str, str, str], int] = {}
    merged: list[dict[str, Any]] = []
    for cookie in existing:
        key = (
            str(cookie.get("name") or ""),
            str(cookie.get("domain") or ""),
            str(cookie.get("path") or "/"),
        )
        index[key] = len(merged)
        merged.append(cookie)
    for cookie in incoming:
        key = (
            str(cookie.get("name") or ""),
            str(cookie.get("domain") or ""),
            str(cookie.get("path") or "/"),
        )
        if key in index:
            merged[index[key]] = cookie
        else:
            index[key] = len(merged)
            merged.append(cookie)
    return merged


def prewarm_clearance(*, force: bool = False) -> dict[str, Any]:
    """Hit FlareSolverr for each CLEARANCE_URLS host; cache Playwright cookies.

    Safe to call when disabled — returns status without network.
    """
    if not clearance_enabled():
        return {
            "enabled": False,
            "ok": True,
            "skipped": True,
            "message": "CLEARANCE_ENABLED=0",
            "cookies": 0,
        }

    with _lock:
        age = time.time() - float(_cache.get("fetched_at") or 0)
        if (
            not force
            and _cache.get("cookies")
            and age < clearance_refresh_sec()
        ):
            return {
                "enabled": True,
                "ok": True,
                "cached": True,
                "age_sec": round(age, 1),
                "cookies": len(_cache["cookies"]),
                "hosts": list(_cache.get("hosts") or []),
                "user_agent": _cache.get("user_agent") or "",
            }

    urls = clearance_urls()
    timeout = float(clearance_timeout_sec())
    fs_proxy = clearance_proxy_url()
    max_timeout_ms = int(timeout * 1000)
    merged: list[dict[str, Any]] = []
    user_agent = ""
    hosts_ok: list[str] = []
    errors: list[str] = []
    per_host: list[dict[str, Any]] = []

    for url in urls:
        host = _host_from_url(url)
        payload: dict[str, Any] = {
            "cmd": "request.get",
            "url": url,
            "maxTimeout": max_timeout_ms,
        }
        if fs_proxy:
            payload["proxy"] = {"url": fs_proxy}
        t0 = time.time()
        try:
            data = _fs_post(payload, timeout=timeout + 15)
            elapsed = round(time.time() - t0, 2)
            if str(data.get("status") or "").lower() != "ok":
                errors.append(f"{host}: status={data.get('status')} msg={data.get('message')}")
                per_host.append({"host": host, "ok": False, "elapsed": elapsed, "error": data.get("message")})
                continue
            solution = data.get("solution") if isinstance(data.get("solution"), dict) else {}
            raw_cookies = solution.get("cookies") or []
            pw_cookies = []
            if isinstance(raw_cookies, list):
                for item in raw_cookies:
                    if not isinstance(item, dict):
                        continue
                    converted = _cookie_to_playwright(item, host)
                    if converted:
                        pw_cookies.append(converted)
            merged = _merge_cookies(merged, pw_cookies)
            ua = str(solution.get("userAgent") or "").strip()
            if ua:
                user_agent = ua
            has_cf = any(c.get("name") == "cf_clearance" for c in pw_cookies)
            hosts_ok.append(host)
            per_host.append(
                {
                    "host": host,
                    "ok": True,
                    "elapsed": elapsed,
                    "http": solution.get("status"),
                    "cookies": len(pw_cookies),
                    "cf_clearance": has_cf,
                }
            )
        except Exception as exc:  # noqa: BLE001 — surface to operator log
            elapsed = round(time.time() - t0, 2)
            errors.append(f"{host}: {exc}")
            per_host.append({"host": host, "ok": False, "elapsed": elapsed, "error": str(exc)})

    with _lock:
        if merged:
            _cache["cookies"] = merged
            _cache["user_agent"] = user_agent
            _cache["fetched_at"] = time.time()
            _cache["hosts"] = hosts_ok
            _cache["last_error"] = "; ".join(errors)
        elif errors:
            _cache["last_error"] = "; ".join(errors)

    return {
        "enabled": True,
        "ok": bool(merged) or not errors,
        "cached": False,
        "cookies": len(merged),
        "hosts": hosts_ok,
        "user_agent": user_agent,
        "errors": errors,
        "detail": per_host,
        "flaresolverr": flaresolverr_url(),
        "clearance_proxy": fs_proxy or "(direct)",
        "register_proxy": register_proxy_url() or "(direct)",
    }


def cached_playwright_cookies() -> list[dict[str, Any]]:
    with _lock:
        return [dict(c) for c in (_cache.get("cookies") or [])]


def cached_user_agent() -> str:
    with _lock:
        return str(_cache.get("user_agent") or "")


async def apply_clearance_to_context(context) -> int:
    """Inject cached CF cookies into a Playwright BrowserContext. Returns count."""
    cookies = cached_playwright_cookies()
    if not cookies or context is None:
        return 0
    try:
        await context.add_cookies(cookies)
        return len(cookies)
    except Exception:
        return 0


def cookie_header_for_host(host: str) -> str:
    """Build a Cookie request header for *host* from the clearance cache.

    Used by pure-HTTP clients (curl_cffi / httpx). Domain matching is
    intentionally simple: cookie domain without leading dot must equal host,
    or host must end with ``.`` + bare domain when cookie is a parent domain.
    """
    host = (host or "").lower().lstrip(".")
    if not host:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for cookie in cached_playwright_cookies():
        name = str(cookie.get("name") or "").strip()
        if not name or name in seen:
            continue
        domain = str(cookie.get("domain") or "").lower().lstrip(".")
        if not domain:
            continue
        if host == domain or host.endswith("." + domain):
            parts.append(f"{name}={cookie.get('value') or ''}")
            seen.add(name)
    return "; ".join(parts)


def clearance_bundle() -> dict[str, Any]:
    """Snapshot for HTTP sessions: cookies + UA + age."""
    with _lock:
        cookies = [dict(c) for c in (_cache.get("cookies") or [])]
        return {
            "cookies": cookies,
            "user_agent": str(_cache.get("user_agent") or ""),
            "fetched_at": float(_cache.get("fetched_at") or 0),
            "hosts": list(_cache.get("hosts") or []),
            "cookie_header_accounts": cookie_header_for_host("accounts.x.ai"),
            "cookie_header_auth": cookie_header_for_host("auth.x.ai"),
            "has_cf_clearance": any(
                str(c.get("name") or "") == "cf_clearance" for c in cookies
            ),
        }


def format_prewarm_log(result: dict[str, Any]) -> str:
    if result.get("skipped"):
        return "[clearance] 未启用 (CLEARANCE_ENABLED=0)"
    if result.get("cached"):
        return (
            f"[clearance] 复用缓存 cookies={result.get('cookies')} "
            f"age={result.get('age_sec')}s hosts={','.join(result.get('hosts') or [])}"
        )
    detail = result.get("detail") or []
    parts = []
    for row in detail:
        host = row.get("host")
        if row.get("ok"):
            cf = "cf+" if row.get("cf_clearance") else "cf-"
            parts.append(f"{host}:{row.get('elapsed')}s/{cf}")
        else:
            parts.append(f"{host}:FAIL")
    err = result.get("errors") or []
    suffix = f" errors={len(err)}" if err else ""
    return (
        f"[clearance] 预热完成 cookies={result.get('cookies')} "
        f"fs={result.get('flaresolverr')} reg_proxy={result.get('register_proxy')} "
        f"fs_proxy={result.get('clearance_proxy')} | " + " ".join(parts) + suffix
    )
