# grok_register/
> L2 | 父级: ../CLAUDE.md

成员清单
__init__.py: 包标识，无运行逻辑。
register.py: 注册流水线入口，负责 clearance 预热、sign-up 配置抓取、S/P/C worker 编排、注册提交与持久化。
clearance.py: Cloudflare clearance 门面，负责 FlareSolverr 预热、cookie/UA 缓存、代理角色拆分与 Playwright 注入。
http_protocol.py: 纯 HTTP 注册协议实现，负责 curl_cffi session、clearance 注入、gRPC-Web 与 Server Action 请求。
turnstile_provider.py: Turnstile provider 解析与外部 solver 接线，隔离 browser/lite/capsolver/http 来源。
email_server.py: custom 邮箱本地 webhook 服务，承接 Cloudflare Email Routing 转发的验证码。
core/: CSP 基础模块，封装 AdmissionGate、ResourceEnvelope、Inventory、Metrics。

依赖边界
register.py 只通过 clearance.py 获取清障与代理能力；worker 只通过 core 库存门面移动 T/Q；HTTP 协议细节留在 http_protocol.py。

[PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
