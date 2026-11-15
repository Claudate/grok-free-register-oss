# 运行状态与排障

别人那边挂、你这边正常时：**先在出问题的那台机器上按本文自查**，不要先改代码。
多数 `needs_interaction` / 队列卡住 = **清障 env 或 FlareSolverr 不可达**，或 **设备授权限流**，不是注册协议坏了。

相关文档：[清障栈](clearance.md) · [认证服务](auth-service.md) · [注册](registration.md)

---

## 0. 30 秒自检（出问题的机器上跑）

在项目根目录：

```bash
# 1) .env 是否存在且关键项非空
test -f .env && echo ".env ok" || echo "缺 .env → cp .env.example .env 后按 clearance.md 填"
grep -E '^(CLEARANCE_ENABLED|FLARESOLVERR_URL|CLEARANCE_URLS|CLEARANCE_PROXY|REGISTER_PROXY)=' .env || true

# 2) FlareSolverr 通不通（地址以 .env 的 FLARESOLVERR_URL 为准）
curl -sS -m 5 -X POST "${FLARESOLVERR_URL:-http://127.0.0.1:8191}/v1" \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"sessions.list"}'
# 期望 JSON: "status":"ok"

# 3) 清障预热（启动 auth/register 前必过）
bash clearance/prewarm.sh --direct
# 或与 compose 同网走 WARP：
# CLEARANCE_PROXY=http://privoxy:8118 bash clearance/prewarm.sh
```

预热日志里：

| 现象 | 含义 |
|---|---|
| `accounts.x.ai` / `x.ai` 为 `cf+` 或 `cf_clearance=yes` | 清障正常 |
| 全部失败 / `CLEARANCE_ENABLED=0` | **清障链路未启用或 FS 挂** → 见 §2 |
| 仅 `auth.x.ai` 为 `cf-` 但其它 `cf+` | **正常**，靠 `.x.ai` 父域 cookie |
| `Connection refused` 到 8191 | FS 没起或 `FLARESOLVERR_URL` 写错 |

---

## 1. 普通模式日志符号

| 前缀 | 含义 |
|---|---|
| `[→]` | 新任务开始 |
| `[✓]` | 成功或来源连接完成 |
| `[✗]` | 当前任务失败或跳过 |
| `[⏸]` | 限流冷却或服务暂停 |
| `[▶]` | 限流解除或服务恢复 |
| `[!]` | 来源 / 配置 / 流水线需关注 |

认证端输入 `s`：待处理数、阶段、平均速率、累计成功、可用/已取用、是否限流。

Debug：

```bash
bash start.sh --debug           # 注册：T/Q、物理并发、S/P/C
bash auth-service.sh --debug    # 认证：队列、重试、节拍、冷却、探针
```

---

## 2. 环境变量清单（认证卡死优先查这些）

`auth-service.sh` / `start.sh` 会 `load_dotenv(.env)`。
**注意：`ps e` / `/proc/<pid>/environ` 往往看不到 dotenv 注入的变量**——以 `.env` 文件 + 预热脚本结果为准，不要只看进程环境表。

### 2.1 清障（缺了就会大量 unknown_page / challenge_required）

| 变量 | 必填？ | 正确示例 | 常见错法 |
|---|---|---|---|
| `CLEARANCE_ENABLED` | 是 | `1` | 未写、写 `0`、只 export 没进 `.env` |
| `CLEARANCE_REQUIRED` | 否 | `0` | 写 `1` 后 FS 不通会在浏览器启动前直接退出 |
| `FLARESOLVERR_URL` | 是 | `http://127.0.0.1:8191` | 写成容器内网名却从宿主机调；端口未映射 |
| `CLEARANCE_URLS` | 建议 | 五条 x.ai 家族根（见 `.env.example`） | 空、带 path、少 `accounts.x.ai` |
| `CLEARANCE_PROXY` | 条件 | compose 内 `http://privoxy:8118` | 填了 `http://127.0.0.1:40080`（FS 容器内 127.0.0.1 是自己） |
| `REGISTER_PROXY` | 条件 | 宿主机 `http://127.0.0.1:40080` | 与 `CLEARANCE_PROXY` 混用同一写法；一边 WARP 一边直连 → cookie 失效 |

规则（背下来）：

1. **`REGISTER_PROXY`** = 本机 Playwright / httpx 出口 → **宿主机可达**地址。
2. **`CLEARANCE_PROXY`** = FlareSolverr **容器里**浏览器出口 → **compose 服务名**。
3. 注册/认证走 WARP 时，FS 必须同一条 WARP（`CLEARANCE_PROXY`），否则 `cf_clearance` 绑错出口。
4. 两边都直连时：两个 PROXY 都可注释掉；`bash clearance/prewarm.sh --direct`。

容器：

```bash
cd clearance && docker compose up -d && docker compose ps
# 不要为了腾内存 docker stop flaresolverr；只杀注册机 CloakBrowser 孤儿
```

详见 [clearance.md](clearance.md)。

### 2.2 认证来源（读不到号 / 一直等同步）

| 变量 | 作用 |
|---|---|
| `XAI_AUTH_SERVICE_SOURCE` | `auto` / `local` / `ssh` |
| `XAI_AUTH_SERVICE_SSH_HOST` | 设了且 `auto` → 走 SSH 拉远端会话 |
| `XAI_AUTH_SERVICE_REMOTE_ROOT` | 远端项目根 |
| `XAI_AUTH_SERVICE_SSH_IDENTITY` | 私钥路径；ssh-agent 可省 |
| `XAI_AUTH_SERVICE_REGISTER_ROOT` | `local` 时读哪份注册结果（默认本仓库） |
| `XAI_AUTH_SERVICE_SYNC_SEC` | 快照间隔，默认 30 |
| `XAI_AUTH_SERVICE_MIN_INTERVAL_SEC` | 两次授权最小间隔，默认 10；撞限可调到 20–30 |
| `XAI_AUTH_SERVICE_RETRY_SEC` | 限流后探测间隔，默认 60 |

同机：不要设 `XAI_AUTH_SERVICE_SSH_HOST`，或 `SOURCE=local`。
跨机：SSH 主机 + 远端已放 `scripts/export_registered_sessions.py`。见 [auth-service.md](auth-service.md)。

**不要和注册推送变量搞混：**

| 注册侧推 keys | 认证侧拉会话 |
|---|---|
| `AUTH_SSH_HOST` / `AUTH_REMOTE_ROOT` | `XAI_AUTH_SERVICE_SSH_HOST` / `XAI_AUTH_SERVICE_REMOTE_ROOT` |

只配了一边 = 另一边仍读本地空目录。

### 2.3 一键打印「生效配置」（推荐贴到群里）

```bash
cd /path/to/grok-free-register
.venv/bin/python - <<'PY'
from pathlib import Path
from dotenv import load_dotenv
import os
load_dotenv(Path(".env"))
keys = [
    "CLEARANCE_ENABLED", "FLARESOLVERR_URL", "CLEARANCE_URLS",
    "CLEARANCE_PROXY", "REGISTER_PROXY",
    "XAI_AUTH_SERVICE_SOURCE", "XAI_AUTH_SERVICE_SSH_HOST",
    "XAI_AUTH_SERVICE_REMOTE_ROOT", "XAI_AUTH_SERVICE_REGISTER_ROOT",
    "XAI_AUTH_SERVICE_MIN_INTERVAL_SEC", "XAI_AUTH_SERVICE_RETRY_SEC",
]
for k in keys:
    v = os.environ.get(k, "")
    print(f"{k}={v if v else '(unset)'}")
from grok_register.clearance import prewarm_clearance, format_prewarm_log, clearance_enabled
print("clearance_enabled()", clearance_enabled())
print(format_prewarm_log(prewarm_clearance(force=True)))
PY
```

把输出（可打码主机名）发协助者；**不要贴 SSO / cookie / token**。

---

## 3. 认证 reason_code 对照表

日志形如：`needs_interaction (unknown_page)`、`rate_limited`。

| reason_code | 含义 | 优先动作 |
|---|---|---|
| `unknown_page` | 设备确认页 2.5s 内无法归类（白屏/中间页/布局变了/未过 CF） | §0 预热；确认 `CLEARANCE_ENABLED=1` 且 cookie 注入；`auth-service.sh --debug` 看 clearance 预热行 |
| `challenge_required` | 标题/正文命中 Cloudflare 墙 | 同上；检查 `CLEARANCE_PROXY` 与 `REGISTER_PROXY` 是否同一出口策略 |
| `rate_limited` | URL `?error=rate_limited` 或正文 too many requests | **不是清障丢了**；加大 `XAI_AUTH_SERVICE_MIN_INTERVAL_SEC`，等冷却，勿并发多开 auth-service |
| `browser_error` | Playwright 异常（浏览器挂、协议错） | 重装依赖/`ensure_runtime`；看本机是否有 headless chrome；少见为瞬时，会 transient 重试 |
| `confirmation_timeout` | 确认环超时 | 网络慢或页卡住；查代理与 FS；可稍后重试 |
| `mfa_required` | 页要求二次验证 | 该会话账号本身要 MFA，换会话源 |
| `device_verify_failed` | verify URL 带其它 `error=` | 会话失效或设备流失败；检查 keys 是否完整 SSO |
| `device_flow_failed` | 设备码/协议层失败 | 网络、代理、账号状态；debug 看 transport |
| `unsafe_page` | 跳到非 `*.x.ai` | 代理劫持/重定向异常 |
| `imported` | 成功写入 authenticated | 正常 |
| `already_imported` | 指纹已入库 | 正常跳过 |

流水线对 `browser_error` / `confirmation_timeout` / `challenge_required` / `unknown_page` 会 **transient 重试**；`rate_limited` 走冷却闸，不会死刷。

---

## 4. 「别人卡在这里」决策树

```
auth 不出货 / 大量 needs_interaction
├─ prewarm 失败或 CLEARANCE_ENABLED 实际为 false？
│  └─ 修 .env + 起 clearance 容器 → 再 prewarm → 重启 auth-service
├─ prewarm 全 cf+ 仍 unknown_page？
│  ├─ 是否多开 auth 或间隔过短？（看 rate_limited 混杂）
│  ├─ REGISTER_PROXY 与 CLEARANCE 出口不一致？
│  └─ SSO 源是否空/过期？（keys 会话、SSH 同步是否成功）
├─ 几乎全是 rate_limited？
│  └─ 调大 MIN_INTERVAL；单实例跑；等 60s+ 冷却
└─ 配置错误打印了变量名？
   └─ 按提示查 auth-service.md / registration.md，无 traceback 属正常
```

维护者本机正常 **不能证明** 使用者机器：

- 使用者没起 `clearance/` 或端口冲突；
- 使用者复制了 README 但漏了 `CLEARANCE_ENABLED=1`；
- 使用者 `FLARESOLVERR_URL` 指向已停的旧容器；
- 使用者 `SOURCE=ssh` 但密钥/远端路径错，一直吃空快照；
- 使用者把 `CLEARANCE_PROXY=http://127.0.0.1:40080` 塞进 FS（容器内连自己）。

---

## 5. 限流与节奏

- 限流后 **不会** 持续重试打满。认证默认等约 60s，只放一个恢复探针；仍限流则再等。
- 注册端有全局冷却闸，避免并发漏过等待。
- 远端 SSH 源断开时：继续用上一份完整快照；恢复后自动同步，一般 **不必重启**。
- 设备授权侧 429 / `rate_limited` 与 CPA 探活 429（模型 capacity）是两回事：后者在 `acpa_watchdog` 会 discard 号，不归 auth reason 表。

建议：

```env
XAI_AUTH_SERVICE_MIN_INTERVAL_SEC=20
XAI_AUTH_SERVICE_RETRY_SEC=90
```

---

## 6. 配置错误与调试输出

配置错误会指出 **缺少或非法的配置名**，不输出 traceback。按提示检查：

- [注册配置](registration.md#配置邮箱)
- [认证同步配置](auth-service.md#配置远端同步)

需要队列 / 节拍 / 冷却探针细节时用 `--debug`。
协助排障时请提供：§2.3 脚本输出 + 最近十几行 auth 日志（打码邮箱可）+ `docker compose ps`（若用本仓 clearance）。

---

## 7. 健康时对照（便于说「我这边正常」）

本机健康时通常同时满足：

1. `clearance_enabled() is True`，预热 `accounts.x.ai`/`x.ai` 为 cf+；
2. `bash auth-service.sh` 启动行有 clearance 预热成功（或 debug 里 `clearance_prewarm` 非 skip）；
3. ledger / 终端有持续 `imported`，而不是单一 reason 刷屏；
4. `keys/` 或 `authenticated/` 在增长；
5. 仅偶发 `rate_limited` / `browser_error` 后自动恢复。

若维护者满足 1–5、使用者卡在 `unknown_page`：**让使用者跑 §0 + §2.3**，对照哪一列 env 为空或 FS 不通，而不是先怀疑协议代码。
