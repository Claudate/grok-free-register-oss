"""Pure-HTTP legs for x.ai signup (curl_cffi).

Flow:
  FlareSolverr / browser mints cf_clearance → inject into curl_cffi Session
  → Connect grpc-web + Next Server Action over HTTP.

What this module does NOT mint:
  - Castle request tokens (obfuscated JS; pass in or leave empty for A/B)
  - Turnstile tokens (iframe; pass in for createUser / CreateSession)

Wire formats match live accounts.x.ai capture.

Critical: curl_cffi TLS impersonate must be a *recent* Chrome profile.
chrome131 + modern CF clearance is rejected (403 HTML). Prefer chrome146
(library default) and fall back through a known-good list. Always send the
cf_clearance Cookie header explicitly — jar domain matching alone is flaky.

Throughput notes:
  - Server Action body must carry conversionId + castleRequestToken +
    react-query second arg; missing fields → silent 0-sso.
  - One Session RLock serializes all P/C; use XAIHttpClientPool for parallel.
  - CreateSession password fallback recovers sso when RSC has no set-cookie chain.
"""

from __future__ import annotations

import json
import queue
import random
import re
import struct
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from grok_register.clearance import (
    cached_user_agent,
    clearance_bundle,
    cookie_header_for_host,
    prewarm_clearance,
    register_proxy_url,
)

SITE_URL = "https://accounts.x.ai"
CONNECT_CREATE = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
CONNECT_VERIFY = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
CONNECT_CREATE_SESSION = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateSession"
SIGNUP_URL = f"{SITE_URL}/sign-up"
SIGNUP_URL_GROK = f"{SITE_URL}/sign-up?redirect=grok-com"

# curl_cffi 0.15 default; chrome131 is rejected by CF against current clearance.
DEFAULT_IMPERSONATE = "chrome146"
# Pool size for parallel P/C when PROTOCOL_HTTP=1 (each client owns a Session+RLock).
DEFAULT_HTTP_POOL_SIZE = 8

# Prefer newest first. Session() may accept a name that request() later rejects —
# resolve_impersonate probes with a no-op Session construct + request path later.
_IMPERSONATE_CANDIDATES = (
    "chrome146",
    "chrome142",
    "chrome136",
    "chrome133a",
    "chrome131",
    "chrome124",
    "chrome",
)


def _chrome_major_from_ua(ua: str) -> int | None:
    m = re.search(r"Chrome/(\d+)", ua or "")
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def resolve_impersonate(preferred: str | None = None, *, user_agent: str = "") -> str:
    """Pick a curl_cffi impersonate profile that this install actually supports.

    CF binds clearance to JA3/UA. Stale profiles (e.g. chrome131) get 403 even
    with a fresh cf_clearance. Prefer DEFAULT / library-supported modern Chrome.
    """
    from curl_cffi import requests as cf_requests

    ordered: list[str] = []
    for name in (preferred, DEFAULT_IMPERSONATE, *_IMPERSONATE_CANDIDATES):
        n = (name or "").strip()
        if n and n not in ordered:
            ordered.append(n)

    # If FS UA reports a major we have an exact profile for, try it first.
    major = _chrome_major_from_ua(user_agent)
    if major is not None:
        exact = f"chrome{major}"
        if exact not in ordered:
            ordered.insert(0, exact)

    last_err: Exception | None = None
    for name in ordered:
        try:
            sess = cf_requests.Session(impersonate=name, verify=False, timeout=5)
            try:
                # Some builds accept Session(impersonate=X) but fail on first request.
                # A HEAD to example.org is enough to surface ImpersonateError.
                sess.head("https://example.org", timeout=5)
            except Exception as exc:
                # Network errors are fine — impersonate was accepted.
                if type(exc).__name__ == "ImpersonateError" or "Impersonating" in str(exc):
                    last_err = exc
                    continue
            finally:
                try:
                    sess.close()
                except Exception:
                    pass
            return name
        except Exception as exc:
            last_err = exc
            continue
    if last_err:
        raise last_err
    return DEFAULT_IMPERSONATE


def _sec_ch_ua_for(ua: str, impersonate: str) -> str:
    major = _chrome_major_from_ua(ua)
    if major is None:
        m = re.search(r"chrome(\d+)", impersonate or "", re.I)
        major = int(m.group(1)) if m else 146
    return f'"Chromium";v="{major}", "Google Chrome";v="{major}", "Not_A Brand";v="99"'


def pb_varint(n: int) -> bytes:
    parts: list[int] = []
    while n > 0x7F:
        parts.append((n & 0x7F) | 0x80)
        n >>= 7
    parts.append(n)
    return bytes(parts)


def pb_str(fid: int, val: str) -> bytes:
    vb = val.encode("utf-8")
    return struct.pack("B", (fid << 3) | 2) + pb_varint(len(vb)) + vb


def connect_frame(*fields: bytes) -> bytes:
    """Connect unary envelope: flags(0) + big-endian length + protobuf body."""
    inner = b"".join(fields)
    return b"\x00" + struct.pack(">I", len(inner)) + inner


def pb_bytes(fid: int, raw: bytes) -> bytes:
    """Length-delimited bytes / nested-message field."""
    return struct.pack("B", (fid << 3) | 2) + pb_varint(len(raw)) + raw


def encode_create_session_request(
    email: str,
    password: str,
    *,
    turnstile_token: str,
    castle_request_token: str = "",
) -> bytes:
    """CreateSessionRequest wire layout (lite oauth_protocol, 2026-07).

    field1 Credentials {
      field1 EmailAndPassword { email=1, clearTextPassword=2 }
    }
    field4 AntiAbuseToken {
      field1 turnstileToken
      field2 castleRequestToken (may be empty)
    }
    """
    email_pw = pb_str(1, email) + pb_str(2, password)
    credentials = pb_bytes(1, email_pw)
    anti = pb_str(1, turnstile_token) + pb_str(2, castle_request_token or "")
    return credentials + pb_bytes(4, anti)


def is_cloudflare_body(status: int | None, text: str, headers: dict | None = None) -> bool:
    if status in (403, 503):
        lowered = (text or "")[:2000].lower()
        if "cloudflare" in lowered or "cf-ray" in lowered or "just a moment" in lowered:
            return True
        if "sorry, you have been blocked" in lowered:
            return True
    if headers:
        server = str(headers.get("server") or headers.get("Server") or "").lower()
        if server == "cloudflare" and status in (403, 503):
            return True
    return False


@dataclass
class ConnectResult:
    ok: bool
    grpc_status: str = ""
    http_status: int = 0
    body_preview: str = ""
    cf_blocked: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str = ""


@dataclass
class SignupResult:
    ok: bool
    http_status: int = 0
    sso: str | None = None
    set_cookie_url: str | None = None
    body_preview: str = ""
    markers: str = ""
    cf_blocked: bool = False
    elapsed_ms: int = 0
    error: str = ""
    # How sso was obtained: set_cookie | direct | jar | create_session | ""
    sso_source: str = ""


@dataclass
class SignupConfig:
    site_key: str = ""
    action_id: str = ""
    state_tree: str = ""
    source: str = ""


class XAIHttpClient:
    """curl_cffi session bound to clearance cookies + optional proxy.

    Each instance is thread-safe via ``_lock``. For parallel P/C use
    ``XAIHttpClientPool`` (one Session per slot) so workers don't serialize
    on a single RLock.
    """

    def __init__(
        self,
        *,
        proxy: str | None = None,
        impersonate: str | None = None,
        user_agent: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        from curl_cffi import requests as cf_requests

        self.proxy = (proxy if proxy is not None else register_proxy_url()) or ""
        self.timeout = timeout
        self._requests = cf_requests
        self._lock = threading.RLock()

        ua = (user_agent or cached_user_agent() or "").strip()
        if not ua:
            ua = (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/146.0.0.0 Safari/537.36"
            )
        self.user_agent = ua
        # Resolve a profile this curl_cffi build can actually request with.
        self.impersonate = resolve_impersonate(impersonate, user_agent=ua)

        kwargs: dict[str, Any] = {
            "impersonate": self.impersonate,
            "verify": False,
            "timeout": timeout,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy

        self.session = cf_requests.Session(**kwargs)
        platform = '"Linux"' if "Linux" in ua or "X11" in ua else '"Windows"'
        self.session.headers.update(
            {
                "user-agent": ua,
                "accept-language": "en-US,en;q=0.9",
                "sec-ch-ua": _sec_ch_ua_for(ua, self.impersonate),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": platform,
            }
        )
        self.apply_clearance()

    def close(self) -> None:
        with self._lock:
            try:
                self.session.close()
            except Exception:
                pass

    def apply_clearance(self) -> dict[str, Any]:
        """Load clearance cache into the session cookie jar + sticky Cookie hdr."""
        with self._lock:
            bundle = clearance_bundle()
            for cookie in bundle.get("cookies") or []:
                name = str(cookie.get("name") or "").strip()
                if not name:
                    continue
                value = str(cookie.get("value") or "")
                domain = str(cookie.get("domain") or "").strip() or ".x.ai"
                path = str(cookie.get("path") or "/") or "/"
                try:
                    self.session.cookies.set(name, value, domain=domain, path=path)
                except Exception:
                    try:
                        self.session.cookies.set(name, value)
                    except Exception:
                        continue
            if bundle.get("user_agent"):
                self.user_agent = str(bundle["user_agent"])
                self.session.headers["user-agent"] = self.user_agent
                self.session.headers["sec-ch-ua"] = _sec_ch_ua_for(
                    self.user_agent, self.impersonate
                )
                platform = (
                    '"Linux"'
                    if ("Linux" in self.user_agent or "X11" in self.user_agent)
                    else '"Windows"'
                )
                self.session.headers["sec-ch-ua-platform"] = platform
            # Explicit Cookie header — jar domain matching alone is unreliable
            # against multi-domain clearance dumps from FlareSolverr.
            cookie_hdr = (
                bundle.get("cookie_header_accounts")
                or cookie_header_for_host("accounts.x.ai")
                or ""
            )
            if cookie_hdr:
                self.session.headers["cookie"] = cookie_hdr
            return bundle

    def refresh_clearance(self, *, force: bool = True) -> dict[str, Any]:
        result = prewarm_clearance(force=force)
        self.apply_clearance()
        return result

    def _cookie_headers(self) -> dict[str, str]:
        """Per-request Cookie overlay (clearance may rotate under the lock)."""
        hdr = cookie_header_for_host("accounts.x.ai") or ""
        if not hdr:
            try:
                jar = dict(self.session.cookies)
                if jar:
                    hdr = "; ".join(f"{k}={v}" for k, v in jar.items() if k and v is not None)
            except Exception:
                hdr = ""
        return {"cookie": hdr} if hdr else {}

    def _connect_headers(self) -> dict[str, str]:
        headers = {
            "content-type": "application/grpc-web+proto",
            "x-grpc-web": "1",
            "x-user-agent": "connect-es/2.1.1",
            "origin": SITE_URL,
            "referer": f"{SITE_URL}/sign-up",
            "accept": "*/*",
        }
        headers.update(self._cookie_headers())
        return headers

    def create_email_code(
        self,
        email: str,
        *,
        castle_token: str = "",
        retry_on_cf: bool = True,
    ) -> ConnectResult:
        """CreateEmailValidationCode: field1=email, field3=castleRequestToken (optional)."""
        fields = [pb_str(1, email)]
        if castle_token:
            fields.append(pb_str(3, castle_token))
        body = connect_frame(*fields)
        return self._connect_post(CONNECT_CREATE, body, retry_on_cf=retry_on_cf)

    def verify_email_code(
        self,
        email: str,
        code: str,
        *,
        retry_on_cf: bool = True,
    ) -> ConnectResult:
        body = connect_frame(pb_str(1, email), pb_str(2, code))
        return self._connect_post(CONNECT_VERIFY, body, retry_on_cf=retry_on_cf)

    def _connect_post(
        self,
        url: str,
        body: bytes,
        *,
        retry_on_cf: bool,
    ) -> ConnectResult:
        t0 = time.time()
        with self._lock:
            try:
                resp = self.session.post(url, data=body, headers=self._connect_headers())
            except Exception as exc:
                return ConnectResult(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.time() - t0) * 1000),
                )

            text = ""
            try:
                text = resp.text or ""
            except Exception:
                try:
                    text = (resp.content or b"")[:800].decode("utf-8", "replace")
                except Exception:
                    text = ""

            headers = {str(k): str(v) for k, v in dict(resp.headers or {}).items()}
            status = int(getattr(resp, "status_code", 0) or 0)
            cf = is_cloudflare_body(status, text, headers)

        if cf and retry_on_cf:
            self.refresh_clearance(force=True)
            return self._connect_post(url, body, retry_on_cf=False)

        grpc_status = headers.get("grpc-status") or headers.get("Grpc-Status") or ""
        # Trailers may carry grpc-status inside body for grpc-web.
        if not grpc_status:
            m = re.search(r"grpc-status[:\s]+(\d+)", text or "", re.I)
            if m:
                grpc_status = m.group(1)
        if not grpc_status and status == 200 and not cf:
            grpc_status = "0"
        if not grpc_status and status != 200:
            grpc_status = str(status)

        ok = (not cf) and status == 200 and str(grpc_status) in {"0", ""}
        return ConnectResult(
            ok=ok,
            grpc_status=str(grpc_status),
            http_status=status,
            body_preview=text[:400].replace("\n", " "),
            cf_blocked=cf,
            headers={k: v for k, v in headers.items() if k.lower().startswith(("grpc", "content", "cf-"))},
            elapsed_ms=int((time.time() - t0) * 1000),
            error="" if ok else f"http={status} grpc={grpc_status} cf={cf}",
        )

    def fetch_signup_html(self, *, retry_on_cf: bool = True) -> tuple[int, str, bool]:
        t0 = time.time()
        with self._lock:
            try:
                headers = {
                    "accept": "text/html,application/xhtml+xml",
                    "referer": "https://grok.com/",
                }
                headers.update(self._cookie_headers())
                resp = self.session.get(
                    f"{SITE_URL}/sign-up?redirect=grok-com",
                    headers=headers,
                )
            except Exception as exc:
                return 0, f"{type(exc).__name__}: {exc}", False
            status = int(getattr(resp, "status_code", 0) or 0)
            text = ""
            try:
                text = resp.text or ""
            except Exception:
                text = ""
            headers_resp = {str(k): str(v) for k, v in dict(resp.headers or {}).items()}
            cf = is_cloudflare_body(status, text, headers_resp)
        if cf and retry_on_cf:
            self.refresh_clearance(force=True)
            return self.fetch_signup_html(retry_on_cf=False)
        _ = t0
        return status, text, cf

    def scrape_signup_config(self) -> SignupConfig:
        """Scrape SITE_KEY / ACTION_ID / STATE_TREE (lite-style parallel JS search).

        ACTION_ID is deployment-specific 42-hex from the sign-up action chunk
        (near ``createUserAndSessionRequest``). Returns partial if incomplete.

        JS chunks are fetched with short-lived Sessions in a thread pool so we
        don't serialize ~40 GETs on the main client RLock (~30s → few seconds).
        """
        status, html, cf = self.fetch_signup_html()
        cfg = SignupConfig(source=f"http status={status} cf={cf} imp={self.impersonate}")
        if cf or status != 200 or not html:
            cfg.source += " (blocked_or_empty)"
            return cfg

        m = re.search(r"0x4AAAAAAA[a-zA-Z0-9_-]+", html)
        if m:
            cfg.site_key = m.group(0)

        # Router state tree from RSC flight segments (lite-compatible).
        cfg.state_tree = _scrape_router_state_tree(html)

        # ACTION_ID: parallel-fetch JS chunks; prefer signup-action keywords.
        js_urls = list(
            dict.fromkeys(re.findall(r'src="(/_next/static/[^"]+\.js)"', html))
        )
        priority_pats = (
            r"createUser",
            r"06rqcsyrqa6v",
            r"0ewiyh8jhugm9",
            r"125d~",  # live signup action chunk prefix (2026-07)
        )
        priority: list[str] = []
        rest: list[str] = []
        for url in js_urls:
            if any(re.search(p, url) for p in priority_pats):
                priority.append(url)
            else:
                rest.append(url)
        ordered = (priority + rest)[:60]

        signup_hash = ""
        fallback_hash = ""

        # Snapshot cookie/UA for worker Sessions (avoid holding self._lock).
        cookie_hdr = ""
        with self._lock:
            try:
                cookie_hdr = str(self.session.headers.get("cookie") or "")
            except Exception:
                cookie_hdr = ""
        ua = self.user_agent
        imp = self.impersonate
        proxy = self.proxy
        cf_requests = self._requests

        def _fetch_and_search(path: str) -> tuple[str, bool]:
            try:
                kwargs: dict[str, Any] = {
                    "impersonate": imp,
                    "verify": False,
                    "timeout": 20.0,
                }
                if proxy:
                    kwargs["proxy"] = proxy
                sess = cf_requests.Session(**kwargs)
                try:
                    headers = {
                        "referer": f"{SITE_URL}/sign-up",
                        "accept": "*/*",
                        "user-agent": ua,
                    }
                    if cookie_hdr:
                        headers["cookie"] = cookie_hdr
                    resp = sess.get(f"{SITE_URL}{path}", headers=headers)
                    js = resp.text or ""
                finally:
                    try:
                        sess.close()
                    except Exception:
                        pass
            except Exception:
                return "", False
            keywords = (
                "createUserAndSessionRequest",
                "emailValidationCode",
            )
            is_signup = any(kw in js for kw in keywords)
            # Lite: prefer quoted 42-hex (action id format).
            quoted = re.findall(r'"([a-f0-9]{42})"', js)
            if is_signup and quoted:
                return quoted[0], True
            if is_signup:
                hexes = re.findall(r"[a-fA-F0-9]{42}", js) or re.findall(
                    r"[a-fA-F0-9]{40,44}", js
                )
                if hexes:
                    return hexes[0], True
            if quoted and not is_signup:
                return quoted[0], False
            return "", False

        if ordered:
            workers = min(12, max(1, len(ordered)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(_fetch_and_search, u): u for u in ordered}
                for fut in as_completed(futures):
                    try:
                        h, is_signup = fut.result()
                    except Exception:
                        continue
                    if not h:
                        continue
                    if is_signup and not signup_hash:
                        signup_hash = h
                        # Cancel remaining — we have the signup chunk.
                        for other in futures:
                            other.cancel()
                        break
                    if not fallback_hash:
                        fallback_hash = h

        cfg.action_id = signup_hash or fallback_hash
        if cfg.action_id:
            cfg.source += f" action={'signup' if signup_hash else 'fallback'}"
        return cfg

    def server_action_register(
        self,
        *,
        email: str,
        password: str,
        code: str,
        turnstile_token: str,
        action_id: str,
        state_tree: str,
        given_name: str = "",
        family_name: str = "",
        castle_request_token: str = "",
        conversion_id: str | None = None,
        retry_on_cf: bool = True,
        create_session_fallback: bool = True,
    ) -> SignupResult:
        """Next.js Server Action createUser (lite body shape).

        Body is a 2-element array:
          [ { emailValidationCode, createUserAndSessionRequest, turnstileToken,
              conversionId, castleRequestToken },
            { client:"$T", meta:"$undefined", mutationKey:"$undefined" } ]

        Optional CreateSession password fallback when RSC has no sso chain.
        """
        given_name = given_name or random.choice(
            ["James", "John", "Robert", "Michael", "William", "David"]
        )
        family_name = family_name or random.choice(
            ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia"]
        )
        conv = (conversion_id or str(uuid.uuid4())).strip()
        # Server Action create-account payload shape (field order matters).
        payload_obj = [
            {
                "emailValidationCode": code,
                "createUserAndSessionRequest": {
                    "email": email,
                    "givenName": given_name,
                    "familyName": family_name,
                    "clearTextPassword": password,
                    "tosAcceptedVersion": "$undefined",
                },
                "turnstileToken": turnstile_token,
                "conversionId": conv,
                "castleRequestToken": castle_request_token or "",
            },
            {
                "client": "$T",
                "meta": "$undefined",
                "mutationKey": "$undefined",
            },
        ]
        payload = json.dumps(payload_obj, separators=(",", ":"))
        t0 = time.time()
        post_url = SIGNUP_URL_GROK  # redirect=grok-com matches state tree scrape
        with self._lock:
            headers = {
                "accept": "text/x-component",
                "content-type": "text/plain;charset=UTF-8",
                "next-router-state-tree": state_tree,
                "next-action": action_id,
                "origin": SITE_URL,
                "referer": post_url,
            }
            headers.update(self._cookie_headers())
            try:
                resp = self.session.post(post_url, data=payload, headers=headers)
            except Exception as exc:
                return SignupResult(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.time() - t0) * 1000),
                )

            status = int(getattr(resp, "status_code", 0) or 0)
            text = ""
            try:
                text = resp.text or ""
            except Exception:
                text = ""
            hdrs = {str(k): str(v) for k, v in dict(resp.headers or {}).items()}
            cf = is_cloudflare_body(status, text, hdrs)

            # Sometimes sso lands directly on Set-Cookie of the action response.
            direct_sso = _sso_from_response(resp)

        if cf and retry_on_cf:
            self.refresh_clearance(force=True)
            return self.server_action_register(
                email=email,
                password=password,
                code=code,
                turnstile_token=turnstile_token,
                action_id=action_id,
                state_tree=state_tree,
                given_name=given_name,
                family_name=family_name,
                castle_request_token=castle_request_token,
                conversion_id=conv,
                retry_on_cf=False,
                create_session_fallback=create_session_fallback,
            )

        markers = _signup_markers(text)
        hard_err = _signup_hard_error(text)
        if hard_err and "rate_limited" not in markers:
            markers = f"{markers},{hard_err}" if markers != "unclassified" else hard_err

        set_url = _extract_set_cookie_url(text)
        hop_urls = _extract_all_set_cookie_urls(text)
        if set_url and set_url not in hop_urls:
            hop_urls.insert(0, set_url)
        # lite: expand JWT success_url → auth.grokusercontent.com hop
        hop_urls = _expand_sso_hop_urls(hop_urls)

        sso = direct_sso
        sso_source = "direct" if sso else ""
        if not sso:
            for hop in hop_urls:
                sso = self.follow_set_cookie(hop)
                if sso:
                    set_url = hop
                    sso_source = "set_cookie"
                    break
        if not sso:
            sso = _session_cookie_value(self.session, "sso")
            if sso:
                sso_source = "jar"
        if not sso:
            sso = _sso_from_text(text)
            if sso:
                sso_source = "body"

        # lite fallback: account may exist but RSC omitted sso chain → CreateSession
        if (
            not sso
            and create_session_fallback
            and status == 200
            and not cf
            and not hard_err
            and turnstile_token
        ):
            sess = self.create_session(
                email=email,
                password=password,
                turnstile_token=turnstile_token,
                castle_request_token=castle_request_token,
            )
            if sess.ok and sess.sso:
                sso = sess.sso
                sso_source = "create_session"
            elif not sso and sess.error:
                markers = f"{markers},cs:{sess.error[:40]}" if markers else sess.error[:60]

        ok = bool(sso)
        err = ""
        if not ok:
            err = (
                f"no_sso http={status} markers={markers} cf={cf} "
                f"set_url={'1' if set_url else '0'} hops={len(hop_urls)} "
                f"body={(text or '')[:160]!r}"
            )
        return SignupResult(
            ok=ok,
            http_status=status,
            sso=sso,
            set_cookie_url=set_url,
            body_preview=text[:500].replace("\n", " "),
            markers=markers,
            cf_blocked=cf,
            elapsed_ms=int((time.time() - t0) * 1000),
            error=err,
            sso_source=sso_source,
        )

    def create_session(
        self,
        *,
        email: str,
        password: str,
        turnstile_token: str,
        castle_request_token: str = "",
        retry_on_cf: bool = True,
    ) -> SignupResult:
        """AuthManagement/CreateSession → session JWT usable as sso cookie value."""
        body = connect_frame(
            encode_create_session_request(
                email,
                password,
                turnstile_token=turnstile_token,
                castle_request_token=castle_request_token,
            )
        )
        t0 = time.time()
        with self._lock:
            headers = self._connect_headers()
            headers["referer"] = f"{SITE_URL}/sign-in?redirect=grok-com"
            try:
                resp = self.session.post(
                    CONNECT_CREATE_SESSION, data=body, headers=headers
                )
            except Exception as exc:
                return SignupResult(
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_ms=int((time.time() - t0) * 1000),
                )
            status = int(getattr(resp, "status_code", 0) or 0)
            raw = b""
            try:
                raw = resp.content or b""
            except Exception:
                raw = b""
            text = ""
            try:
                text = resp.text or ""
            except Exception:
                text = raw[:400].decode("utf-8", "replace") if raw else ""
            hdrs = {str(k): str(v) for k, v in dict(resp.headers or {}).items()}
            cf = is_cloudflare_body(status, text, hdrs)
            direct = _sso_from_response(resp)

        if cf and retry_on_cf:
            self.refresh_clearance(force=True)
            return self.create_session(
                email=email,
                password=password,
                turnstile_token=turnstile_token,
                castle_request_token=castle_request_token,
                retry_on_cf=False,
            )

        grpc_status = hdrs.get("grpc-status") or hdrs.get("Grpc-Status") or ""
        if not grpc_status:
            m = re.search(r"grpc-status[:\s]+(\d+)", text or "", re.I)
            if m:
                grpc_status = m.group(1)

        sso = direct or _session_cookie_value(self.session, "sso")
        if not sso and raw:
            # Session JWT often lands as a printable string field in the protobuf.
            sso = _sso_jwt_from_bytes(raw)
        if not sso:
            sso = _sso_from_text(text)

        ok = bool(sso) and status == 200 and not cf and str(grpc_status) in {"0", ""}
        err = ""
        if not ok:
            err = f"create_session http={status} grpc={grpc_status} cf={cf}"
        return SignupResult(
            ok=ok,
            http_status=status,
            sso=sso,
            body_preview=(text or "")[:300].replace("\n", " "),
            markers=f"grpc={grpc_status}",
            cf_blocked=cf,
            elapsed_ms=int((time.time() - t0) * 1000),
            error=err,
            sso_source="create_session" if sso else "",
        )

    def follow_set_cookie(self, url: str) -> str | None:
        """Follow set-cookie hops (JWT success_url expansion). lite SSOExtractor.

        Manual redirect walk (allow_redirects=False): intermediate 303 Set-Cookie
        for ``sso`` on ``.grok.com`` is often dropped when curl auto-follows.
        """
        if not url:
            return None
        hops = _expand_sso_hop_urls([url])
        with self._lock:
            seen: set[str] = set()
            i = 0
            while i < len(hops) and i < 8:
                hop = hops[i]
                i += 1
                if not hop or hop in seen:
                    continue
                seen.add(hop)
                try:
                    headers = {
                        "accept": (
                            "text/html,application/xhtml+xml,application/xml;"
                            "q=0.9,*/*;q=0.8"
                        ),
                        "referer": f"{SITE_URL}/",
                        "sec-fetch-site": "cross-site",
                        "sec-fetch-mode": "navigate",
                        "sec-fetch-dest": "document",
                        "upgrade-insecure-requests": "1",
                    }
                    headers.update(self._cookie_headers())
                    resp = self.session.get(
                        hop,
                        headers=headers,
                        allow_redirects=False,
                    )
                    direct = _sso_from_response(resp)
                    if direct:
                        return direct
                    try:
                        body = resp.text or ""
                    except Exception:
                        body = ""
                    from_body = _sso_from_text(body)
                    if from_body:
                        return from_body
                    val = _session_cookie_value(self.session, "sso")
                    if val:
                        return val
                    try:
                        jar = dict(self.session.cookies)
                        if "sso" in jar and jar["sso"]:
                            return str(jar["sso"])
                    except Exception:
                        pass
                    # 3xx Location continues the chain (often after sso is set)
                    status = int(getattr(resp, "status_code", 0) or 0)
                    loc = ""
                    try:
                        loc = str(
                            resp.headers.get("location")
                            or resp.headers.get("Location")
                            or ""
                        )
                    except Exception:
                        loc = ""
                    if loc.startswith("/"):
                        # relative: prefer accounts origin; grokusercontent uses abs
                        if "grokusercontent" in hop:
                            loc = "https://auth.grokusercontent.com" + loc
                        else:
                            loc = SITE_URL + loc
                    if (
                        status in (301, 302, 303, 307, 308)
                        and loc.startswith("http")
                        and loc not in seen
                    ):
                        hops.append(loc)
                        # also expand any JWT embedded in Location
                        for extra in _expand_sso_hop_urls([loc]):
                            if extra not in seen and extra not in hops:
                                hops.append(extra)
                except Exception:
                    continue
            # final jar sweep
            val = _session_cookie_value(self.session, "sso")
            if val:
                return val
            try:
                jar = dict(self.session.cookies)
                if "sso" in jar and jar["sso"]:
                    return str(jar["sso"])
            except Exception:
                pass
            return None


class XAIHttpClientPool:
    """Pool of independent XAIHttpClient slots for parallel P/C.

    One Session+RLock per slot — workers check out a client, use it, check in.
    Avoids the single-client RLock bottleneck that serializes all HTTP legs.

    Construction is lazy: the first slot is built immediately (so impersonate/UA
    are known for logging); remaining slots are created on first acquire so
    startup does not pay N× Session init before config scrape.
    """

    def __init__(
        self,
        size: int | None = None,
        *,
        proxy: str | None = None,
        impersonate: str | None = None,
        user_agent: str | None = None,
    ):
        n = int(size if size is not None else DEFAULT_HTTP_POOL_SIZE)
        n = max(1, min(32, n))
        self._size = n
        self._proxy = proxy
        self._impersonate = impersonate
        self._user_agent = user_agent
        self._q: queue.Queue[XAIHttpClient] = queue.Queue(maxsize=n)
        self._all: list[XAIHttpClient] = []
        self._lock = threading.Lock()
        self._closed = False
        self._created = 0
        # Seed one client so impersonate/UA are resolvable for banners.
        first = self._new_client()
        self._all.append(first)
        self._q.put(first)
        self._created = 1
        # Share resolved impersonate with later slots (skip N probe loops).
        if not self._impersonate:
            self._impersonate = first.impersonate
        if not self._user_agent:
            self._user_agent = first.user_agent

    def _new_client(self) -> XAIHttpClient:
        return XAIHttpClient(
            proxy=self._proxy,
            impersonate=self._impersonate,
            user_agent=self._user_agent,
        )

    @property
    def size(self) -> int:
        return self._size

    @property
    def impersonate(self) -> str:
        if self._all:
            return self._all[0].impersonate
        return self._impersonate or ""

    @property
    def user_agent(self) -> str:
        if self._all:
            return self._all[0].user_agent
        return self._user_agent or ""

    def acquire(self, timeout: float | None = 30.0) -> XAIHttpClient:
        if self._closed:
            raise RuntimeError("XAIHttpClientPool is closed")
        # Grow pool lazily up to size when queue is empty.
        try:
            client = self._q.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._size and not self._closed:
                    client = self._new_client()
                    self._all.append(client)
                    self._created += 1
                else:
                    client = None
            if client is None:
                try:
                    client = self._q.get(timeout=timeout)
                except queue.Empty as exc:
                    raise TimeoutError(
                        f"HTTP client pool exhausted (size={self._size})"
                    ) from exc
        try:
            client.apply_clearance()
        except Exception:
            # Still return the slot — caller may retry / refresh.
            pass
        return client

    def release(self, client: XAIHttpClient) -> None:
        if client is None or self._closed:
            return
        try:
            self._q.put_nowait(client)
        except queue.Full:
            pass

    def apply_clearance_all(self, force: bool = False) -> None:
        for client in list(self._all):
            with suppress_exc():
                if force:
                    client.refresh_clearance(force=True)
                else:
                    client.apply_clearance()

    def scrape_signup_config(self) -> SignupConfig:
        client = self.acquire(timeout=60.0)
        try:
            return client.scrape_signup_config()
        finally:
            self.release(client)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        # Drain queue without blocking forever.
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        for client in list(self._all):
            with suppress_exc():
                client.close()
        self._all.clear()


class _SuppressExc:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True


def suppress_exc() -> _SuppressExc:
    return _SuppressExc()


def _normalize_rsc_text(rsc_body: str) -> str:
    if not rsc_body:
        return ""
    text = rsc_body
    for _ in range(3):
        nxt = (
            text.replace("\\u0026", "&")
            .replace("\\u003d", "=")
            .replace("\\u003f", "?")
            .replace("\\u002F", "/")
            .replace("\\u002f", "/")
            .replace("\\/", "/")
            .replace("\\\\/", "/")
            .replace("&amp;", "&")
            .replace("\\u0026amp;", "&")
        )
        if nxt == text:
            break
        text = nxt
    return text


def _scrape_router_state_tree(html: str) -> str:
    """Extract URL-encoded next-router-state-tree from RSC flight HTML."""
    if not html:
        return ""
    # Path 1: classic self.__next_f.push flight segments with sign-up tree.
    for chunk in re.findall(
        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL
    ):
        if "sign-up" not in chunk and "sign_up" not in chunk:
            continue
        decoded = chunk.replace('\\"', '"')
        f_match = re.search(r'"f":\[\[\[', decoded)
        if not f_match:
            continue
        f_start = f_match.start() + 5
        end_idx = decoded.find('"$undefined"', f_start)
        if end_idx < 0:
            continue
        raw_tree = decoded[f_start:end_idx].replace('\\\\"', '"').replace("\\", "")
        if raw_tree:
            return quote(raw_tree, safe="")
    # Path 2: already-encoded tree embedded in HTML attributes / flight.
    m = re.search(
        r'next-router-state-tree["\']?\s*[:=]\s*["\']([^"\']{20,})["\']',
        html,
        re.I,
    )
    if m:
        return m.group(1)
    # Path 3: percent-encoded tree blob near sign-up.
    m = re.search(
        r'(%5B%22[^"\s]{40,}%5D)',
        html,
    )
    if m and "sign" in m.group(1).lower():
        return m.group(1)
    return ""


def _extract_all_set_cookie_urls(text: str) -> list[str]:
    """All candidate set-cookie hop URLs from an RSC body (lite-compatible)."""
    if not text:
        return []
    body = _normalize_rsc_text(text)
    found: list[str] = []
    for m in re.finditer(
        r'https?://[^\s"\'<>\\]+set-cookie/?\?q='
        r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+",
        body,
        flags=re.IGNORECASE,
    ):
        url = m.group(0)
        if url not in found:
            found.append(url)
    # Relative paths only (not //host or https://host).
    for m in re.finditer(
        r"(?<![/:])(/[A-Za-z0-9_./-]*set-cookie/?\?q="
        r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)",
        body,
        flags=re.IGNORECASE,
    ):
        url = "https://accounts.x.ai" + m.group(1)
        if url not in found:
            found.append(url)
    # Fallback: reconstruct grokusercontent hop from bare JWT near set-cookie.
    if not found:
        m = re.search(
            r"set-cookie[^e]{0,80}(eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)",
            body,
            flags=re.IGNORECASE,
        )
        if m:
            found.append(
                f"https://auth.grokusercontent.com/set-cookie?q={m.group(1)}"
            )
    return found


def _signup_hard_error(text: str) -> str:
    """Detect hard signup failure markers that should not trigger CreateSession.

    Never fire on success-shaped RSC ($Sreact.fragment / set-cookie hop):
    JS chunk names embed bare codes like ``invalid_code`` / ``rate_limited``.
    """
    if not text:
        return ""
    if _signup_looks_success(text):
        return ""
    lowered = text.lower()
    # Prefer framed / WKE-style codes over bare substrings.
    code_patterns = (
        ("email_in_use", r"\b(email_already_in_use|user_already_exists|email_in_use)\b"),
        ("email_domain", r"\b(account_email_domain_rejected|form_invalid_disposable_email)\b"),
        ("invalid_code", r"\b(invalid_verification_code|invalid-validation-code)\b"),
        ("action_digest", r"failed to find server action|invalid server action"),
        ("tos", r"\btos_not_accepted\b"),
    )
    for name, pat in code_patterns:
        if re.search(pat, lowered):
            return name
    phrase = (
        ("email_in_use", ("email already", "already registered")),
        ("email_domain", ("disposable email", "blocked domain")),
        ("invalid_code", ("code expired",)),
        ("tos", ("terms of service",)),
    )
    for name, needles in phrase:
        if any(n in lowered for n in needles):
            return name
    return ""


def _sso_from_text(text: str) -> str | None:
    """Extract raw sso=JWT embedded in HTML/RSC/body text."""
    if not text:
        return None
    body = _normalize_rsc_text(text)
    m = re.search(
        r'(?:^|[;,\s\'"\\])sso=(eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)',
        body,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if m:
        return m.group(1)
    # bare JWT near session/sso markers
    m = re.search(
        r'(?:sso|session)[^e]{0,40}(eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)',
        body,
        flags=re.IGNORECASE,
    )
    return m.group(1) if m else None


def _sso_jwt_from_bytes(raw: bytes) -> str | None:
    """Pull a printable JWT from protobuf / binary response bytes."""
    if not raw:
        return None
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        text = ""
    hit = _sso_from_text(text)
    if hit:
        return hit
    # Scan for eyJ... JWT ascii runs in binary.
    m = re.search(
        rb"(eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})",
        raw,
    )
    if not m:
        return None
    try:
        return m.group(1).decode("ascii")
    except Exception:
        return None


def _sso_from_set_cookie_header(header_val: str) -> str | None:
    """Parse a single Set-Cookie header value for name=sso."""
    if not header_val:
        return None
    # "sso=...; Path=/; ..." or just "sso=..."
    first = header_val.split(";", 1)[0].strip()
    if first.lower().startswith("sso="):
        return first[4:] or None
    return None


def _sso_from_response(resp) -> str | None:
    """Pull sso from response Set-Cookie headers or cookie jar side-effects."""
    try:
        # curl_cffi / requests: headers can be multi-valued
        raw = None
        try:
            raw = resp.headers.get("set-cookie") or resp.headers.get("Set-Cookie")
        except Exception:
            raw = None
        if raw:
            # may be one string with commas (ambiguous) — also try get_list
            candidates = [raw]
            try:
                get_list = getattr(resp.headers, "get_list", None) or getattr(
                    resp.headers, "getlist", None
                )
                if callable(get_list):
                    candidates = list(get_list("set-cookie") or get_list("Set-Cookie") or candidates)
            except Exception:
                pass
            for item in candidates:
                for part in re.split(r", (?=[A-Za-z0-9_\-]+=)", str(item)):
                    val = _sso_from_set_cookie_header(part)
                    if val and len(val) >= 20:
                        return val
    except Exception:
        pass
    return None


def _session_cookie_value(session, name: str) -> str | None:
    try:
        # curl_cffi Cookies supports get
        val = session.cookies.get(name)
        if val:
            return str(val)
    except Exception:
        pass
    try:
        for c in session.cookies.jar:
            if getattr(c, "name", "") == name:
                return str(c.value)
    except Exception:
        pass
    # iterate values if cookies yields names only
    try:
        for cookie in session.cookies:
            cname = getattr(cookie, "name", None)
            if cname is None and isinstance(cookie, str):
                # name-only iteration — look up
                if cookie == name:
                    v = session.cookies.get(cookie)
                    if v:
                        return str(v)
                continue
            if cname == name:
                return str(getattr(cookie, "value", "") or "")
    except Exception:
        pass
    return None


def _extract_set_cookie_url(text: str) -> str | None:
    """Primary set-cookie hop URL (first candidate from lite-style parse)."""
    urls = _extract_all_set_cookie_urls(text or "")
    if urls:
        return urls[0]
    # Legacy fallbacks for non-JWT q= payloads
    body = (text or "").replace("\\/", "/")
    m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[^:" \s\\]+)1:', body)
    if m:
        return m.group(1)
    m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[A-Za-z0-9_.\-]+)', body)
    if m:
        return m.group(1)
    return None


def _jwt_payload(jwt: str) -> dict[str, Any] | None:
    """Decode JWT payload segment (no sig verify). Used for SSO success_url hop."""
    try:
        parts = (jwt or "").split(".")
        if len(parts) < 2:
            return None
        raw = parts[1]
        raw += "=" * (4 - len(raw) % 4)
        import base64

        return json.loads(base64.urlsafe_b64decode(raw))
    except Exception:
        return None


def _jwt_from_set_cookie_url(url: str) -> str | None:
    from urllib.parse import unquote

    raw = unquote(url or "")
    m = re.search(
        r"[?&]q=(eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)",
        raw,
    )
    return m.group(1) if m else None


def _expand_sso_hop_urls(urls: list[str]) -> list[str]:
    """lite SSOExtractor: expand each hop with JWT config.success_url (grokusercontent)."""
    expanded: list[str] = []
    for url in urls or []:
        if url and url not in expanded:
            expanded.append(url)
        jwt = _jwt_from_set_cookie_url(url or "")
        if not jwt:
            continue
        payload = _jwt_payload(jwt)
        success = None
        if isinstance(payload, dict):
            cfg = payload.get("config")
            if isinstance(cfg, dict):
                success = cfg.get("success_url")
            # some flights put success_url at top level
            if not success:
                success = payload.get("success_url")
        if isinstance(success, str) and success.startswith("https://"):
            if success not in expanded:
                expanded.append(success)
            # always keep a grokusercontent q= form as alternate hop
            if "set-cookie" in success and "q=" not in success:
                alt = f"{success.rstrip('/')}?q={jwt}"
                if alt not in expanded:
                    expanded.append(alt)
        # hard fallback hop host (still functional 2026-06+) — always try
        fallback = f"https://auth.grokusercontent.com/set-cookie?q={jwt}"
        if fallback not in expanded:
            expanded.append(fallback)
    return expanded


def _signup_looks_success(text: str) -> bool:
    """True when RSC looks like a normal createUser flight (not an error page)."""
    if not text:
        return False
    # Explicit Next.js error flight
    if re.search(r"(?:^|\n)0:E\{", text):
        return False
    if "$Sreact.fragment" in text or '2:"$Sreact.fragment"' in text:
        return True
    if _extract_all_set_cookie_urls(text) or _extract_set_cookie_url(text):
        return True
    return False


def _signup_markers(text: str) -> str:
    """Classify signup RSC body — strict needles only (lite extract_signup_error).

    Success-shaped RSC ($Sreact.fragment + set-cookie hop) must NEVER be marked
    rate_limited: chunk source often embeds the string ``rate_limited`` as a
    client-side error code and used to false-trip the C circuit.
    """
    if not text:
        return "empty"
    lowered = text.lower()
    looks_ok = _signup_looks_success(text)
    hits: list[str] = []

    # Only trust rate_limited when framed as a real error — never on success flights.
    if not looks_ok:
        if re.search(r"\brate_limited\b", lowered) or any(
            n in lowered
            for n in (
                "too many requests",
                "rate limited",
                "rate limit exceeded",
                "try again later",
            )
        ):
            hits.append("rate_limited")

    # On success flights skip error-code tagging entirely (JS chunk pollution).
    if not looks_ok:
        code_patterns = (
            ("turnstile_failed", r"\b(turnstile_failed)\b"),
            ("email_already_in_use", r"\b(email_already_in_use|user_already_exists)\b"),
            ("email_domain", r"\b(account_email_domain_rejected|form_invalid_disposable_email)\b"),
            ("invalid_code", r"\b(invalid_verification_code|invalid-validation-code)\b"),
            ("account_signup_error", r"\b(account_signup_error)\b"),
        )
        for name, pat in code_patterns:
            if re.search(pat, lowered):
                hits.append(name)

        phrase_groups = {
            "challenge": ("cf-chl", "challenge-platform", "just a moment"),
            "email": ("email already", "already registered", "disposable email"),
            "action_error": ("failed to find server action", "invalid server action"),
        }
        for name, needles in phrase_groups.items():
            if name in hits:
                continue
            if any(n in lowered for n in needles):
                hits.append(name)

    if looks_ok and not hits:
        return "success_flight"
    if looks_ok and hits:
        # Prefer success_flight tag first for diagnostics.
        return "success_flight," + ",".join(hits)
    return ",".join(hits) if hits else "unclassified"


def probe_clearance_and_create(
    email: str = "probe@example.com",
    *,
    castle_token: str = "",
    force_prewarm: bool = True,
) -> dict[str, Any]:
    """Operator probe: prewarm → HTTP CreateEmailValidationCode (no browser)."""
    prewarm = prewarm_clearance(force=force_prewarm)
    client = XAIHttpClient()
    try:
        bundle = client.apply_clearance()
        create = client.create_email_code(email, castle_token=castle_token)
        cfg = client.scrape_signup_config()
        return {
            "prewarm": {
                "ok": prewarm.get("ok"),
                "cookies": prewarm.get("cookies"),
                "hosts": prewarm.get("hosts"),
                "errors": prewarm.get("errors"),
                "user_agent": (prewarm.get("user_agent") or "")[:80],
                "register_proxy": prewarm.get("register_proxy"),
                "clearance_proxy": prewarm.get("clearance_proxy"),
            },
            "bundle": {
                "has_cf_clearance": bundle.get("has_cf_clearance"),
                "cookie_count": len(bundle.get("cookies") or []),
                "cookie_header_preview": (bundle.get("cookie_header_accounts") or "")[:120],
            },
            "create": {
                "ok": create.ok,
                "http_status": create.http_status,
                "grpc_status": create.grpc_status,
                "cf_blocked": create.cf_blocked,
                "elapsed_ms": create.elapsed_ms,
                "error": create.error,
                "body_preview": create.body_preview[:200],
                "headers": create.headers,
            },
            "config": {
                "site_key": cfg.site_key,
                "action_id": (cfg.action_id or "")[:16] + ("…" if cfg.action_id else ""),
                "state_tree_len": len(cfg.state_tree or ""),
                "source": cfg.source,
            },
            "impersonate": client.impersonate,
            "user_agent": (client.user_agent or "")[:80],
            "castle_sent": bool(castle_token),
            "email": email,
        }
    finally:
        client.close()
