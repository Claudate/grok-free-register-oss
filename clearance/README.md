# Clearance stack (WARP + Privoxy + FlareSolverr)

Standalone Cloudflare clearance containers for `grok-free-register`.
No application image — only the three services the register/auth process
calls from the host.

| Service | Container name | Host bind | Role |
|---|---|---|---|
| `warp-proxy` | `grok-clearance-warp` | `127.0.0.1:40000` | Cloudflare WARP SOCKS5 egress |
| `privoxy` | `grok-clearance-privoxy` | `127.0.0.1:40080` | HTTP proxy → WARP |
| `flaresolverr` | `grok-clearance-flaresolverr` | `127.0.0.1:8191` | Mint `cf_clearance` |

## Two egress paths (do not mix)

| Variable | Who uses it | Typical value |
|---|---|---|
| `REGISTER_PROXY` | grok-free-register process (Playwright + httpx) | `http://127.0.0.1:40080` |
| `CLEARANCE_PROXY` | FlareSolverr **browser inside the container** | `http://privoxy:8118` |
| `FLARESOLVERR_URL` | host → FS API | `http://127.0.0.1:8191` |

`CLEARANCE_PROXY` must resolve **from inside the FlareSolverr container**.
Use the compose service name `privoxy`, not host `127.0.0.1`.
Clearance cookies are only valid on the same egress they were minted on —
if register traffic goes through WARP, mint on WARP too.

## Start

```bash
cd clearance
docker compose up -d
docker compose ps
```

Optional env (compose project directory or shell):

```bash
# WARP_SOCKS_PORT=40000
# PRIVOXY_PORT=40080
# FLARESOLVERR_PORT=8191
# FLARESOLVERR_LOG_LEVEL=info
# TZ=Asia/Shanghai
# WARP_LICENSE_KEY=          # WARP+ optional
```

Port clash: if another stack already binds `40000` / `40080` / `8191`,
either reuse that stack and skip this one, or change the three `*_PORT`
values here.

**Keep the stack running.** Do not stop FlareSolverr to free memory —
reap orphan CloakBrowser processes from the register side instead.

## Wire into grok-free-register

In the project root `.env`:

```env
# host process egress → Privoxy → WARP
REGISTER_PROXY=http://127.0.0.1:40080

# call FS on host loopback
CLEARANCE_ENABLED=1
FLARESOLVERR_URL=http://127.0.0.1:8191

# FS browser egress (docker-network name of privoxy)
CLEARANCE_PROXY=http://privoxy:8118

# x.ai family roots (host-level, no path). Defaults match if omitted.
CLEARANCE_URLS=https://accounts.x.ai,https://x.ai,https://status.x.ai,https://console.x.ai,https://auth.x.ai
CLEARANCE_TIMEOUT_SEC=60
CLEARANCE_REFRESH_SEC=3000
```

Then:

```bash
# from project root
bash start.sh
# and/or
bash auth-service.sh
```

Register and auth both call `grok_register.clearance.prewarm_clearance`
on startup and inject cached cookies into Playwright contexts.

## Smoke-test prewarm (no register run)

```bash
# FS browser via WARP (recommended when REGISTER_PROXY also uses WARP)
CLEARANCE_PROXY=http://privoxy:8118 bash clearance/prewarm.sh

# FS direct egress (only if register is also direct)
bash clearance/prewarm.sh --direct
```

Expected: most of the five hosts return `cf_clearance=yes` within a few
seconds. `auth.x.ai` sometimes returns 200 without a clearance cookie;
that is normal.

## Logs / ops

```bash
cd clearance
docker compose logs -f flaresolverr
docker compose logs -f warp-proxy
docker compose restart flaresolverr   # if Chromium inside FS wedged
docker compose down                   # stop stack (avoid unless intentional)
```

## Layout

```text
clearance/
  docker-compose.yml    warp + privoxy + flaresolverr
  privoxy-warp.conf     forward-socks5t → warp-proxy:1080
  prewarm.sh            one-shot FS probe for CLEARANCE_URLS
  README.md             this file
```
