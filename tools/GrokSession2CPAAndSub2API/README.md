# Grok Session → CPA / sub2api

纯前端单页工具：把 grok-free-register 产出的凭证，转成 CPA、sub2api 导入包、batch 建号、或 sso-to-oauth 请求体。

对照思路：GPT session → CPA / sub2api 纯前端转换器（同类工具，无外部依赖）。

## 本地使用

直接打开：

```text
tools/GrokSession2CPAAndSub2API/index.html
```

所有解析和转换都在浏览器本地完成，不上传 token。

## 支持输入

| 来源 | 说明 |
|------|------|
| `authenticated/xai-*.json` | 注册成功 OAuth（base_url 常是 api.x.ai） |
| `cpa_ready/xai-*.json` | watchdog 终态（带 headers/email） |
| `sub2api-data` / accounts 数组 | 已转换过的包，可再导成其它格式 |
| `auth-sessions.jsonl` | `email` + `cookies.sso` |
| `accounts.txt` | `email:password:sso` |
| `grok.txt` | 每行一个 SSO JWT |

可粘贴 JSON / 数组 / jsonl，或拖入多个文件。

## 输出格式

### CPA

对齐注册机 cpa_ready：

- `type: "xai"`
- `access_token` / `refresh_token` / `id_token`
- `base_url` **强制** `https://cli-chat-proxy.grok.com/v1`（忽略 authenticated 里的 `api.x.ai`）
- `headers`：保留源 headers，否则写 grok-cli 默认头
- `email` / `sub` / `expired` / `expires_in`

单条输出对象，多条输出数组。

### sub2api 导入

`type: "sub2api-data"` 包，可走 sub2api 数据导入：

```json
{
  "type": "sub2api-data",
  "version": 1,
  "exported_at": "...",
  "proxies": [],
  "accounts": [ ... ]
}
```

账号字段：

- `platform: "grok"`, `type: "oauth"`
- `credentials.access_token` / `refresh_token` / `id_token` / `expires_at` / `email` / `sub` / `base_url`
- **有 `refresh_token`**：不写账号顶层 `expires_at` / `auto_pause_on_expired`（避免 access 6h 到期被 pause）
- **无 `refresh_token`**：写 access JWT exp + `auto_pause_on_expired: true`

### batch 建号

`POST /api/v1/admin/accounts/batch` 体：

```json
{ "accounts": [ /* 同上 sub2api 账号对象 */ ] }
```

### sso-to-oauth

`POST /api/v1/admin/grok/sso-to-oauth` 体：

```json
{
  "sso_tokens": ["eyJ...", "..."],
  "_meta": [ /* 仅本地对照，API 可忽略 */ ]
}
```

纯 SSO 输入（grok.txt / accounts.txt / auth-sessions）**只能**走这个格式；CPA / sub2api / batch 需要 OAuth。

## 关键规则（别踩坑）

1. free 号 API 走 `cli-chat-proxy.grok.com`，不是 `api.x.ai`。
2. free OAuth access ≈ 6h（`expires_in=21600`）。有 refresh 时让 sub2api 自己刷；别把账号级 `expires_at` 钉成 access exp。
3. 当前线上常见：refresh 被整批 revoke，access 在 exp 前仍可用。这种号导入后到期无法续命，只能重铸。
4. 注册机 `grok.txt` / `accounts.txt` / `auth-sessions.jsonl` 只有 SSO，没有 OAuth；要进 sub2api 要么走 sso-to-oauth，要么先走 xai_enroller 出 `authenticated/` 再转。

## 与注册机衔接

```
register → grok.txt / accounts.txt / auth-sessions.jsonl   (SSO)
        → xai_enroller → authenticated/xai-*.json          (OAuth)
        → acpa_watchdog → cpa_ready/xai-*.json + acc.md
        → 本工具 → sub2api-data / batch / CPA / sso-to-oauth
```
