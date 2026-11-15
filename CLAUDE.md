# grok-free-register - x.ai 注册与认证流水线
Python + asyncio + Playwright + curl_cffi/httpx + FlareSolverr 清障栈

<directory>
grok_register/ - 注册核心、清障注入、HTTP 协议与邮箱服务 (1子目录: core)
xai_enroller/ - OAuth device-flow 认证、账本、执行器与出货服务
clearance/ - WARP + Privoxy + FlareSolverr 容器清障栈
keys/ - 运行时账号、会话、认证整备脚本与本地库存
scripts/ - 环境探测、会话导出、历史清理与运维入口
tests/ - pytest/unittest 回归测试，守住协议、库存、认证与入口脚本边界
docs/ - 面向维护者和使用者的架构、注册、清障、认证与排障说明
</directory>

<config>
.env.example - 运行配置模板，声明邮箱、代理、清障、容量和认证变量
requirements.txt - Python 运行依赖入口
start.sh - 注册 CLI 启动与首次配置入口
auth-service.sh - OAuth 认证服务启动入口
reset_pipeline.sh - 注册到认证到 CPA 的运行时数据清零入口
</config>

架构决策: 启动期先预热 clearance，再抓取 sign-up 配置；清障预热失败立即停止，页面仍是 Cloudflare challenge 时视为配置/出口错误，而非注册协议错误。

变更日志:
- 2026-08-03: 将 sign-up 配置抓取失败提升为可读配置错误，补清障诊断提示。
- 2026-08-03: 清障预热失败时在浏览器启动前快速失败，避免继续使用失效代理。

法则: 极简·稳定·导航·版本精确
