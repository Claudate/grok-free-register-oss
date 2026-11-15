# tests/
> L2 | 父级: ../CLAUDE.md

成员清单
test_register_runtime_unittest.py: 注册运行时主回归，覆盖配置错误、清障诊断、注册持久化、worker 与热页池边界。
test_playwright_proxy_settings.py: Playwright 代理配置解析与环境变量回归。
test_proxy_pool.py: 代理池加载、轮换、坏代理隔离回归。
test_http_sso_isolation.py: HTTP session 中 SSO 与 clearance cookie 隔离回归。
test_turnstile_provider.py: Turnstile provider 选择与外部 solver 配置回归。
test_admission_gate.py: CSP 局部门控水位回归。
test_inventory_unittest.py: Inventory、PairLease、slot 生命周期回归。
test_runtime_log_analyzer.py: 运行日志解析兼容性回归。
test_xai_auth_*.py: OAuth 认证服务、pipeline、来源同步与出货回归。
test_xai_enroller_*.py: device-flow enroller 配置、协议、执行器、账本与 sink 回归。
test_cancel.py: 取消语义边界测试。
test_property.py: 库存与并发不变量随机化测试。
test_stress.py: fake-service 并发压力测试。
test_shell_entrypoints.py: shell 入口脚本行为回归。
conftest.py: pytest 公共夹具与路径设置。
fakes.py: 跨测试 fake 对象与轻量替身。
requirements.txt: 测试依赖清单。

依赖边界
测试优先使用 fake 对象隔离 Playwright、网络与外部服务；真实网络、真实浏览器和 destructive runtime 数据不应进入单元测试。

[PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
