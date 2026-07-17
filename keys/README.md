# keys/

运行时出货目录。默认被 `.gitignore` 忽略下列产物，**不要**把真实账号/token 提交进仓库：

| 文件 / 目录 | 说明 |
|-------------|------|
| `accounts.txt` | `email:password:sso` |
| `grok.txt` | 每行一个 SSO JWT |
| `auth-sessions.jsonl` | 注册会话快照 |
| `acc.md` | 探活通过后的 access_token 列表 |
| `cpa_ready/` | watchdog 整备后的 CPA json + `_state.tsv` |
| `authenticated/` | 一般不在这里；见 `~/Downloads/grok-free-register-auth/` |

本目录随仓库发布的脚本：

- `acpa_watchdog.py` — 出货目录 → `cpa_ready/` 整备 + 探活
- `sync_acc.py` — 仅把 alive 号的 access_token 写入 `acc.md`
- `async_auth.sh` — 同时前台跑上面两个

```bash
# auth-service 出货后
bash keys/async_auth.sh
# 或
python3 keys/acpa_watchdog.py
python3 keys/sync_acc.py
```
