# 运行状态与排障

## 普通模式

普通模式的每一行都对应一次状态变化：

- `[→]`：新任务开始；
- `[✓]`：任务成功或来源连接完成；
- `[✗]`：当前任务失败或被跳过；
- `[⏸]`：进入限流冷却或服务暂停；
- `[▶]`：限流解除或服务恢复；
- `[!]`：来源、配置或流水线出现需要关注的问题。

认证端输入 `s` 可查看运行状态、待处理数量、当前阶段、本次运行平均速率、累计成功、可用和已取用凭据，以及是否处于限流。

## Debug 模式

注册端：

```bash
bash start.sh --debug
```

认证端：

```bash
bash auth-service.sh --debug
```

注册 Debug 面板包含 T/Q 库存、物理并发、S/P/C 阶段耗时和 token 求解时间。认证 Debug 状态包含 source/prepared/completion 队列、重试、授权节拍、冷却、单次探针和近五分钟滚动速率。

## 常见状态

限流后不会持续重试。认证端默认等待 60 秒，再只放行一个恢复探针；探针仍被限流时重新等待。注册端同样通过全局冷却闸门阻止并发任务漏过等待周期。

远端来源暂时断开时，本地认证服务继续使用上一份完整有效快照。恢复连接后会自动同步，不需要重启。

配置错误会指出缺少或非法的配置名，不输出 traceback。按提示检查 [注册配置](registration.md#配置邮箱) 或 [认证同步配置](auth-service.md#配置远端同步)。

## Config fetch failed

启动阶段要从 `https://accounts.x.ai/sign-up` 抓取 `SITE_KEY` / `ACTION_ID` / `STATE_TREE`。任一缺失即报：

```text
RuntimeError: Config fetch failed
```

这不是邮箱模式错误，也不是安装失败。常见原因：

1. 机房 IP 被 Cloudflare 拦，注册页返回挑战页/空壳，解析失败
2. 未配置出口代理：`REGISTER_PROXY` 为空直连
3. 未启用清障：`CLEARANCE_ENABLED=0`，FlareSolverr 未起
4. 页面结构变化导致正则未命中（较少见）

排查：

```bash
bash start.sh --debug
# 看是否拿到 SITE_KEY / ACTION_ID / STATE_TREE，以及 RegisterProxy 值

# 本机能否打开注册页
curl -sS -I --max-time 20 https://accounts.x.ai/sign-up | head

# 推荐：先起 clearance 栈，再写代理
cd clearance && docker compose up -d
```

`.env` 示例：

```env
EMAIL_MODE=tempmail
REGISTER_PROXY=http://127.0.0.1:40080
CLEARANCE_ENABLED=1
FLARESOLVERR_URL=http://127.0.0.1:8191
CLEARANCE_PROXY=http://privoxy:8118
CLEARANCE_URLS=https://accounts.x.ai,https://x.ai,https://status.x.ai,https://console.x.ai,https://auth.x.ai
```

然后再：

```bash
bash start.sh --debug
```

[PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
