# Fix: PROTOCOL_HTTP pool SSO cross-email reuse (2026-07-17)

## Symptom
With `PROTOCOL_HTTP=1`, `keys/auth-sessions.jsonl` / `accounts.txt` / `grok.txt`
grew hundreds of emails sharing a handful of real `session_id` JWTs. Downstream
enroller/device-flow then burned on polluted SSO; "auth success" count was fake.

## Root cause
`XAIHttpClientPool` reused curl_cffi Sessions without clearing `sso` / `sso-rw`.
`server_action_register` treated leftover jar cookies as success (`sso_source=jar`).

## Fix
1. `grok_register/http_protocol.py`
   - `clear_auth_cookies()` / `_purge_auth_cookies` (keep CF clearance)
   - clear on: register/create_session entry, apply_clearance, pool acquire+release
   - same-request jar SSO labeled `sso_source=session` (safe after pre-clear)
   - never emit legacy `sso_source=jar` as success without isolation
2. `grok_register/register.py`
   - reject `sso_source=jar` at HTTP signup gate
   - `_persist_registration` dedupes by JWT `session_id` (memory + disk)
3. `tests/test_http_sso_isolation.py` — 7 regression cases

## Ops note
Restart register / auth-service after upgrade. Existing polluted key files are
NOT auto-cleaned; collapse by unique `session_id` before re-import if needed.
