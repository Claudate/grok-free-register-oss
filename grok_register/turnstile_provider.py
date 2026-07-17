"""Turnstile token providers for the S stage.

Default path remains the local browser solver in register.py.
External providers exist to break the Physical_Sem / browser mint ceiling
when token throughput becomes the real bottleneck.

Providers
---------
browser   — no-op marker; register.py uses Playwright solver (default, 开箱)
lite      — HTTP client: createTask + getTaskResult against an *external*
            YesCaptcha-shaped farm (e.g. lite :5072). Code-level wire only;
            this repo does NOT ship the farm / Camoufox / Docker image.
capsolver — commercial CapSolver createTask + getTaskResult (needs API key)
http      — generic one-shot POST → {token|turnstile_token|data.token}

Env
---
TURNSTILE_PROVIDER=browser|lite|capsolver|http
TURNSTILE_SITE_URL=https://accounts.x.ai/sign-up
LITE_SOLVER_URL=http://127.0.0.1:5072          # external farm base (no path)
LITE_SOLVER_CLIENT_KEY=                        # optional; only if farm set API_KEY
LITE_SOLVER_TASK_TYPE=TurnstileTaskProxyless
LITE_SOLVER_POLL_INTERVAL_SEC=0.8
CAPSOLVER_API_KEY=...
CAPSOLVER_API_URL=https://api.capsolver.com
CAPSOLVER_TASK_TYPE=AntiTurnstileTaskProxyLess
TURNSTILE_HTTP_URL=http://127.0.0.1:9100/solve
TURNSTILE_HTTP_TOKEN=...
TURNSTILE_HTTP_TIMEOUT_SEC=90
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional, Tuple

import requests as req


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def resolve_provider_name() -> str:
    name = _env("TURNSTILE_PROVIDER", "browser").lower()
    if name in {"", "local", "playwright", "browser"}:
        return "browser"
    if name in {"lite", "local-solver", "turnstile-solver", "farm", "yescaptcha"}:
        return "lite"
    if name in {"capsolver", "cap"}:
        return "capsolver"
    if name in {"http", "api", "external"}:
        return "http"
    return name


def uses_browser(provider: Optional[str] = None) -> bool:
    return (provider or resolve_provider_name()) == "browser"


def site_url() -> str:
    return _env("TURNSTILE_SITE_URL") or _env("SITE_URL", "https://accounts.x.ai") + "/sign-up"


def mint_turnstile_token(site_key: str, *, provider: Optional[str] = None) -> Tuple[Optional[str], Dict[str, Any]]:
    """Blocking mint. Returns (token_or_None, trace).

    Designed to run in a worker thread / executor so asyncio S workers stay free.
    Does not touch Playwright or Physical_Sem.
    """
    name = provider or resolve_provider_name()
    started = time.time()
    trace: Dict[str, Any] = {
        "provider": name,
        "goto_s": 0.0,
        "reused": False,
        "reuse_count": 0,
        "inject_s": 0.0,
        "initial_s": 0.0,
        "click_s": 0.0,
        "wait_s": 0.0,
        "visible_frame": False,
    }
    if not site_key:
        trace["error"] = "missing_site_key"
        return None, trace
    if name == "browser":
        trace["error"] = "browser_provider_not_callable_here"
        return None, trace
    try:
        if name == "lite":
            token = _mint_two_step(
                site_key,
                trace,
                base=_env("LITE_SOLVER_URL", "http://127.0.0.1:5072"),
                client_key=_env("LITE_SOLVER_CLIENT_KEY"),
                task_type=_env("LITE_SOLVER_TASK_TYPE", "TurnstileTaskProxyless"),
                poll_interval=float(_env("LITE_SOLVER_POLL_INTERVAL_SEC", "0.8") or "0.8"),
                require_client_key=False,
                timeout_error="lite_solver_timeout",
            )
        elif name == "capsolver":
            token = _mint_two_step(
                site_key,
                trace,
                base=_env("CAPSOLVER_API_URL", "https://api.capsolver.com"),
                client_key=_env("CAPSOLVER_API_KEY"),
                task_type=_env("CAPSOLVER_TASK_TYPE", "AntiTurnstileTaskProxyLess"),
                poll_interval=float(_env("CAPSOLVER_POLL_INTERVAL_SEC", "1") or "1"),
                require_client_key=True,
                missing_key_error="missing_CAPSOLVER_API_KEY",
                timeout_error="capsolver_timeout",
            )
        elif name == "http":
            token = _mint_http(site_key, trace)
        else:
            trace["error"] = f"unknown_provider:{name}"
            token = None
    except Exception as exc:
        trace["error"] = type(exc).__name__
        token = None
    trace["wait_s"] = max(0.0, time.time() - started - float(trace.get("goto_s") or 0.0))
    return token, trace


def _mint_two_step(
    site_key: str,
    trace: Dict[str, Any],
    *,
    base: str,
    client_key: str,
    task_type: str,
    poll_interval: float,
    require_client_key: bool,
    timeout_error: str,
    missing_key_error: str = "missing_client_key",
) -> Optional[str]:
    """createTask + getTaskResult (YesCaptcha / CapSolver / external farm shape).

    External farms may accept empty clientKey when their API_KEY is unset.
    Commercial CapSolver still needs a real key. This is pure HTTP — no browser.
    """
    base = (base or "").rstrip("/")
    if not base:
        trace["error"] = "missing_solver_base_url"
        return None
    if require_client_key and not client_key:
        trace["error"] = missing_key_error
        return None

    timeout = max(10, _env_int("TURNSTILE_HTTP_TIMEOUT_SEC", 90))
    deadline = time.time() + timeout

    task: Dict[str, Any] = {
        "type": task_type or "TurnstileTaskProxyless",
        "websiteURL": site_url(),
        "websiteKey": site_key,
    }
    action = _env("TURNSTILE_ACTION")
    cdata = _env("TURNSTILE_CDATA")
    if action or cdata:
        meta: Dict[str, str] = {}
        if action:
            meta["action"] = action
            task["action"] = action
        if cdata:
            meta["cdata"] = cdata
            task["cdata"] = cdata
        task["metadata"] = meta

    create_body: Dict[str, Any] = {"task": task}
    if client_key:
        create_body["clientKey"] = client_key

    create_started = time.time()
    create = req.post(f"{base}/createTask", json=create_body, timeout=min(30, timeout))
    create.raise_for_status()
    create_json = create.json()
    trace["goto_s"] = time.time() - create_started
    if create_json.get("errorId"):
        trace["error"] = str(
            create_json.get("errorDescription")
            or create_json.get("errorCode")
            or "createTask_error"
        )
        return None
    task_id = create_json.get("taskId")
    if not task_id:
        token = _extract_token(create_json)
        if token:
            return token
        trace["error"] = "missing_taskId"
        return None

    poll_interval = max(0.2, float(poll_interval or 0.8))
    while time.time() < deadline:
        time.sleep(poll_interval)
        poll_body: Dict[str, Any] = {"taskId": task_id}
        if client_key:
            poll_body["clientKey"] = client_key
        result = req.post(
            f"{base}/getTaskResult",
            json=poll_body,
            timeout=min(30, max(5, int(deadline - time.time()))),
        )
        result.raise_for_status()
        body = result.json()
        if body.get("errorId"):
            trace["error"] = str(
                body.get("errorDescription")
                or body.get("errorCode")
                or "getTaskResult_error"
            )
            return None
        status = (body.get("status") or "").lower()
        if status == "ready":
            token = _extract_token(body)
            if not token:
                trace["error"] = "ready_without_token"
            return token
        if status in {"failed", "error"}:
            trace["error"] = f"task_{status}"
            return None
    trace["error"] = timeout_error
    return None


def _mint_http(site_key: str, trace: Dict[str, Any]) -> Optional[str]:
    url = _env("TURNSTILE_HTTP_URL")
    if not url:
        trace["error"] = "missing_TURNSTILE_HTTP_URL"
        return None
    timeout = max(10, _env_int("TURNSTILE_HTTP_TIMEOUT_SEC", 90))
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    bearer = _env("TURNSTILE_HTTP_TOKEN")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    payload = {
        "websiteURL": site_url(),
        "websiteKey": site_key,
        "sitekey": site_key,
        "url": site_url(),
    }
    action = _env("TURNSTILE_ACTION")
    cdata = _env("TURNSTILE_CDATA")
    if action:
        payload["action"] = action
    if cdata:
        payload["cdata"] = cdata

    started = time.time()
    resp = req.post(url, json=payload, headers=headers, timeout=timeout)
    trace["goto_s"] = time.time() - started
    resp.raise_for_status()
    try:
        body = resp.json()
    except Exception:
        text = (resp.text or "").strip()
        if len(text) > 20:
            return text
        trace["error"] = "non_json_empty_body"
        return None
    token = _extract_token(body)
    if not token:
        trace["error"] = "http_response_without_token"
    return token


def _extract_token(body: Any) -> Optional[str]:
    if not isinstance(body, dict):
        if isinstance(body, str) and len(body) > 20:
            return body
        return None
    for key in ("token", "turnstile_token", "turnstileToken", "cf_turnstile_response"):
        val = body.get(key)
        if isinstance(val, str) and len(val) > 20:
            return val
    solution = body.get("solution")
    if isinstance(solution, dict):
        for key in ("token", "turnstile_token", "text", "gRecaptchaResponse"):
            val = solution.get(key)
            if isinstance(val, str) and len(val) > 20:
                return val
    data = body.get("data")
    if isinstance(data, dict):
        for key in ("token", "turnstile_token"):
            val = data.get(key)
            if isinstance(val, str) and len(val) > 20:
                return val
    return None
