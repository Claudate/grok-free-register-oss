# Cloudflare 清障栈

本项目在 `clearance/` 自带独立容器栈（从常见 WARP+Privoxy+FlareSolverr 组合抽出，无业务 app 依赖）：

| 服务 | 容器名 | 宿主机端口 | 作用 |
|---|---|---|---|
| warp-proxy | `grok-clearance-warp` | `127.0.0.1:40000` | WARP SOCKS5 出口 |
| privoxy | `grok-clearance-privoxy` | `127.0.0.1:40080` | HTTP 代理 → WARP |
| flaresolverr | `grok-clearance-flaresolverr` | `127.0.0.1:8191` | 获取 `cf_clearance` |

完整说明见 [clearance/README.md](../../clearance/README.md)。

## 启动

```bash
cd clearance
docker compose up -d
docker compose ps
```

端口被占用时（例如机器上已有另一套 WARP/FS）：

- 直接复用已有 `127.0.0.1:8191` / `40080`，不必再起本栈；或
- 改 `WARP_SOCKS_PORT` / `PRIVOXY_PORT` / `FLARESOLVERR_PORT` 后重启。

**常驻。** 不要为了腾内存 `docker stop` FlareSolverr；只回收注册机侧 CloakBrowser 孤儿进程。

## 接入注册 / 认证

写入项目根 `.env`：

```env
REGISTER_PROXY=http://127.0.0.1:40080
CLEARANCE_ENABLED=1
FLARESOLVERR_URL=http://127.0.0.1:8191
CLEARANCE_PROXY=http://privoxy:8118
CLEARANCE_URLS=https://accounts.x.ai,https://x.ai,https://status.x.ai,https://console.x.ai,https://auth.x.ai
```

| 变量 | 归属 | 说明 |
|---|---|---|
| `REGISTER_PROXY` | 本进程 Playwright + httpx | 必须是**宿主机**可达地址 |
| `CLEARANCE_PROXY` | FS **容器内**浏览器 | 必须是 compose 网内名，如 `http://privoxy:8118`；勿填 `127.0.0.1` |
| `FLARESOLVERR_URL` | 本进程调 FS API | 默认 `http://127.0.0.1:8191` |
| `CLEARANCE_URLS` | 预热根域 | 默认五条 x.ai 家族根（无 path） |

Clearance cookie 与 mint 时的出口绑定。注册走 WARP 时，FS 也必须经 `CLEARANCE_PROXY` 走同一条 WARP，否则 cookie 无效。

`bash start.sh` / `bash auth-service.sh` 启动时会调用 `grok_register.clearance.prewarm_clearance`，并把缓存 cookie 注入 Playwright context。

## 单独探活

```bash
CLEARANCE_PROXY=http://privoxy:8118 bash clearance/prewarm.sh
# 或直连 FS 出口（仅当注册也直连时）
bash clearance/prewarm.sh --direct
```

多数根域应出现 `cf_clearance=yes`；`auth.x.ai` 有时 200 但无 clearance，属正常。

## 代码落点

- `clearance/docker-compose.yml` — 三容器
- `clearance/privoxy-warp.conf` — `forward-socks5t` → `warp-proxy:1080`
- `clearance/prewarm.sh` — 一键探活
- `grok_register/clearance.py` — 预热、缓存、代理 helpers（不启动容器）
