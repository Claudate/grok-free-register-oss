#!/bin/bash
# 一键启动:自动装依赖 → 引导配置 → 运行
# 用法:
#   bash start.sh                          # 首次会引导选模式,之后直接启动
#   bash start.sh --reconfig               # 重新选择邮箱模式
#   bash start.sh --debug                  # 保留完整调试面板
#   bash start.sh --proxy URL              # 单代理 (REGISTER_PROXY)
#   bash start.sh --proxy-file path        # 加载整份代理池，任务间轮换；失效自动跳过
#   bash start.sh --loop                   # 崩溃后自动重启（适合整晚跑）
#   bash start.sh --proxy-file p.txt --loop --target 0
set -e
cd "$(dirname "$0")"

. scripts/ensure_runtime.sh
ensure_runtime

if [ "${1:-}" = "--email-service" ]; then
    shift
    if command -v flock >/dev/null 2>&1; then
        mkdir -p logs
        exec 8>logs/email-service.lock
        if ! flock -n 8; then
            echo "[!] 邮箱服务已经在运行。"
            exit 1
        fi
    fi
    echo "[*] 启动邮箱服务... (Ctrl-C 停止)"
    exec .venv/bin/python -m grok_register.email_server "$@"
fi

# 打印时脱敏：user:pass@host → user:***@host
mask_proxy_url() {
    local url="$1"
    if [[ "$url" =~ ^(https?|socks5?h?)://([^:/@]+):([^@]+)@(.+)$ ]]; then
        echo "${BASH_REMATCH[1]}://${BASH_REMATCH[2]}:***@${BASH_REMATCH[4]}"
    else
        echo "$url"
    fi
}

count_proxy_file_lines() {
    local file="$1"
    awk 'NF && $0 !~ /^[[:space:]]*#/ { n++ } END { print n+0 }' "$file"
}

reconfig=0
loop_mode=0
loop_sleep=15
proxy_url=""
proxy_file=""
register_args=()
args=("$@")
i=0
while [ $i -lt ${#args[@]} ]; do
    arg="${args[$i]}"
    case "$arg" in
        --reconfig)
            reconfig=1
            ;;
        --loop)
            loop_mode=1
            ;;
        --loop-sleep)
            i=$((i + 1))
            if [ $i -ge ${#args[@]} ]; then
                echo "[!] --loop-sleep 需要秒数" >&2
                exit 1
            fi
            loop_sleep="${args[$i]}"
            ;;
        --loop-sleep=*)
            loop_sleep="${arg#--loop-sleep=}"
            ;;
        --proxy)
            i=$((i + 1))
            if [ $i -ge ${#args[@]} ]; then
                echo "[!] --proxy 需要一个 URL，例如:" >&2
                echo "      bash start.sh --proxy 'http://user:pass@host:3129'" >&2
                exit 1
            fi
            proxy_url="${args[$i]}"
            ;;
        --proxy=*)
            proxy_url="${arg#--proxy=}"
            ;;
        --proxy-file)
            i=$((i + 1))
            if [ $i -ge ${#args[@]} ]; then
                echo "[!] --proxy-file 需要文件路径" >&2
                exit 1
            fi
            proxy_file="${args[$i]}"
            ;;
        --proxy-file=*)
            proxy_file="${arg#--proxy-file=}"
            ;;
        *)
            register_args+=("$arg")
            ;;
    esac
    i=$((i + 1))
done

if [ -n "$proxy_url" ] && [ -n "$proxy_file" ]; then
    echo "[!] --proxy 与 --proxy-file 只能二选一" >&2
    exit 1
fi

if [ -n "$proxy_file" ]; then
    if [ ! -f "$proxy_file" ]; then
        echo "[!] 代理文件不存在: $proxy_file" >&2
        exit 1
    fi
    # 绝对路径，避免 cwd 变化后找不到
    case "$proxy_file" in
        /*) proxy_file_abs="$proxy_file" ;;
        *)  proxy_file_abs="$(pwd)/$proxy_file" ;;
    esac
    n_proxies="$(count_proxy_file_lines "$proxy_file_abs")"
    if [ "${n_proxies:-0}" -le 0 ]; then
        echo "[!] 代理文件无可用行: $proxy_file_abs" >&2
        exit 1
    fi
    export PROXY_FILE="$proxy_file_abs"
    # 清掉单代理 env，避免只吃第一条而忽略文件
    unset REGISTER_PROXY HTTPS_PROXY HTTP_PROXY ALL_PROXY 2>/dev/null || true
    echo "[*] 代理池已加载: $n_proxies 条 ← $proxy_file_abs"
    echo "[*] 策略: 每任务轮换；失效自动隔离 ${PROXY_BAD_TTL_SEC:-300}s (PROXY_STRATEGY=${PROXY_STRATEGY:-round_robin})"
fi

if [ -n "$proxy_url" ]; then
    export REGISTER_PROXY="$proxy_url"
    unset PROXY_FILE 2>/dev/null || true
    echo "[*] 单代理已开启: $(mask_proxy_url "$REGISTER_PROXY")"
fi

# 同一工作目录只允许一个注册进程，避免重复启动同时写账号和日志。
if command -v flock >/dev/null 2>&1; then
    mkdir -p logs
    exec 9>logs/register.lock
    if ! flock -n 9; then
        echo "[!] 注册机已经在运行。"
        exit 1
    fi
fi

# 1) 配置:无 .env 或显式 --reconfig 时进入引导
if [ ! -f .env ] || [ "$reconfig" -eq 1 ]; then
    echo ""
    echo "选择邮箱模式:"
    echo "  [1] 免费临时邮箱           (默认 · 零配置 · 直接回车 · 多 provider 自动 fallback)"
    echo "  [2] 自建域名邮箱           (需 Cloudflare Email Routing + 本地 webhook)"
    read -rp "输入 1 或 2 [1]: " mode || mode=1
    if [ "$mode" = "2" ]; then
        read -rp "  你的域名 (如 example.com): " domain
        read -rp "  webhook 地址 [http://127.0.0.1:8080]: " api
        api=${api:-http://127.0.0.1:8080}
        cat > .env <<ENV
EMAIL_MODE=custom
EMAIL_DOMAIN=${domain}
EMAIL_API=${api}
# CSP 容量(可选,0=按 CPU/内存启动期静态派生)
# PHYSICAL_CAP=0
# PHYSICAL_PER_CPU=2
# PHYSICAL_MEM_MB=512
# MIN_FREE_MEM_MB=500
ENV
        echo ""
        echo "[!] custom 模式还需在另一终端运行收信服务:"
        echo "      bash start.sh --email-service"
        echo "    并按 README「自建邮箱模式」配置 Cloudflare Email Worker。"
    else
        echo "EMAIL_MODE=tempmail" > .env
    fi
    echo "[*] 已写入 .env"
fi

# 2) 运行
run_register() {
    .venv/bin/python -m grok_register.register "${register_args[@]}"
}

if [ "$loop_mode" -eq 1 ]; then
    echo "[*] 整晚守护模式: 退出后 ${loop_sleep}s 自动重启 (Ctrl-C 停止)"
    while true; do
        set +e
        run_register
        code=$?
        set -e
        # 0 = 正常跑满 target 或主动退出
        if [ "$code" -eq 0 ]; then
            echo "[*] 注册进程正常退出 (code=0)，守护结束。"
            exit 0
        fi
        echo "[!] 注册进程异常退出 code=$code，${loop_sleep}s 后重启… $(date '+%F %T')"
        sleep "$loop_sleep"
    done
else
    echo "[*] 启动注册服务... (Ctrl-C 停止)"
    exec .venv/bin/python -m grok_register.register "${register_args[@]}"
fi
