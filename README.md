# grok-free-register

`grok-free-register` 是一个命令行注册工具。程序会启动本机浏览器，完成页面操作、邮箱验证码处理和结果保存。

运行结果写入 `keys/` 目录。

## 快速开始

```bash
git clone <your-fork-or-mirror>/grok-free-register.git
cd grok-free-register
bash start.sh
```

首次运行会自动创建 `.venv`、安装依赖，并引导生成 `.env`。

需要完整说明时，按用途查看：

- [注册教程](docs/guides/registration.md)
- [CF 清障栈](docs/guides/clearance.md)
- [本地认证服务](docs/guides/auth-service.md)
- [凭据库存与取用](docs/guides/credential-inventory.md)
- [运行状态与排障](docs/guides/runtime-troubleshooting.md)

常用命令：

```bash
bash start.sh               # 按当前 .env 前台运行
bash start.sh --target 100  # 成功 100 个后停止
bash start.sh --max-mem 6G  # 自动估算并发时最多使用 6G 内存
bash start.sh --reconfig    # 重新选择邮箱模式
```

`start.sh` 直接在当前终端显示状态。按 `Ctrl-C` 停止，再次执行同一命令即可重启；不需要额外的会话管理或守护进程依赖。

S 默认本进程浏览器 mint Turnstile（开箱）。若外部已有 YesCaptcha 形 farm（如 lite `:5072`），只接线：

```env
TURNSTILE_PROVIDER=lite
LITE_SOLVER_URL=http://127.0.0.1:5072
```

本仓只做 createTask/getTaskResult 客户端，不内置 farm / Docker 镜像。

代理与 CF 清障是两套配置，不要混。本仓自带清障容器栈：

```bash
cd clearance && docker compose up -d   # WARP:40000 / Privoxy:40080 / FS:8191
# 可选探活：CLEARANCE_PROXY=http://privoxy:8118 bash clearance/prewarm.sh
```

```env
# 本进程出口（Playwright + httpx 显式挂载；Playwright 不会自己读 env）
REGISTER_PROXY=http://127.0.0.1:40080

# 清障预热：调 clearance/ 里的 FlareSolverr
CLEARANCE_ENABLED=1
FLARESOLVERR_URL=http://127.0.0.1:8191
CLEARANCE_URLS=https://accounts.x.ai,https://x.ai,https://status.x.ai,https://console.x.ai,https://auth.x.ai
# FS 容器内出口（compose 服务名）；与 REGISTER_PROXY 同走 WARP
CLEARANCE_PROXY=http://privoxy:8118
```

详见 [clearance/README.md](clearance/README.md)。容器宜常驻；腾内存只收尸注册机 CloakBrowser，不要 `docker stop` FlareSolverr。

## 邮箱模式

`tempmail` 是默认模式，不需要额外配置，适合快速试跑：

```env
EMAIL_MODE=tempmail
```

`custom` 是自建域名邮箱模式，适合长时间运行。需要一个已接入 Cloudflare Email Routing 的域名，并在运行机器上启动本项目的收信服务。

配置步骤：

1. 在 Cloudflare 为域名开启 Email Routing。
2. 部署 `cloudflare/email-worker.js`。
3. 在 Email Routing 中配置 catch-all，动作选择发送到该 Worker。
4. 在运行机器上启动收信服务：

```bash
bash start.sh --email-service
```

5. 在 `.env` 中配置：

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
```

如果 Worker 需要回调本机服务，`WEBHOOK_URL` 应使用可访问的域名地址。

## 配置

完整模板见 `.env.example`。日常使用通常只需要配置邮箱模式、代理、目标数量和内存预算。

| 配置 | 默认值 | 说明 |
|---|---:|---|
| `EMAIL_MODE` | `tempmail` | 邮箱模式，支持 `tempmail` 和 `custom` |
| `EMAIL_DOMAIN` | 空 | `custom` 模式使用的域名 |
| `EMAIL_API` | `http://127.0.0.1:8080` | 本地收信服务地址 |
| `TARGET` | `0` | 成功数量目标，`0` 表示不限 |
| `PHYSICAL_CAP` | `0` | 浏览器并发上限，`0` 表示启动时自动估算 |
| `PHYSICAL_PER_CPU` | `2` | 自动估算时每个 CPU 核心对应的并发参考值 |
| `PHYSICAL_MEM_MB` | `512` | 自动估算时每个浏览器任务的内存预算 |
| `MIN_FREE_MEM_MB` | `500` | 自动估算时保留的内存 |
| `T_SLOT_CAP` | `8` | token 缓冲容量 |
| `Q_SLOT_CAP` | `8` | 验证码缓冲容量 |
| `Q_PENDING_CAP` | `12` | 等待验证码返回的请求上限 |
| `SOLVER_MOUSE_CLICK_RETRIES` | `3` | token 验证框中心点击次数，`0` 表示关闭 |
| `PAGE_BLOCK_STATIC_ASSETS` | `0` | 可选：阻断部分静态资源，降低页面准备成本 |
| `C_HOT_PAGE_POOL` | `0` | 可选：复用消费阶段页面，减少页面重建开销 |

不确定怎么设置时，先保持默认值。性能压测时优先观察 `PHYSICAL_CAP` 和内存，不建议先改 Worker 数量。

## 运行日志

直接运行 `bash start.sh` 时，终端只输出任务开始、成功或失败、本次运行平均速度、累计数量和限流等待：

```text
[→] 开始注册 #38
[✓] 注册成功 #38 | 运行平均 47/分 | 累计 38
[⏸] 触发限流 | 60秒后恢复探测
[▶] 限流解除 | 实际等待 61秒
```

需要调试并发、库存和阶段耗时时，使用：

```bash
bash start.sh --debug
```

它会在上述任务事件之外，每 8 秒输出一次完整的 T/Q、物理并发和阶段耗时面板。已有自动化环境也可继续使用 `REGISTER_LOG_MODE=debug`。

常用字段：

| 字段 | 含义 |
|---|---|
| `T` | 当前可用 token 数量 |
| `Q` | 当前可用验证码数量 |
| `phys` | 空闲浏览器并发许可 |
| `s_phys` / `p_phys` / `c_phys` | S/P/C 获取浏览器许可的平均等待秒数 / 平均持有秒数 |
| `p_stage` | P 阶段平均耗时：建邮箱 / 准备页面 / 发送请求 |
| `c_stage` | C 阶段平均耗时：拿页面 / 验证码校验 / 注册提交 |
| `c_hot` | C 热页池命中 / 未命中次数 |
| `t_solve_avg` | 平均 token 获取时间 |
| `q_sent` / `q_ret` | 已发送 / 已收到的验证码数量 |
| `pair` | 已配对消费次数 |
| `ok` / `fail` | 成功 / 失败数量 |
| `rate` | 当前累计成功速率 |

简单判断：

- `T` 长期为 `0` 且 `Q` 有库存，通常是 token 获取较慢。
- `Q` 长期为 `0` 且 `T` 有库存，通常是邮箱或验证码链路较慢。
- `phys` 长期为 `0`，说明浏览器并发已经用满。
- `t_solve_avg` 明显升高，通常表示浏览器压力、网络质量或 token 服务响应变慢。

可以用日志分析工具解析已有日志：

```bash
python3 - <<'PY'
from pathlib import Path
from tools.runtime_log_analyzer import analyze_text
print(analyze_text(Path("run.log").read_text()))
PY
```

## 输出文件

成功结果写入：

```text
keys/accounts.txt
keys/grok.txt
```

`accounts.txt` 每行格式：

```text
email:password:sso_token
```

`keys/` 目录包含运行结果，默认不会提交到 Git。

认证成功后的 OAuth 出货默认在：

```text
~/Downloads/grok-free-register-auth/authenticated/
```

可用 `keys/async_auth.sh` 把出货整备成 `keys/cpa_ready/` 并同步探活通过的 `access_token` 到 `keys/acc.md`。

## 清零流水线（删库跑路）

只删 `keys/acc.md` / `accounts.txt` **不够**：`acpa_watchdog` 会从
`~/Downloads/grok-free-register-auth/authenticated/` 把旧号再整备进
`keys/cpa_ready/`，`sync_acc` 再写回 `acc.md`。状态文件 `_state.tsv` 也会残留幽灵 alive。

**全量清零**用项目根的 `reset_pipeline.sh`（默认 dry-run，必须加 `--yes` 才真删）：

```bash
# 建议先停 register / auth-service / async_auth
bash reset_pipeline.sh              # dry-run，只列将删内容
bash reset_pipeline.sh --yes        # 真删：注册源 + 认证出货 + cpa_ready + state
bash reset_pipeline.sh --yes --keep-register
                                    # 只清认证/CPA，保留 keys 里新注册的 SSO 源
bash reset_pipeline.sh --yes --keep-cpa
                                    # 只清认证账本/源快照，保留 cpa_ready（少用）
bash reset_pipeline.sh --yes --wipe-salt
                                    # 连 enrollment ledger salt 一并清（极少需要）
```

会清掉的大致范围：

| 范围 | 路径 |
|------|------|
| 注册源 | `keys/accounts.txt`、`keys/grok.txt`、`keys/auth-sessions.jsonl`、`keys/acc.md` |
| CPA | `keys/cpa_ready/xai-*.json`、`_state.tsv`、`_discarded/` |
| 认证出货 | `$XAI_AUTH_SERVICE_LOCAL_DIR`（默认 `~/Downloads/grok-free-register-auth`）下的 `authenticated/`、`claimed/`、`enrollment-ledger.db*`、`source-snapshot.jsonl` |

认证目录可用环境变量覆盖：

```bash
export XAI_AUTH_SERVICE_LOCAL_DIR=~/Downloads/grok-free-register-auth
# 或
export XAI_ENROLLER_LOCAL_AUTH_DIR=~/Downloads/grok-free-register-auth
```

**只清历史归档、不动现网号池**用 `scripts/clean_history.sh`：

```bash
bash scripts/clean_history.sh              # dry-run
bash scripts/clean_history.sh --yes        # 清 zip / _discarded / logs / 杂包
bash scripts/clean_history.sh --yes --deep # 再清 source-snapshot 等更深一层
```

清完后重开：

```bash
bash start.sh                  # 或远端注册机继续跑
bash auth-service.sh           # device-flow → authenticated/
bash keys/async_auth.sh        # acpa_watchdog + sync_acc
```

## 项目结构

```text
grok_register/              注册核心与 custom 邮箱服务
xai_enroller/               OAuth 认证服务
clearance/                  WARP + Privoxy + FlareSolverr 清障栈（docker compose）
keys/                       acpa_watchdog / sync_acc / async_auth（运行时产物 gitignore）
cloudflare/email-worker.js  Cloudflare Email Routing Worker 示例
start.sh                    首次配置和运行
auth-service.sh             认证服务入口
reset_pipeline.sh           全量清零注册→认证→CPA 运行时数据
scripts/clean_history.sh    只清历史归档，不动现网号池
scripts/push_keys_to_auth.sh  本机 SSO 源推远端 auth（需显式 AUTH_SSH_*）
setup.sh                    安装依赖
.env.example                配置模板
tools/                      日志分析 + GrokSession→CPA/sub2api 前端转换
tests/                      自动化测试
docs/architecture.md        并发架构说明
docs/guides/clearance.md    清障栈接入说明
```

## 测试

测试依赖与运行依赖分开安装：

```bash
.venv/bin/pip install -r tests/requirements.txt
```

快速检查：

```bash
python3 -m unittest tests.test_admission_gate tests.test_register_runtime_unittest tests.test_inventory_unittest tests.test_runtime_log_analyzer -v
```

完整测试：

```bash
python3 -m pytest tests -q
```

## xAI OAuth Enroller

`xai_enroller/` 用于把已有 xAI 账号的 SSO 会话转换为 OAuth 凭据，并导入 CPA
凭据库。它独立于注册流程运行：注册机不需要启动，已有账号也不需要重新注册。

### 默认同机模式

注册和认证在同一个项目目录运行时，无需配置来源：

```bash
bash auth-service.sh
```

认证服务默认读取本项目的 `keys/auth-sessions.jsonl` 和历史
`keys/accounts.txt`，每 30 秒生成一次经过校验的原子快照。注册仍可同时运行；认证服务
只读取完整记录，不会读取正在追加的半行。

### 分离设备的 SSH 模式

注册机和认证服务分开运行时，服务器继续写入注册结果，本地认证服务
每 30 秒通过一次性 SSH 导出全量 JSONL 快照。快照经逐行校验、`fsync` 后原子替换到
`~/Downloads/grok-free-register-auth/source-snapshot.jsonl`；同步失败会继续使用上一份有效快照。
认证使用本机 CloakBrowser Chromium，
成功结果写入 `~/Downloads/grok-free-register-auth/authenticated/`，运行状态、同步快照与
认证文件分开保存；认证文件格式可以直接供 CPA 使用。

先把导出器同步到注册机的 `scripts/` 目录：

```bash
scp scripts/export_registered_sessions.py user@your-server:/opt/grok-free-register/scripts/
```

然后在本地配置 SSH 连接：

下面这些变量既可在终端 `export`，也可写入由 `.env.example` 复制出的 `.env`；
`auth-service.sh` 会自动读取该文件。

```bash
export XAI_AUTH_SERVICE_SSH_HOST=user@your-server
export XAI_AUTH_SERVICE_SSH_IDENTITY=/path/to/ssh-key.pem  # 使用 ssh-agent 时可省略
export XAI_AUTH_SERVICE_REMOTE_ROOT=/opt/grok-free-register
export XAI_AUTH_SERVICE_SYNC_SEC=30
```

存在 `XAI_AUTH_SERVICE_SSH_HOST` 时会自动选择 SSH。也可显式设置
`XAI_AUTH_SERVICE_SOURCE=local|ssh`；默认值 `auto` 优先兼容已有 SSH 配置，否则使用同机来源。

启动认证服务：

```bash
bash auth-service.sh
```

需要查看队列、重试、节拍和冷却探针时使用：

```bash
bash auth-service.sh --debug
```

默认每次认证至少间隔 10 秒；实测该值比无间隔运行有更高的长期平均成功速率。限流后
每 60 秒只进行一次恢复探测。需要覆盖时使用：

```bash
export XAI_AUTH_SERVICE_MIN_INTERVAL_SEC=10
export XAI_AUTH_SERVICE_RETRY_SEC=60
```

终端只在账号开始、认证结果、限流状态或控制状态变化时输出，并在底部保持 `认证> ` 输入行。`s` 查看状态，`p` 暂停，
`r` 恢复，`c` 取消当前账号，`q` 退出；`Ctrl-C` 同样会退出。在输入行键入 `take 100`
会把最新的 100 个可用凭证登记为已取用，并移动到独立批次目录。认证记录仍保留为
`imported`，不会因为凭证被取出而重新认证。

可用凭证保存在：

```text
~/Downloads/grok-free-register-auth/authenticated/
```

已取用批次保存在：

```text
~/Downloads/grok-free-register-auth/claimed/<batch-id>/
```

库存状态保存在 `enrollment-ledger.db` 的 `credential_inventory` 表中，状态为
`available`、`claiming` 或 `claimed`。每条记录预留 `note` 字段，默认留空。

`auth-service.sh` 首次运行会自动安装项目依赖。正式用户流程只需要这个 Bash 入口；底层 Python 模块保留给开发和测试，不作为另一套使用方式。

## 开发文档

[docs/architecture.md](docs/architecture.md) 记录并发模型、资源生命周期和必须保持的不变量。

## License

MIT
