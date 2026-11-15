# 注册教程

## 开始运行

```bash
git clone <your-fork-or-mirror>/grok-free-register.git
cd grok-free-register
bash start.sh
```

首次运行会安装 Python、CloakBrowser Chromium 及其系统依赖，然后引导选择邮箱模式。以后再次执行 `bash start.sh` 会直接使用已有配置。

普通模式只显示服务启动、任务开始、注册成功或失败、本次运行平均速率、累计数量和限流状态。查看完整并发、库存和阶段耗时时使用：

```bash
bash start.sh --debug
```

常用参数：

```bash
bash start.sh --target 100
bash start.sh --max-mem 6G
bash start.sh --reconfig
```

未设置 `--target` 时服务持续运行，按 `Ctrl-C` 安全停止。
再次执行 `bash start.sh` 即可重启。程序直接使用当前终端，不需要额外的会话管理工具。

## 配置邮箱

临时邮箱无需额外配置：

```env
EMAIL_MODE=tempmail
```

自建邮箱需要可接收邮件的域名和本项目的收信服务：

```env
EMAIL_MODE=custom
EMAIL_DOMAIN=example.com
EMAIL_API=http://127.0.0.1:8080
```

自建模式还需运行：

```bash
bash start.sh --email-service
```

性能参数默认会根据 CPU 和可用内存估算。除非正在压测，否则保持 `.env.example` 中的默认值即可。

## Cloudflare 清障（可选但推荐）

x.ai 家族页面前有 CF。本仓 `clearance/` 提供 WARP + Privoxy + FlareSolverr：

```bash
cd clearance && docker compose up -d
```

根目录 `.env`：

```env
REGISTER_PROXY=http://127.0.0.1:40080
CLEARANCE_ENABLED=1
FLARESOLVERR_URL=http://127.0.0.1:8191
CLEARANCE_PROXY=http://privoxy:8118
CLEARANCE_URLS=https://accounts.x.ai,https://x.ai,https://status.x.ai,https://console.x.ai,https://auth.x.ai
```

`REGISTER_PROXY` 是本进程出口；`CLEARANCE_PROXY` 是 FS 容器内浏览器出口（compose 服务名，不要填宿主机 `127.0.0.1`）。详见 [clearance.md](clearance.md) 与 [clearance/README.md](../../clearance/README.md)。

成功结果写入 `keys/accounts.txt`、`keys/grok.txt` 和 `keys/auth-sessions.jsonl`；这些文件默认不提交到 Git。
