# scripts/
> L2 | 父级: ../CLAUDE.md

成员清单
clean_history.sh: 清理历史归档与运行残留，不动现网号池时用于瘦身。
ensure_runtime.sh: 启动前运行时探测，保证依赖和浏览器环境可用。
export_registered_sessions.py: 通过 SSH/本地快照导出注册会话，屏蔽密码字段。
import-keys-cpa-ready-to-cliproxy.sh: 将 keys/cpa_ready OAuth 成品导入 CLIProxyAPI auths。
import_cpa_ready_to_sub2api.py: 将注册 SSO 转交 sub2api 兑换 OAuth，或将 keys/cpa_ready OAuth 成品转成 sub2api-data 导入。
probe_http_protocol.py: 注册协议探针，定位 HTTP 注册链路入口和响应形态。
push_keys_to_auth.sh: 将本机 SSO 源原子推送到远端认证机。

法则: 只放可提交工具；真实凭据留在 keys/ 运行时产物中。

[PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
