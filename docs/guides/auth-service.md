# 本地认证服务

认证服务把已有的 SSO 会话转换为 CPA 可直接读取的 OAuth 凭据。注册与认证可以在同一台机器，也可以分开运行。

## 默认同机运行

同一个项目目录已经或正在运行注册服务时，直接启动认证：

```bash
bash auth-service.sh
```

未配置 SSH 主机时，认证服务自动读取本项目 `keys/` 中的完整会话与历史账号。注册可以继续追加，认证服务只安装经过校验的完整快照。

## 配置远端同步

先把无密码导出器放到服务器项目目录：

```bash
scp scripts/export_registered_sessions.py user@server.example:/opt/grok-free-register/scripts/
```

在本地终端设置连接信息：

可以直接 `export`，也可以把 `.env.example` 复制为 `.env` 后填写；认证入口会自动读取 `.env`。

```bash
export XAI_AUTH_SERVICE_SSH_HOST=user@server.example
export XAI_AUTH_SERVICE_SSH_IDENTITY=/path/to/key.pem
export XAI_AUTH_SERVICE_REMOTE_ROOT=/opt/grok-free-register
```

使用 `ssh-agent` 时可省略 `XAI_AUTH_SERVICE_SSH_IDENTITY`。

设置了 `XAI_AUTH_SERVICE_SSH_HOST` 后，默认的 `auto` 模式会选择 SSH。需要明确覆盖时使用：

```bash
export XAI_AUTH_SERVICE_SOURCE=local  # 强制读取同机注册结果
export XAI_AUTH_SERVICE_SOURCE=ssh    # 强制使用 SSH，必须配置主机
```

## 运行

```bash
bash auth-service.sh
```

首次运行会自动安装项目依赖。该命令在当前终端持续运行并直接接受控制命令；输入 `q` 或按 `Ctrl-C` 停止，再次执行同一命令即可重启。不需要额外的会话管理工具。

普通模式只在来源连接、发现新账号、任务开始、认证结果、限流和控制状态变化时输出。查看队列、重试、节拍和冷却探针时使用：

```bash
bash auth-service.sh --debug
```

运行中终端底部会保持 `认证> ` 输入行。日志更新不会清掉尚未提交的内容；直接输入命令并回车：

```text
s       查看状态
take N  取用 N 个凭据
p       暂停
r       恢复
c       取消当前任务
q       安全退出
```

快照默认每 30 秒更新一次；内容无变化时终端保持安静。有效快照和已生成凭据会在重启后继续使用。
