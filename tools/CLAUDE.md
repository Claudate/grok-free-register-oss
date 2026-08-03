# tools/
> L2 | 父级: ../CLAUDE.md

成员清单
__init__.py: 工具包标识，无运行逻辑。
runtime_log_analyzer.py: 注册运行日志解析器，提取吞吐、库存、阶段耗时与限流信号。
GrokSession2CPAAndSub2API/: 纯前端凭证转换器，将 SSO/OAuth 出货转为 CPA、sub2api 与 grok2api 导入格式。

依赖边界
tools/ 只放离线、本地、可提交的辅助工具；真实凭据与运行出货仍留在 keys/ 或外部下载目录，不进入仓库。

[PROTOCOL]: 变更时更新此头部，然后检查 CLAUDE.md
