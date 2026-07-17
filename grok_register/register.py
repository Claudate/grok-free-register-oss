"""
Grok Free Register — CSP 异步并发架构
=============================================
单进程 asyncio + 单共享 CloakBrowser + Semaphore 背压:
  - S_Worker: 生成 Turnstile token (T)
  - P_Worker: 创建邮箱 + 发送验证码 + 轮询验证码 (Q)
  - C_Worker: claim pair 并执行注册
  - Semaphore 背压控制容量,无需中心调度器

两种邮箱模式(EMAIL_MODE):
  - tempmail (默认,零配置): 免费临时邮箱,多 provider 自动 fallback
  - custom: 自建域名邮箱,Cloudflare Email Routing → Worker → 本地 webhook
            (见 grok_register/email_server.py / cloudflare/email-worker.js)

配置全部走环境变量 / .env(见 .env.example);CLI: --max-mem 6G --target 100
用法:
  bash start.sh          # 一键引导
"""
import os, json, random, string, time, re, secrets, base64, struct, asyncio, glob, sys, multiprocessing, threading, uuid
from contextlib import suppress
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
import requests as req
from urllib.parse import quote
from playwright.async_api import async_playwright
from concurrent.futures import ThreadPoolExecutor

# CSP 架构组件
from grok_register.core.admission import AdmissionGate
from grok_register.core.envelope import ResourceEnvelope
from grok_register.core.inventory import Inventory
from grok_register.core.observer import Metrics
from grok_register.clearance import (
    apply_clearance_to_context,
    cached_user_agent,
    format_prewarm_log,
    playwright_proxy_settings,
    prewarm_clearance,
    register_proxy_url,
)
from grok_register import turnstile_provider as turnstile_provider_mod

# PROTOCOL_HTTP=1 → P/C 业务腿走 curl_cffi（Create/Verify/ServerAction + set-cookie）
# S 默认仍浏览器 mint Turnstile；TURNSTILE_PROVIDER=lite|capsolver|http 可卸掉 Physical_Sem。
# 清障仍靠 FlareSolverr → clearance 注入 HTTP session。默认 PROTOCOL_HTTP=0。
# 默认 0，保持全浏览器路径与现有单测契约。

os.makedirs("keys", exist_ok=True)
SITE_URL = "https://accounts.x.ai"

# ── 配置（环境变量 / .env，见 .env.example）──
def _env_int(key, default):
    try:
        return int(str(os.environ.get(key, "")).strip() or default)
    except ValueError:
        return default

def _env_int_or_none(key):
    raw = str(os.environ.get(key, "")).strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None

EMAIL_MODE      = (os.environ.get("EMAIL_MODE") or "tempmail").strip().lower()   # tempmail | custom
if EMAIL_MODE == "mailtm":      # 兼容旧名
    EMAIL_MODE = "tempmail"
LOCAL_EMAIL_API = (os.environ.get("EMAIL_API") or "http://127.0.0.1:8080").strip()
EMAIL_DOMAIN    = (os.environ.get("EMAIL_DOMAIN") or "").strip()
MIN_FREE_MEM_MB = _env_int("MIN_FREE_MEM_MB", 500)   # 自动容量派生时保留的内存(MB)
T_TARGET        = _env_int("T_TARGET", 4)            # token 池缓冲目标
Q_TARGET        = _env_int("Q_TARGET", 4)            # 就绪验证码缓冲目标
TARGET          = _env_int("TARGET", 0)              # 攒够 N 个号自动停(0=不限;--target N 可覆盖)

# CSP 容量参数
PHYSICAL_CAP    = _env_int("PHYSICAL_CAP", 0)        # 本地物理资源许可,0=自动派生
PHYSICAL_PER_CPU = _env_int("PHYSICAL_PER_CPU", 2)   # 自动派生 CPU 侧保守上限;压测可临时覆盖
PHYSICAL_MEM_MB = _env_int("PHYSICAL_MEM_MB", 512)   # 每个物理许可的保守内存预算(MB)
CAPACITY_PROFILE = (os.environ.get("CAPACITY_PROFILE") or "").strip()
T_SLOT_CAP      = _env_int("T_SLOT_CAP", 8)          # token 库存缓冲
Q_SLOT_CAP      = _env_int("Q_SLOT_CAP", 8)          # 验证码库存缓冲
Q_PENDING_CAP   = _env_int("Q_PENDING_CAP", 12)       # 外部在途 Q 请求上限
T_MAX_AGE       = _env_int("T_MAX_AGE", 300)          # token 最大年龄(秒)
Q_MAX_AGE       = _env_int("Q_MAX_AGE", 120)          # 验证码最大年龄(秒)
P_REQUEST_TIMEOUT = _env_int("P_REQUEST_TIMEOUT", 95) # P 等待 Q 返回超时(秒)
C_CONSUME_TIMEOUT = _env_int("C_CONSUME_TIMEOUT", 60) # C 消费完整 pair 超时(秒)
S_WORKERS       = _env_int("S_WORKERS", 0)            # 0=自动
P_WORKERS       = _env_int("P_WORKERS", 0)
C_WORKERS       = _env_int("C_WORKERS", 0)
C_HOT_PAGE_POOL = (os.environ.get("C_HOT_PAGE_POOL", "0").strip().lower() in ("1", "true", "yes"))
C_HOT_PAGE_POOL_SIZE = _env_int("C_HOT_PAGE_POOL_SIZE", 0)
C_SET_COOKIE_VIA_REQUEST = (
    os.environ.get("C_SET_COOKIE_VIA_REQUEST", "1" if C_HOT_PAGE_POOL else "0")
    .strip()
    .lower()
    in ("1", "true", "yes")
)
PROTOCOL_HTTP = (
    os.environ.get("PROTOCOL_HTTP", "0").strip().lower() in ("1", "true", "yes")
)
# CreateEmailValidationCode field3（Castle）；空=不发。边沿变严时由外部 mint 注入。
CASTLE_TOKEN = (os.environ.get("CASTLE_TOKEN") or "").strip()
# S 腿 Turnstile 来源：browser(默认) | lite(本地:5072) | capsolver | http
# 外接 provider 不占 Physical_Sem / 浏览器页，是突破 S 天花板的主开关。
TURNSTILE_PROVIDER = turnstile_provider_mod.resolve_provider_name()
TURNSTILE_USES_BROWSER = turnstile_provider_mod.uses_browser(TURNSTILE_PROVIDER)
# 本地 browser solver：注入后先短轮询，token 已就绪则跳过 ~2s 空点击
SOLVER_EARLY_POLL_MS = max(0, _env_int("SOLVER_EARLY_POLL_MS", 800))
SOLVER_EARLY_POLL_ATTEMPTS = max(0, _env_int("SOLVER_EARLY_POLL_ATTEMPTS", 2))

# 进程级 HTTP 客户端池（仅 PROTOCOL_HTTP）；browser S 仍占 physical_sem。
# 单 Session RLock 会串行化全部 P/C，池化后每槽独立 Session 并行。
_HTTP_CLIENT = None  # XAIHttpClientPool | XAIHttpClient
_HTTP_CLIENT_LOCK = asyncio.Lock()
HTTP_POOL_SIZE = max(1, min(32, _env_int("HTTP_POOL_SIZE", 8)))

# CSP v2 局部门控/批量发送参数。水位默认在启动期结合 Physical_Sem 派生。
_T_HIGH_WATER_OVERRIDE = _env_int_or_none("T_HIGH_WATER")
_T_LOW_WATER_OVERRIDE  = _env_int_or_none("T_LOW_WATER")
_Q_HIGH_WATER_OVERRIDE = _env_int_or_none("Q_HIGH_WATER")
_Q_LOW_WATER_OVERRIDE  = _env_int_or_none("Q_LOW_WATER")
P_BATCH_MAX     = max(1, _env_int("P_BATCH_MAX", 4))
P_SEND_CAP      = _env_int("P_SEND_CAP", 0)           # >0=显式限制并发 P 发送页面;0=不额外建模
PAGE_GOTO_WAIT_UNTIL = os.environ.get("PAGE_GOTO_WAIT_UNTIL", "domcontentloaded").strip() or "domcontentloaded"
PAGE_POST_WAIT_MS = _env_int("PAGE_POST_WAIT_MS", 500)
PAGE_BLOCK_STATIC_ASSETS = (
    os.environ.get("PAGE_BLOCK_STATIC_ASSETS", "0").strip().lower()
    in ("1", "true", "yes")
)
REGISTRATION_DIAGNOSTICS = (
    os.environ.get("REGISTRATION_DIAGNOSTICS", "0").strip().lower()
    in ("1", "true", "yes")
)
# Short cooldown floor; long floors over-punish false-positive rate limits.
REGISTRATION_RATE_LIMIT_COOLDOWN = max(
    5, _env_int("REGISTRATION_RATE_LIMIT_COOLDOWN", 20)
)
REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS = max(
    1, _env_int("REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS", 60)
)
REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL = max(
    1, _env_int("REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL", 3)
)
REGISTER_LOG_MODE = "user"

SITE_KEY = None
ACTION_ID = None
STATE_TREE = None

start_time = time.time()
success_count = 0
file_lock = asyncio.Lock()
STOP = asyncio.Event()

# 角色标识 + 轮询/HTTP 专用线程池（与 CPU 密集的浏览器操作解耦）
SOLVE, PRODUCE, CONSUME, IDLE = 'SOLVE', 'PRODUCE', 'CONSUME', 'IDLE'
POLL_EXECUTOR = ThreadPoolExecutor(max_workers=32)

def resolve_register_log_mode(argv=None, env=None):
    argv = list([] if argv is None else argv)
    env = dict(os.environ if env is None else env)
    mode = (env.get("REGISTER_LOG_MODE") or "user").strip().lower()
    if "--debug" in argv:
        mode = "debug"
    if mode not in {"user", "debug"}:
        raise ValueError("REGISTER_LOG_MODE must be user or debug")
    return mode


def _terminal_output(msg):
    print(msg, flush=True)


def log(msg):
    try:
        _terminal_output(msg)
    except Exception:
        return


def debug_log(msg):
    if REGISTER_LOG_MODE == "debug":
        log(msg)
def rand_str(n=15): return ''.join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def sanitize_terminal_error(error):
    return type(error).__name__


def format_user_registration_event(
    kind,
    *,
    task_id=None,
    count=None,
    rate_per_minute=None,
    wait_seconds=None,
    remaining=None,
):
    if kind == "service_started":
        progress = f"剩余 {remaining}" if remaining is not None else "持续运行"
        return f"[✓] 注册服务已启动 | {progress}"
    if kind == "started":
        suffix = f" | 剩余 {remaining}" if remaining is not None else ""
        return f"[→] 开始注册 #{task_id}{suffix}"
    if kind == "success":
        rate = "—" if rate_per_minute is None else f"{rate_per_minute:.1f}/分"
        return f"[✓] 注册成功 #{task_id} | 运行平均 {rate} | 累计 {count}"
    if kind == "failed":
        return f"[✗] 注册失败 #{task_id} | 已跳过，继续下一任务"
    if kind == "rate_limited":
        return f"[⏸] 触发限流 | {wait_seconds}秒后恢复探测"
    if kind == "recovered":
        return f"[▶] 限流解除 | 实际等待 {wait_seconds}秒"
    if kind == "stopped":
        return f"[■] 注册服务已停止 | 累计 {count or 0}"
    raise ValueError(f"unknown user registration event: {kind}")


class RegistrationRateLimited(RuntimeError):
    """注册提交被目标站点的限流页替代。"""


class RegistrationRateLimitCircuit:
    """在检测到注册限流后暂停新的 C 阶段提交。"""

    def __init__(
        self,
        cooldown_seconds,
        recovery_seconds=60,
        recovery_interval=3,
        clock=time.monotonic,
    ):
        self.cooldown_seconds = cooldown_seconds
        self.recovery_seconds = recovery_seconds
        self.recovery_interval = recovery_interval
        self._clock = clock
        self._blocked_until = 0.0
        self._tripped_at = None
        self._probe_active = False
        self._probe_token = None
        self._recovering_until = 0.0
        self._next_recovery_submit = 0.0

    def remaining_seconds(self):
        return max(0, int(self._blocked_until - self._clock() + 0.999))

    def is_open(self):
        return self.remaining_seconds() > 0

    def trip(self):
        starts_new_window = not self.is_open()
        if self._tripped_at is None:
            self._tripped_at = self._clock()
        self._recovering_until = 0.0
        self._next_recovery_submit = 0.0
        self._probe_active = False
        self._probe_token = None
        self._blocked_until = max(
            self._blocked_until,
            self._clock() + self.cooldown_seconds,
        )
        return starts_new_window

    async def wait(self):
        while True:
            if self.is_open():
                await asyncio.sleep(min(self.remaining_seconds(), 5))
                continue
            if self._tripped_at is None:
                if not self._recovering_until:
                    return False
                if self._probe_active:
                    await asyncio.sleep(0.2)
                    continue
                if self._clock() >= self._recovering_until:
                    self._recovering_until = 0.0
                    self._next_recovery_submit = 0.0
                    return False
                recovery_wait = self._next_recovery_submit - self._clock()
                if recovery_wait > 0:
                    await asyncio.sleep(min(recovery_wait, 0.5))
                    continue
            if not self._probe_active:
                self._probe_active = True
                self._probe_token = object()
                return self._probe_token
            await asyncio.sleep(0.2)

    def can_submit(self, probe_token=False):
        """仅允许正常态任务或当前恢复探针发起注册提交。"""
        if self._tripped_at is None and not self._recovering_until:
            return True
        return (
            probe_token is not False
            and probe_token is self._probe_token
            and not self.is_open()
        )

    def consume_recovery_seconds(self, probe_token=None):
        if self._tripped_at is None:
            self.complete_recovery_submission(probe_token)
            return None
        if probe_token is not None and probe_token is not self._probe_token:
            return None
        elapsed = self._clock() - self._tripped_at
        self._tripped_at = None
        self._blocked_until = 0.0
        self._probe_active = False
        self._probe_token = None
        self._recovering_until = self._clock() + self.recovery_seconds
        self._next_recovery_submit = self._clock() + self.recovery_interval
        return elapsed

    def complete_recovery_submission(self, probe_token):
        """完成恢复期内的一次成功提交，并按固定节奏放行下一项。"""
        if probe_token is not self._probe_token or not self._recovering_until:
            return False
        self._probe_active = False
        self._probe_token = None
        self._next_recovery_submit = self._clock() + self.recovery_interval
        return True

    def release_probe(self, probe_token):
        """提交前资源已失效时让出探针，不额外增加冷却窗口。"""
        if probe_token is self._probe_token:
            self._probe_active = False
            self._probe_token = None

    def defer_probe(self, probe_token):
        """真正的探针失败后重新进入完整冷却。"""
        if probe_token is not self._probe_token:
            return
        self._probe_active = False
        self._probe_token = None
        if self._tripped_at is not None:
            self._blocked_until = max(
                self._blocked_until,
                self._clock() + self.cooldown_seconds,
            )
        elif self._recovering_until:
            self._next_recovery_submit = self._clock() + self.recovery_interval


REGISTRATION_RATE_LIMIT_CIRCUIT = RegistrationRateLimitCircuit(
    REGISTRATION_RATE_LIMIT_COOLDOWN,
    recovery_seconds=REGISTRATION_RATE_LIMIT_RECOVERY_SECONDS,
    recovery_interval=REGISTRATION_RATE_LIMIT_RECOVERY_INTERVAL,
)

def _signup_response_markers(text):
    """将失败响应归类为固定标签；严格 needle，避免 RSC/JS 源码误触 rate_limited。

    Success-shaped RSC ($Sreact.fragment / set-cookie hop) must not trip
    rate_limited — chunk source embeds that string as a client error code.
    """
    lowered = (text or "").lower()
    hits = []
    looks_ok = bool(
        text
        and (
            "$Sreact.fragment" in text
            or '2:"$Sreact.fragment"' in text
            or "set-cookie?q=" in lowered
            or "set-cookie/?q=" in lowered
        )
        and not re.search(r"(?:^|\n)0:E\{", text)
    )
    if not looks_ok and (
        re.search(r"\brate_limited\b", lowered)
        or any(
            n in lowered
            for n in (
                "too many requests",
                "rate limited",
                "rate limit exceeded",
                "try again later",
            )
        )
    ):
        hits.append("rate_limited")
    if any(n in lowered for n in ("cf-chl", "challenge-platform", "just a moment")):
        hits.append("challenge")
    if any(n in lowered for n in ("failed to find server action", "invalid server action")):
        hits.append("action_error")
    if any(n in lowered for n in ("email already", "already registered", "disposable email")):
        hits.append("email")
    # Page-shape tags only when no hard failure — diagnostic, not circuit triggers.
    if not hits:
        if looks_ok:
            return "success_flight"
        if "sign in" in lowered or "log in" in lowered:
            hits.append("signin_page")
        if "sign up" in lowered or "create your account" in lowered:
            hits.append("signup_page")
        if "__next" in lowered or "/_next/" in lowered:
            hits.append("next_page")
    return ",".join(hits) or "unclassified"

def pb_varint(n):
    parts = []
    while n > 0x7f: parts.append((n & 0x7f) | 0x80); n >>= 7
    parts.append(n); return bytes(parts)
def pb_str(fid, val):
    vb = val.encode()
    return struct.pack('B', (fid << 3) | 2) + pb_varint(len(vb)) + vb


async def _get_http_client():
    """Lazy process-wide HTTP pool (or single client); re-applies clearance.

    Prefer XAIHttpClientPool so parallel P Create + C ServerAction don't
    serialize on one Session RLock (lite-style throughput path).
    """
    global _HTTP_CLIENT
    if not PROTOCOL_HTTP:
        return None
    async with _HTTP_CLIENT_LOCK:
        if _HTTP_CLIENT is None:
            from grok_register.http_protocol import XAIHttpClientPool

            def _build():
                return XAIHttpClientPool(size=HTTP_POOL_SIZE)

            _HTTP_CLIENT = await asyncio.to_thread(_build)
            debug_log(
                f"[+] HTTP pool ready size={getattr(_HTTP_CLIENT, 'size', 1)} "
                f"impersonate={_HTTP_CLIENT.impersonate} "
                f"ua={(_HTTP_CLIENT.user_agent or '')[:48]}"
            )
        else:
            apply = getattr(_HTTP_CLIENT, "apply_clearance_all", None)
            if callable(apply):
                await asyncio.to_thread(apply)
            else:
                await asyncio.to_thread(_HTTP_CLIENT.apply_clearance)
        return _HTTP_CLIENT


async def _http_pool_call(method_name: str, *args, **kwargs):
    """Checkout a pool slot (or use single client), run sync method, release."""
    pool = await _get_http_client()
    if pool is None:
        raise RuntimeError("PROTOCOL_HTTP client unavailable")
    acquire = getattr(pool, "acquire", None)
    release = getattr(pool, "release", None)
    if callable(acquire) and callable(release):
        client = await asyncio.to_thread(acquire)
        try:
            fn = getattr(client, method_name)
            return await asyncio.to_thread(fn, *args, **kwargs)
        finally:
            await asyncio.to_thread(release, client)
    fn = getattr(pool, method_name)
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _close_http_client():
    global _HTTP_CLIENT
    async with _HTTP_CLIENT_LOCK:
        client = _HTTP_CLIENT
        _HTTP_CLIENT = None
    if client is not None:
        with suppress(Exception):
            await asyncio.to_thread(client.close)


def decode_jwt_payload(token):
    parts = token.split('.')
    if len(parts) < 2: return None
    payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
    try: return json.loads(base64.urlsafe_b64decode(payload))
    except Exception: return None
def find_chrome():
    # Linux: ~/.cloakbrowser/chromium-*/chrome
    # macOS: ~/.cloakbrowser/chromium-*/Chromium.app/Contents/MacOS/Chromium
    patterns = [
        os.path.expanduser("~/.cloakbrowser/chromium-*/chrome"),
        os.path.expanduser("~/.cloakbrowser/chromium-*/Chromium.app/Contents/MacOS/Chromium"),
        os.path.expanduser("~/.cloakbrowser/chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium"),
    ]
    paths = []
    for pattern in patterns:
        paths.extend(glob.glob(pattern))
    # Prefer cloakbrowser API when available (handles layout drift).
    try:
        from cloakbrowser import ensure_binary
        api_path = ensure_binary()
        if api_path and os.path.isfile(api_path):
            paths.append(api_path)
    except Exception:
        pass
    paths = [p for p in paths if p and os.path.isfile(p)]
    if not paths:
        raise RuntimeError("CloakBrowser not found")
    return sorted(set(paths))[-1]


def _read_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
        return ""


def _is_cloakbrowser_chrome(cmdline: str) -> bool:
    """注册机用的 CloakBrowser 路径；排除 docker FS 的 /usr/lib/chromium。"""
    if not cmdline:
        return False
    if "cloakbrowser" in cmdline:
        return True
    # playwright 偶发只带 chrome 绝对路径
    if "/.cloakbrowser/" in cmdline and "chrome" in cmdline:
        return True
    return False


def reap_orphan_register_browsers(*, exclude_pids: set[int] | None = None) -> int:
    """杀掉无主的 CloakBrowser/chrome（上次 Ctrl-C 失败或 kill -9 残留）。

    只动 host 上 cloakbrowser 路径的进程，不动 docker 里 FlareSolverr 的 chromium。
    """
    import signal as _signal
    import time as _time

    exclude = set(exclude_pids or ())
    exclude.add(os.getpid())
    # 当前进程树：子进程先别杀，留给 browser.close
    try:
        for line in os.popen(f"ps -o pid= --ppid {os.getpid()}").read().split():
            try:
                exclude.add(int(line))
            except ValueError:
                pass
    except Exception:
        pass

    victims: list[int] = []
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            if pid in exclude or pid <= 1:
                continue
            cmd = _read_cmdline(pid)
            if not _is_cloakbrowser_chrome(cmd):
                continue
            # 跳过 docker cgroup（双保险）
            try:
                with open(f"/proc/{pid}/cgroup", "r", encoding="utf-8") as fh:
                    if "docker" in fh.read():
                        continue
            except Exception:
                pass
            victims.append(pid)
    except Exception:
        return 0

    if not victims:
        return 0

    for pid in victims:
        try:
            os.kill(pid, _signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    _time.sleep(0.4)
    for pid in victims:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        try:
            os.kill(pid, _signal.SIGKILL)
        except ProcessLookupError:
            pass
        except PermissionError:
            pass
    return len(victims)


async def _close_browser_hard(browser, timeout: float = 5.0) -> None:
    """先正常 close；超时则按进程树 SIGKILL，避免重启叠浏览器。"""
    if browser is None:
        return
    proc = getattr(browser, "process", None)
    pid = None
    if proc is not None:
        pid = getattr(proc, "pid", None)
    try:
        await asyncio.wait_for(browser.close(), timeout=timeout)
        return
    except Exception:
        pass
    if pid:
        try:
            # 杀进程组（playwright 常把 chrome 放同组）
            os.killpg(os.getpgid(pid), 9)
        except Exception:
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        # 扫一遍残留 cloakbrowser
        await asyncio.to_thread(reap_orphan_register_browsers)


# ──────────────────────────────────────────────
#  资源检测
# ──────────────────────────────────────────────
def get_system_resources(max_mem_arg=None):
    import subprocess
    cpu_count = multiprocessing.cpu_count()
    try:
        out = subprocess.check_output(["free", "-m"]).decode()
        for line in out.split("\n"):
            if "Mem" in line:
                parts = line.split()
                total, available = int(parts[1]), int(parts[6])
                break
        else:
            total, available = 4096, 2048
    except Exception:
        total, available = 4096, 2048

    if max_mem_arg:
        if max_mem_arg.endswith('%'):
            max_mem = int(total * float(max_mem_arg[:-1]) / 100)
        elif max_mem_arg.upper().endswith('G'):
            max_mem = int(float(max_mem_arg[:-1]) * 1024)
        elif max_mem_arg.upper().endswith('M'):
            max_mem = int(max_mem_arg[:-1])
        else:
            max_mem = int(max_mem_arg)
    else:
        max_mem = available

    return {'cpu': cpu_count, 'total_mem': total, 'available_mem': available, 'max_mem': max_mem}


def load_capacity_profile(path=CAPACITY_PROFILE):
    """读取离线校准生成的设备 profile。不存在或无效时返回空配置。"""
    if not path:
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    profile = {}
    try:
        physical_cap = int(data.get("physical_cap", 0))
    except (TypeError, ValueError):
        physical_cap = 0
    if physical_cap > 0:
        profile["physical_cap"] = physical_cap
    return profile


def derive_capacity(
    cpu_count,
    max_mem_mb,
    *,
    physical_cap=None,
    profile_physical_cap=None,
    physical_per_cpu=None,
    physical_mem_mb=None,
    min_free_mem_mb=None,
):
    """启动期静态容量派生:显式配置 > 设备 profile > CPU/内存保守自动值。"""
    configured_physical = PHYSICAL_CAP if physical_cap is None else physical_cap
    profiled_physical = profile_physical_cap or 0
    per_cpu = PHYSICAL_PER_CPU if physical_per_cpu is None else physical_per_cpu
    mem_per_physical = PHYSICAL_MEM_MB if physical_mem_mb is None else physical_mem_mb
    reserve_mem = MIN_FREE_MEM_MB if min_free_mem_mb is None else min_free_mem_mb

    cpu_cap = max(1, cpu_count * max(1, per_cpu))
    usable_mem = max(0, max_mem_mb - reserve_mem)
    mem_cap = max(1, usable_mem // max(1, mem_per_physical))
    auto_cap = max(1, min(cpu_cap, mem_cap))

    if configured_physical > 0:
        physical = configured_physical
    elif profiled_physical > 0:
        physical = max(1, min(profiled_physical, mem_cap))
    else:
        physical = auto_cap

    # Browser-S: never oversubscribe Physical_Sem (physical+2 → TargetClosed thrash).
    # External mint: keep a few extra S workers; they don't hold browsers.
    if S_WORKERS > 0:
        s_workers = S_WORKERS
    elif TURNSTILE_USES_BROWSER:
        s_workers = physical
    else:
        s_workers = max(physical + 2, min(32, max(8, T_SLOT_CAP * 2)))
    p_workers = P_WORKERS if P_WORKERS > 0 else Q_PENDING_CAP + 2
    # PROTOCOL_HTTP C is pure curl — scale with pool/pending, not browser slots.
    if C_WORKERS > 0:
        c_workers = C_WORKERS
    elif PROTOCOL_HTTP:
        c_workers = max(physical + 2, min(24, max(6, HTTP_POOL_SIZE)))
    else:
        c_workers = physical + 2
    return physical, s_workers, p_workers, c_workers


def derive_admission_watermarks(
    physical_cap,
    *,
    t_slot_cap=None,
    q_pending_cap=None,
    t_target=None,
    q_target=None,
    t_high_override=None,
    t_low_override=None,
    q_high_override=None,
    q_low_override=None,
):
    """派生 CSP v2 局部门控水位。

    T 的默认高水位跟随 Physical_Sem,避免物理并发提高后仍只允许少量 T
    in-progress。显式环境变量覆盖仍然优先。
    """
    t_slot = T_SLOT_CAP if t_slot_cap is None else t_slot_cap
    q_pending = Q_PENDING_CAP if q_pending_cap is None else q_pending_cap
    t_goal = T_TARGET if t_target is None else t_target
    q_goal = Q_TARGET if q_target is None else q_target

    t_high_cfg = _T_HIGH_WATER_OVERRIDE if t_high_override is None else t_high_override
    t_low_cfg = _T_LOW_WATER_OVERRIDE if t_low_override is None else t_low_override
    q_high_cfg = _Q_HIGH_WATER_OVERRIDE if q_high_override is None else q_high_override
    q_low_cfg = _Q_LOW_WATER_OVERRIDE if q_low_override is None else q_low_override

    if t_high_cfg is None:
        t_high = min(max(1, t_slot), max(1, t_goal, physical_cap))
    else:
        t_high = max(1, min(max(1, t_slot), t_high_cfg))

    if t_low_cfg is None:
        t_low = max(0, min(t_high, t_high // 2))
    else:
        t_low = max(0, min(t_high, t_low_cfg))

    if q_high_cfg is None:
        q_high = max(1, q_pending)
    else:
        q_high = max(1, min(max(1, q_pending), q_high_cfg))

    if q_low_cfg is None:
        q_low = max(0, min(q_high, q_goal, q_high // 2))
    else:
        q_low = max(0, min(q_high, q_low_cfg))

    return {
        "t_low": t_low,
        "t_high": t_high,
        "q_low": q_low,
        "q_high": q_high,
    }


def derive_c_hot_page_pool_size(physical_cap, c_workers, configured_size=None):
    """启动期静态派生 C 热页池容量；显式配置优先。"""
    configured = C_HOT_PAGE_POOL_SIZE if configured_size is None else configured_size
    if configured and configured > 0:
        return configured
    return max(1, min(max(1, physical_cap), max(1, c_workers)))


# ──────────────────────────────────────────────
#  配置获取
# ──────────────────────────────────────────────
def _launch_options():
    """Playwright chromium.launch kwargs. HTTP_PROXY/REGISTER_PROXY apply here (not to FlareSolverr)."""
    options = {"executable_path": find_chrome(), "headless": True}
    proxy = playwright_proxy_settings()
    if proxy:
        options["proxy"] = proxy
    return options


async def _prepare_browser_page(browser):
    """new_page + inject prewarmed CF cookies into its context when available."""
    page = await browser.new_page()
    await apply_clearance_to_context(page.context)
    return page


async def _prepare_browser_context(browser):
    """new_context with optional FS user-agent + CF cookies."""
    kwargs = {}
    ua = cached_user_agent()
    if ua:
        kwargs["user_agent"] = ua
    context = await browser.new_context(**kwargs)
    await apply_clearance_to_context(context)
    return context


async def fetch_config():
    global SITE_KEY, ACTION_ID, STATE_TREE
    debug_log('[*] Fetching config...')

    # PROTOCOL_HTTP: scrape via curl_cffi first (no browser for config).
    if PROTOCOL_HTTP:
        try:
            pool = await _get_http_client()
            scrape = getattr(pool, "scrape_signup_config", None)
            if callable(scrape):
                cfg = await asyncio.to_thread(scrape)
            else:
                cfg = await _http_pool_call("scrape_signup_config")
            if cfg.site_key:
                SITE_KEY = cfg.site_key
            if cfg.action_id:
                ACTION_ID = cfg.action_id
            if cfg.state_tree:
                STATE_TREE = cfg.state_tree
            debug_log(
                f'[+] HTTP config site_key={bool(SITE_KEY)} '
                f'action_id={bool(ACTION_ID)} state_tree={bool(STATE_TREE)} '
                f'src={cfg.source}'
            )
            if all([SITE_KEY, ACTION_ID, STATE_TREE]):
                return
            debug_log('[*] HTTP config incomplete, falling back to browser scrape')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            debug_log(f'[*] HTTP config err: {sanitize_terminal_error(exc)}; browser fallback')

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**_launch_options())
        try:
            page = await _prepare_browser_page(browser)
            # Prefer domcontentloaded: CF/xAI signup often never reaches full 'load'.
            config_goto_timeout_ms = int(os.environ.get("CONFIG_GOTO_TIMEOUT_MS", "60000") or "60000")
            await page.goto(
                f'{SITE_URL}/sign-up?redirect=grok-com',
                timeout=config_goto_timeout_ms,
                wait_until=PAGE_GOTO_WAIT_UNTIL,
            )
            await page.wait_for_timeout(int(os.environ.get("CONFIG_GOTO_SETTLE_MS", "5000") or "5000"))
            html = await page.content()
            m = re.search(r'0x4AAAAAAA[a-zA-Z0-9_-]+', html)
            if m: SITE_KEY = m.group(0); debug_log(f'[+] SITE_KEY: {SITE_KEY}')
            for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL):
                if 'sign-up' not in chunk: continue
                decoded = chunk.replace('\\"', '"')
                f_match = re.search(r'"f":\[\[\[', decoded)
                if not f_match: continue
                f_start = f_match.start() + 5
                end_idx = decoded.find('"$undefined"', f_start)
                if end_idx < 0: continue
                STATE_TREE = quote(decoded[f_start:end_idx].replace('\\\\"', '"').replace('\\', ''), safe='')
                debug_log(f'[+] STATE_TREE: {STATE_TREE[:50]}...')
                break
            js_urls = re.findall(r'src="(/_next/static/[^"]+\.js)"', html)
            for js_url in js_urls[:50]:
                try:
                    js = await page.evaluate(f"(async()=>{{return await fetch('{js_url}').then(r=>r.text()).catch(()=>\"\" )}})()")
                    if not js: continue
                    if not any(kw in js for kw in ['createUser','registerUser','emailValidation']): continue
                    hexes = re.findall(r'[a-fA-F0-9]{40,50}', js)
                    if hexes: ACTION_ID = hexes[0]; break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
            if ACTION_ID: debug_log(f'[+] ACTION_ID: {ACTION_ID}')
        finally:
            await _close_browser_hard(browser, timeout=5.0)
    if not all([SITE_KEY, ACTION_ID, STATE_TREE]):
        missing = [
            name
            for name, value in (
                ("SITE_KEY", SITE_KEY),
                ("ACTION_ID", ACTION_ID),
                ("STATE_TREE", STATE_TREE),
            )
            if not value
        ]
        proxy = register_proxy_url() or "(direct)"
        raise RuntimeError(
            "Config fetch failed: missing "
            + ", ".join(missing)
            + f"; url={SITE_URL}/sign-up proxy={proxy}. "
            "Usually CF/network blocks signup page scrape. "
            "Try: bash start.sh --debug; set REGISTER_PROXY; "
            "enable CLEARANCE_ENABLED=1 + FlareSolverr; "
            "or verify CloakBrowser can open accounts.x.ai."
        )


# ──────────────────────────────────────────────
#  异步操作
# ──────────────────────────────────────────────
async def grpc_create_code(page, email):
    """CreateEmailValidationCode.

    page=None 且 PROTOCOL_HTTP → curl_cffi；否则 page.evaluate（浏览器路径）。
    """
    if page is None and PROTOCOL_HTTP:
        result = await _http_pool_call(
            "create_email_code", email, castle_token=CASTLE_TOKEN
        )
        # Always log failed creates on HTTP path — grpc=3 / CF are the P wall.
        # Include domain so domain-ban vs protocol is one glance.
        if not result.ok:
            domain = email.rsplit("@", 1)[-1] if email and "@" in email else "?"
            debug_log(
                f'[P] create http={result.http_status} grpc={result.grpc_status} '
                f'cf={result.cf_blocked} domain={domain} '
                f'err={(result.error or "")[:80]}'
            )
        return bool(result.ok)

    # field1=email；可选 field3=Castle（边沿加严时由 CASTLE_TOKEN 注入）
    inner = pb_str(1, email)
    if CASTLE_TOKEN:
        inner = inner + pb_str(3, CASTLE_TOKEN)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    return s == '0'


async def _prepare_signup_page(page, *, redirect=True, timeout=30000):
    if PAGE_BLOCK_STATIC_ASSETS:
        await page.route("**/*", _route_static_asset_filter)
    url = f'{SITE_URL}/sign-up?redirect=grok-com' if redirect else f'{SITE_URL}/sign-up'
    await page.goto(url, timeout=timeout, wait_until=PAGE_GOTO_WAIT_UNTIL)
    if PAGE_POST_WAIT_MS > 0:
        await page.wait_for_timeout(PAGE_POST_WAIT_MS)


async def _route_static_asset_filter(route):
    req = route.request
    if (
        req.resource_type in ("image", "font", "media", "stylesheet")
        or "/_next/static/" in req.url
        or "analytics" in req.url
    ):
        await route.abort()
        return
    await route.continue_()


async def grpc_verify_code(page, email, code):
    """VerifyEmailValidationCode. page=None + PROTOCOL_HTTP → HTTP."""
    if page is None and PROTOCOL_HTTP:
        result = await _http_pool_call("verify_email_code", email, code)
        if not result.ok:
            debug_log(
                f'[C] verify http={result.http_status} grpc={result.grpc_status} '
                f'cf={result.cf_blocked} err={(result.error or "")[:80]}'
            )
        return bool(result.ok)

    inner = pb_str(1, email) + pb_str(2, code)
    frame = b'\x00' + struct.pack('>I', len(inner)) + inner
    fb64 = base64.b64encode(frame).decode()
    s = await page.evaluate(f"(async()=>{{var fb=Uint8Array.from(atob('{fb64}'),c=>c.charCodeAt(0));var r=await fetch('{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode',{{method:'POST',headers:{{'content-type':'application/grpc-web+proto','x-grpc-web':'1','x-user-agent':'connect-es/2.1.1'}},body:fb.buffer}});return r.headers.get('grpc-status')||'0';}})()")
    if REGISTRATION_DIAGNOSTICS and s != '0':
        debug_log('[C] verify rejected')
    return s == '0'


def auth_cookie_snapshot(cookies):
    """保留认证所需 Cookie 的原始作用域；不写入邮箱密码。"""
    fields = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")
    return [
        {field: cookie[field] for field in fields if field in cookie}
        for cookie in cookies
        if cookie.get("name") in {"sso", "sso-rw"}
    ]


async def server_action_register(
    page,
    email,
    password,
    code,
    turnstile_token,
    *,
    include_session=False,
):
    # PROTOCOL_HTTP + page=None: full Server Action + set-cookie over curl_cffi
    if page is None and PROTOCOL_HTTP:
        if not ACTION_ID or not STATE_TREE:
            raise RuntimeError("HTTP signup requires ACTION_ID and STATE_TREE")
        result = await _http_pool_call(
            "server_action_register",
            email=email,
            password=password,
            code=code,
            turnstile_token=turnstile_token,
            action_id=ACTION_ID,
            state_tree=STATE_TREE,
            castle_request_token=CASTLE_TOKEN,
        )
        if result.cf_blocked:
            # Always surface CF on HTTP C — silent skip was the 0-sso black hole.
            debug_log(
                f"[C] signup CF blocked on HTTP path http={result.http_status} "
                f"imp=check body={(result.body_preview or '')[:120]!r}"
            )
            return None
        # Log every HTTP signup outcome before circuit trip — rate_limited used to
        # raise first and hide markers/body, making pair burn look unexplained.
        if result.sso:
            debug_log(
                f"[C] signup HTTP ok src={getattr(result, 'sso_source', '')} "
                f"ms={result.elapsed_ms} markers={result.markers}"
            )
        else:
            debug_log(
                f"[C] signup HTTP no sso http={result.http_status} "
                f"markers={result.markers} set_url={bool(result.set_cookie_url)} "
                f"src={getattr(result, 'sso_source', '')} "
                f"err={(result.error or '')[:160]} "
                f"body={(result.body_preview or '')[:160]!r}"
            )
        if "rate_limited" in (result.markers or ""):
            raise RegistrationRateLimited("signup_rate_limited")
        if not result.sso:
            return None
        if include_session:
            # HTTP jar: only sso value is reliable; sso-rw may or may not land
            cookies = [
                {
                    "name": "sso",
                    "value": result.sso,
                    "domain": ".x.ai",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                    "sameSite": "None",
                }
            ]
            return result.sso, auth_cookie_snapshot(cookies)
        return result.sso

    # Browser path: lite-aligned Server Action body (conversionId + castle + $T arg).
    payload = json.dumps([
        {
            "emailValidationCode": code,
            "createUserAndSessionRequest": {
                "email": email,
                "givenName": random.choice(["James","John","Robert","Michael","William","David","Richard","Joseph","Thomas","Charles"]),
                "familyName": random.choice(["Smith","Johnson","Williams","Brown","Jones","Garcia","Miller","Davis","Rodriguez","Martinez"]),
                "clearTextPassword": password,
                "tosAcceptedVersion": "$undefined",
            },
            "turnstileToken": turnstile_token,
            "conversionId": str(uuid.uuid4()),
            "castleRequestToken": CASTLE_TOKEN or "",
        },
        {
            "client": "$T",
            "meta": "$undefined",
            "mutationKey": "$undefined",
        },
    ], separators=(",", ":"))
    pb64 = base64.b64encode(payload.encode()).decode()
    if REGISTRATION_DIAGNOSTICS:
        diagnostic_json = await page.evaluate(f"""(async()=>{{var r=await fetch('{SITE_URL}/sign-up',{{method:'POST',headers:{{'accept':'text/x-component','content-type':'text/plain;charset=UTF-8','next-router-state-tree':'{STATE_TREE}','next-action':'{ACTION_ID}'}},body:atob('{pb64}')}});return JSON.stringify({{status:r.status,retryAfter:r.headers.get('retry-after')||'',text:await r.text()}});}})()""")
        diagnostic = json.loads(diagnostic_json)
        result_text = diagnostic['text']
    else:
        diagnostic = None
        result_text = await page.evaluate(f"""(async()=>{{var r=await fetch('{SITE_URL}/sign-up',{{method:'POST',headers:{{'accept':'text/x-component','content-type':'text/plain;charset=UTF-8','next-router-state-tree':'{STATE_TREE}','next-action':'{ACTION_ID}'}},body:atob('{pb64}')}});return await r.text();}})()""")
    # 注册响应里带一个 set-cookie 重定向 URL,必须访问它,x.ai 才会下发真正的 sso cookie(152 字符 JWT)。
    # 注意:直接解 q= 里的 JWT 取 config.token 是错的——那是 120 字符内部 blob,不是 sso 凭证。
    text = result_text.replace('\\/', '/')  # RSC 里 / 被转义成 \/
    m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[^:" \s\\]+)1:', text)
    if not m:
        m = re.search(r'(https://[^" \s\\]+set-cookie\?q=[A-Za-z0-9_.\-]+)', text)
    if not m:
        markers = _signup_response_markers(result_text)
        if diagnostic is not None:
            debug_log(
                f"[C] signup no session http_status={diagnostic['status']} "
                f"retry_after={diagnostic['retryAfter'] or '-'} response_bytes={len(result_text)} "
                f"markers={markers}"
            )
        if "rate_limited" in markers:
            raise RegistrationRateLimited("signup_rate_limited")
        return None
    url = m.group(1)
    if C_SET_COOKIE_VIA_REQUEST:
        try:
            await page.context.request.get(url, timeout=15000)
            cookies = await page.context.cookies()
            sso = next((c['value'] for c in cookies if c['name'] == 'sso'), None)
            if sso:
                if include_session:
                    return sso, auth_cookie_snapshot(cookies)
                return sso
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    # 首方导航访问该 URL,浏览器正常落 sso cookie(跨域 fetch 会被 CORS/三方cookie 拦)
    try:
        await page.goto(url, timeout=15000, wait_until='domcontentloaded')
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    cookies = await page.context.cookies()
    sso = next((c['value'] for c in cookies if c['name'] == 'sso'), None)
    if REGISTRATION_DIAGNOSTICS and not sso:
        debug_log('[C] signup set-cookie completed without sso cookie')
    if not sso:
        return None
    if include_session:
        return sso, auth_cookie_snapshot(cookies)
    return sso

# solver 预热页面池:复用已停在 sign-up 的页面,省掉每次 page.goto 的重型 SPA 加载
# SOLVER_REUSE=0 可关闭(用于 A/B 对比 goto 优化的增益)
SOLVER_REUSE = (os.environ.get("SOLVER_REUSE", "1").strip().lower() not in ("0", "false", "no"))
_solver_pool = []
_solver_lock = asyncio.Lock()
MAX_SOLVER_REUSE = _env_int("MAX_SOLVER_REUSE", 25)
SOLVER_INITIAL_WAIT_MS = _env_int("SOLVER_INITIAL_WAIT_MS", 500)
SOLVER_POLL_INTERVAL_MS = _env_int("SOLVER_POLL_INTERVAL_MS", 500)
SOLVER_POLL_ATTEMPTS = _env_int("SOLVER_POLL_ATTEMPTS", 100)
SOLVER_HARD_TIMEOUT = max(10, _env_int("SOLVER_HARD_TIMEOUT", 90))
SOLVER_CLEANUP_TIMEOUT = max(1, _env_int("SOLVER_CLEANUP_TIMEOUT", 5))
SOLVER_FAST_CLICK = (os.environ.get("SOLVER_FAST_CLICK", "1").strip().lower() not in ("0", "false", "no"))
SOLVER_MOUSE_CLICK_RETRIES = _env_int("SOLVER_MOUSE_CLICK_RETRIES", 3)
SOLVER_MOUSE_CLICK_INTERVAL_MS = _env_int("SOLVER_MOUSE_CLICK_INTERVAL_MS", 600)
SOLVER_TIMELINE_TRACE = (os.environ.get("SOLVER_TIMELINE_TRACE", "0").strip().lower() in ("1", "true", "yes"))
SOLVER_TIMELINE_SAMPLE = _env_int("SOLVER_TIMELINE_SAMPLE", 8)
_solver_timeline_emitted = 0
_solver_timeline_next_id = 0


def _new_solver_timeline(*, enabled=None):
    global _solver_timeline_next_id
    enabled = SOLVER_TIMELINE_TRACE if enabled is None else enabled
    if not enabled:
        return None
    _solver_timeline_next_id += 1
    return {"start": time.time(), "solve_id": _solver_timeline_next_id, "events": []}


def _trace_solver_event(timeline, event, **fields):
    if timeline is None:
        return
    item = {"t": round(time.time() - timeline["start"], 4), "event": event}
    if "solve_id" in timeline:
        item["solve_id"] = timeline["solve_id"]
    item.update(fields)
    timeline["events"].append(item)


def _new_solver_poll_stats():
    return {
        "poll_attempts": 0,
        "first_token_attempt": None,
        "poll_read_ms_total": 0.0,
        "poll_read_ms_max": 0.0,
        "poll_read_count": 0,
        "poll_retry_click_count": 0,
    }


def _finish_solver_poll_stats(stats):
    if not stats:
        return {}
    read_count = stats.pop("poll_read_count", 0)
    total = stats.pop("poll_read_ms_total", 0.0)
    stats["poll_read_ms_avg"] = round(total / read_count, 1) if read_count else 0.0
    stats["poll_read_ms_max"] = round(stats.get("poll_read_ms_max", 0.0), 1)
    return stats


async def _turnstile_frame_count(p):
    try:
        return await p.evaluate(
            "() => document.querySelectorAll('iframe[src*=turnstile], iframe[src*=challenges.cloudflare.com]').length"
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return 0


async def _turnstile_dom_snapshot(p):
    try:
        return await p.evaluate(
            """() => {
                const clip = (value, n = 96) => String(value || "").slice(0, n);
                const rectInfo = (el) => {
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {
                        x: Math.round(r.left),
                        y: Math.round(r.top),
                        w: Math.round(r.width),
                        h: Math.round(r.height),
                        visible: r.width >= 1 && r.height >= 1
                    };
                };
                const elemInfo = (el) => {
                    if (!el) return null;
                    return {
                        tag: clip(el.tagName),
                        id: clip(el.id, 64),
                        class: clip(el.className, 96),
                        is_iframe: el.tagName === "IFRAME"
                    };
                };
                const urlInfo = (src) => {
                    try {
                        const u = new URL(src || "", location.href);
                        return {host: clip(u.host, 96), path: clip(u.pathname, 96)};
                    } catch (_) {
                        return {host: "", path: ""};
                    }
                };
                const widget = document.querySelector(".cf-turnstile");
                const wr = rectInfo(widget);
                const center = wr ? {
                    x: Math.round(wr.x + wr.w / 2),
                    y: Math.round(wr.y + wr.h / 2)
                } : null;
                const centerEl = center ? document.elementFromPoint(center.x, center.y) : null;
                const iframes = Array.from(document.querySelectorAll("iframe"));
                const iframeSummaries = iframes.slice(0, 8).map((f) => {
                    const info = Object.assign(urlInfo(f.getAttribute("src") || ""), rectInfo(f) || {});
                    info.in_widget = widget ? widget.contains(f) : false;
                    return info;
                });
                const isTurnstileFrame = (f) => {
                    const src = f.getAttribute("src") || "";
                    return src.includes("turnstile") || src.includes("challenges.cloudflare.com");
                };
                const response = document.querySelector('input[name="cf-turnstile-response"]');
                return {
                    __csp_solver_snapshot: true,
                    ready_state: document.readyState,
                    viewport: {w: window.innerWidth, h: window.innerHeight},
                    widget: Object.assign({present: Boolean(widget)}, wr || {}),
                    click_center: center,
                    element_at_center: elemInfo(centerEl),
                    all_iframe_count: iframes.length,
                    turnstile_iframe_count: iframes.filter(isTurnstileFrame).length,
                    iframe_summaries: iframeSummaries,
                    turnstile_loaded: Boolean(window.turnstile),
                    response_input: {
                        present: Boolean(response),
                        token_len: response && response.value ? response.value.length : 0
                    }
                };
            }"""
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return {}


async def _turnstile_page_trace(p):
    try:
        return await p.evaluate(
            "() => window.__cspTurnstileTrace ? Object.assign({}, window.__cspTurnstileTrace) : null"
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


async def _get_solver_page(browser):
    if SOLVER_REUSE:
        async with _solver_lock:
            if _solver_pool:
                item = _solver_pool.pop()
                item["reused"] = True
                item["goto_s"] = 0.0
                return item
    p = await _prepare_browser_page(browser)
    await p.set_viewport_size({"width": 800, "height": 600})
    goto_started = time.time()
    await p.goto(f'{SITE_URL}/sign-up', timeout=20000)
    await p.wait_for_timeout(1000)
    return {"page": p, "n": 0, "reused": False, "goto_s": time.time() - goto_started}

async def _put_solver_page(item, ok):
    p = item["page"]
    item["n"] += 1
    if SOLVER_REUSE and ok and item["n"] < MAX_SOLVER_REUSE:
        try:  # 清理本次注入痕迹,留待复用
            await asyncio.wait_for(
                p.evaluate("document.querySelectorAll('.cf-turnstile').forEach(e=>e.remove());var i=document.querySelector('input[name=\"cf-turnstile-response\"]');if(i)i.remove();"),
                timeout=SOLVER_CLEANUP_TIMEOUT,
            )
            async with _solver_lock:
                _solver_pool.append(item)
            return
        except asyncio.CancelledError:
            await _close_solver_page(item)
            raise
        except Exception:
            pass
    await _close_solver_page(item)


async def _close_solver_page(item):
    """Bound cleanup so a wedged renderer cannot trap the worker in finally."""
    page = item["page"]
    try:
        await asyncio.wait_for(page.close(), timeout=SOLVER_CLEANUP_TIMEOUT)
    except asyncio.CancelledError:
        # Solver cancellation is expected at the hard deadline.  Give cleanup
        # its own bounded task so the page is not returned to the reuse pool.
        cleanup = asyncio.create_task(page.close())
        try:
            await asyncio.wait_for(cleanup, timeout=SOLVER_CLEANUP_TIMEOUT)
        except BaseException:
            cleanup.cancel()
        raise
    except Exception:
        pass

async def _inject_turnstile_widget(p, *, timeline=False):
    if not timeline:
        await p.evaluate(f"""var d=document.createElement('div');d.className='cf-turnstile';d.setAttribute('data-sitekey','{SITE_KEY}');d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';document.body.appendChild(d);function __r(){{window.turnstile&&window.turnstile.render(d,{{sitekey:'{SITE_KEY}',callback:function(t){{var i=document.querySelector('input[name="cf-turnstile-response"]');if(!i){{i=document.createElement('input');i.type='hidden';i.name='cf-turnstile-response';document.body.appendChild(i);}}i.value=t;}}}})}}if(window.turnstile){{__r()}}else{{var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=function(){{setTimeout(__r,1000)}};document.head.appendChild(s);}}""")
        return
    await p.evaluate(f"""var __trace=window.__cspTurnstileTrace={{created_at:performance.now(),script_inserted_at:null,script_loaded_at:null,render_called_at:null,render_returned_at:null,token_written_at:null,token_len:0,error:null}};var d=document.createElement('div');d.className='cf-turnstile';d.setAttribute('data-sitekey','{SITE_KEY}');d.style.cssText='position:fixed;top:10px;left:10px;z-index:99999;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px';document.body.appendChild(d);function __r(){{try{{if(!window.turnstile)return;__trace.render_called_at=performance.now();var __ret=window.turnstile.render(d,{{sitekey:'{SITE_KEY}',callback:function(t){{var i=document.querySelector('input[name="cf-turnstile-response"]');if(!i){{i=document.createElement('input');i.type='hidden';i.name='cf-turnstile-response';document.body.appendChild(i);}}i.value=t;__trace.token_written_at=performance.now();__trace.token_len=t?t.length:0;}}}});__trace.render_returned_at=performance.now();__trace.render_return_type=typeof __ret;}}catch(e){{__trace.error=e&&e.name?e.name:String(e);}}}}if(window.turnstile){{__r()}}else{{var s=document.createElement('script');s.src='https://challenges.cloudflare.com/turnstile/v0/api.js';s.onload=function(){{__trace.script_loaded_at=performance.now();setTimeout(__r,1000)}};__trace.script_inserted_at=performance.now();document.head.appendChild(s);}}""")


async def _has_visible_turnstile_frame(p):
    try:
        return await p.evaluate(
            """() => Array.from(document.querySelectorAll('iframe')).some((f) => {
                const r = f.getBoundingClientRect();
                return r.width >= 20 && r.height >= 20;
            })"""
        )
    except Exception:
        return False


async def _read_turnstile_token(p):
    try:
        return await p.evaluate('document.querySelector("input[name=\\"cf-turnstile-response\\"]")?.value||""')
    except asyncio.CancelledError:
        raise
    except Exception:
        return ""


async def _mouse_click_turnstile_center(p):
    clicked, _trace = await _mouse_click_turnstile_center_trace(p)
    return clicked


async def _mouse_click_turnstile_center_trace(p):
    trace = {}
    started = time.time()
    box = await p.evaluate(
        """() => {
            const e = document.querySelector('.cf-turnstile');
            if (!e) return null;
            const r = e.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2};
        }"""
    )
    trace["box_eval_ms"] = round((time.time() - started) * 1000, 1)
    if not box:
        return False, trace
    x = float(box["x"])
    y = float(box["y"])
    trace["click_x"] = round(x, 1)
    trace["click_y"] = round(y, 1)
    started = time.time()
    await p.mouse.move(max(0, x - 25), max(0, y - 8))
    trace["mouse_move1_ms"] = round((time.time() - started) * 1000, 1)
    started = time.time()
    await p.mouse.move(x, y, steps=8)
    trace["mouse_move2_ms"] = round((time.time() - started) * 1000, 1)
    started = time.time()
    await p.mouse.down()
    trace["mouse_down_ms"] = round((time.time() - started) * 1000, 1)
    await asyncio.sleep(0.05)
    started = time.time()
    await p.mouse.up()
    trace["mouse_up_ms"] = round((time.time() - started) * 1000, 1)
    return True, trace


async def _repeat_mouse_click_turnstile(p, *, timeline=None):
    retries = max(0, SOLVER_MOUSE_CLICK_RETRIES)
    if retries <= 0:
        return False
    clicked = False
    interval = max(50, SOLVER_MOUSE_CLICK_INTERVAL_MS) / 1000.0
    for i in range(retries):
        token = await _read_turnstile_token(p)
        dom = await _turnstile_dom_snapshot(p) if timeline is not None else None
        iframe_count = dom.get("turnstile_iframe_count", 0) if dom else 0
        _trace_solver_event(
            timeline, "click_before", attempt=i + 1,
            token_len=len(token or ""), iframe_count=iframe_count, dom=dom
        )
        if token and len(token) > 10:
            return clicked
        click_started = time.time()
        click_error = None
        try:
            if timeline is not None:
                attempt_clicked, click_trace = await _mouse_click_turnstile_center_trace(p)
            else:
                attempt_clicked = await _mouse_click_turnstile_center(p)
                click_trace = {}
            clicked = attempt_clicked or clicked
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            click_error = type(exc).__name__
            attempt_clicked = False
            click_trace = {}
        click_call_ms = round((time.time() - click_started) * 1000, 1)
        token = await _read_turnstile_token(p) if timeline is not None else ""
        dom = await _turnstile_dom_snapshot(p) if timeline is not None else None
        iframe_count = dom.get("turnstile_iframe_count", 0) if dom else 0
        _trace_solver_event(
            timeline, "click_after", attempt=i + 1, clicked=clicked,
            attempt_clicked=attempt_clicked,
            token_len=len(token or ""), iframe_count=iframe_count,
            click_call_ms=click_call_ms, click_error=click_error,
            click_trace=click_trace, dom=dom
        )
        if i != retries - 1:
            await asyncio.sleep(interval)
    return clicked


async def _click_turnstile_if_possible(p, *, fast=False, timeline=None):
    if SOLVER_MOUSE_CLICK_RETRIES > 0:
        return await _repeat_mouse_click_turnstile(p, timeline=timeline)
    visible = await _has_visible_turnstile_frame(p)
    _trace_solver_event(timeline, "visible_check", visible=visible)
    if fast and not visible:
        return visible
    click_timeout = 500 if fast else 3000
    for sel in ["iframe[src*='challenges.cloudflare.com']","iframe[src*='turnstile']",".cf-turnstile iframe"]:
        try:
            fr = p.frame_locator(sel).first
            await fr.locator("#checkbox, .checkbox, input[type=checkbox], body").first.click(timeout=click_timeout)
            break
        except asyncio.CancelledError:
            raise
        except Exception:
            continue
    return visible


async def _poll_turnstile_token(p, *, stats=None):
    for i in range(SOLVER_POLL_ATTEMPTS):
        await asyncio.sleep(max(50, SOLVER_POLL_INTERVAL_MS) / 1000)
        if stats is not None:
            stats["poll_attempts"] = i + 1
            read_started = time.time()
        try:
            t = await _read_turnstile_token(p)
            if stats is not None:
                read_ms = (time.time() - read_started) * 1000
                stats["poll_read_count"] += 1
                stats["poll_read_ms_total"] += read_ms
                stats["poll_read_ms_max"] = max(stats["poll_read_ms_max"], read_ms)
            if t and len(t) > 10:
                if stats is not None and stats["first_token_attempt"] is None:
                    stats["first_token_attempt"] = i + 1
                return t
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        retry_every = max(1, int(10000 / max(50, SOLVER_POLL_INTERVAL_MS)))
        if i > 0 and i % retry_every == 0:
            try:
                await p.locator(".cf-turnstile").first.click(timeout=1000)
                if stats is not None:
                    stats["poll_retry_click_count"] += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
    return None


async def _early_poll_turnstile_token(p, *, attempts=None, interval_ms=None):
    """Short pre-click poll: managed/non-interactive widgets often write token without a click."""
    attempts = SOLVER_EARLY_POLL_ATTEMPTS if attempts is None else attempts
    interval_ms = SOLVER_EARLY_POLL_MS if interval_ms is None else interval_ms
    if attempts <= 0:
        return None
    for _ in range(attempts):
        await asyncio.sleep(max(50, interval_ms) / 1000.0)
        token = await _read_turnstile_token(p)
        if token and len(token) > 10:
            return token
    return None


async def _start_turnstile_challenge(browser, *, fast_click=False):
    global _solver_timeline_emitted
    item = await _get_solver_page(browser)
    p = item["page"]
    trace_timeline = SOLVER_TIMELINE_TRACE and _solver_timeline_emitted < SOLVER_TIMELINE_SAMPLE
    if trace_timeline:
        _solver_timeline_emitted += 1
    timeline = _new_solver_timeline(enabled=trace_timeline)
    trace = {
        "goto_s": item.get("goto_s", 0.0),
        "reused": bool(item.get("reused", False)),
        "reuse_count": item.get("n", 0),
        "inject_s": 0.0,
        "initial_s": 0.0,
        "click_s": 0.0,
        "wait_s": 0.0,
        "visible_frame": False,
        "early_token": False,
        "click_skipped": False,
    }
    item["trace"] = trace
    item["timeline"] = timeline
    item["early_token"] = None
    try:
        stage_started = time.time()
        _trace_solver_event(timeline, "inject_start")
        await _inject_turnstile_widget(p, timeline=timeline is not None)
        trace["inject_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "inject_done")
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_inject",
                page_trace=await _turnstile_page_trace(p)
            )
        stage_started = time.time()
        await p.wait_for_timeout(SOLVER_INITIAL_WAIT_MS)
        trace["initial_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "initial_done")
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_initial",
                page_trace=await _turnstile_page_trace(p)
            )
        # Managed / already-solved widgets: skip the ~2.4s forced click chain.
        early = await _early_poll_turnstile_token(p)
        if early:
            item["early_token"] = early
            trace["early_token"] = True
            trace["click_skipped"] = True
            trace["click_s"] = 0.0
            _trace_solver_event(timeline, "early_token", token_len=len(early))
            return item
        stage_started = time.time()
        trace["visible_frame"] = await _click_turnstile_if_possible(
            p, fast=fast_click, timeline=timeline
        )
        trace["click_s"] = time.time() - stage_started
        _trace_solver_event(timeline, "click_stage_done", clicked=bool(trace["visible_frame"]))
        if timeline is not None:
            _trace_solver_event(
                timeline, "page_trace_after_click",
                page_trace=await _turnstile_page_trace(p)
            )
        return item
    except BaseException:
        await _put_solver_page(item, False)
        raise


async def _wait_turnstile_challenge(item):
    ok = False
    try:
        wait_started = time.time()
        timeline = item.get("timeline")
        early = item.get("early_token")
        if early:
            item.get("trace", {})["wait_s"] = time.time() - wait_started
            ok = True
            _trace_solver_event(timeline, "poll_done", ok=True, token_len=len(early), early=True)
            return early
        poll_stats = _new_solver_poll_stats() if timeline is not None else None
        _trace_solver_event(timeline, "poll_start")
        token = await _poll_turnstile_token(item["page"], stats=poll_stats)
        item.get("trace", {})["wait_s"] = time.time() - wait_started
        ok = token is not None
        page_trace = await _turnstile_page_trace(item["page"]) if timeline is not None else None
        _trace_solver_event(
            timeline, "poll_done", ok=ok, token_len=len(token or ""),
            page_trace=page_trace, **_finish_solver_poll_stats(poll_stats)
        )
        return token
    except asyncio.CancelledError:
        raise
    except Exception:
        return None
    finally:
        timeline = item.get("timeline")
        if timeline is not None:
            debug_log("[solver_timeline] " + json.dumps(timeline["events"], separators=(",", ":")))
        await _put_solver_page(item, ok)


async def solve_one_turnstile(browser):
    token, _trace = await solve_one_turnstile_with_trace(browser)
    return token


async def solve_one_turnstile_external():
    """Mint via TURNSTILE_PROVIDER without browser / Physical_Sem."""
    loop = asyncio.get_event_loop()
    token, trace = await loop.run_in_executor(
        POLL_EXECUTOR,
        lambda: turnstile_provider_mod.mint_turnstile_token(SITE_KEY),
    )
    return token, trace


async def solve_one_turnstile_with_trace(browser):
    if not TURNSTILE_USES_BROWSER:
        return await solve_one_turnstile_external()
    item = await _start_turnstile_challenge(browser, fast_click=SOLVER_FAST_CLICK)
    token = await _wait_turnstile_challenge(item)
    return token, item.get("trace", {})


def _record_solver_trace(metrics, trace, total_seconds, token):
    metrics.t_solve_count += 1
    metrics.t_solve_seconds += total_seconds
    if token is None:
        metrics.t_solve_failed += 1
    if not trace:
        return
    metrics.solver_goto_seconds += trace.get("goto_s", 0.0)
    metrics.solver_inject_seconds += trace.get("inject_s", 0.0)
    metrics.solver_initial_seconds += trace.get("initial_s", 0.0)
    metrics.solver_click_seconds += trace.get("click_s", 0.0)
    metrics.solver_wait_seconds += trace.get("wait_s", 0.0)
    if trace.get("reused"):
        metrics.solver_reused_count += 1
    if trace.get("visible_frame"):
        metrics.solver_visible_frame_count += 1


# ──────────────────────────────────────────────
#  邮箱服务:custom(自建 webhook) / tempmail(免费临时邮箱,多 provider fallback)
# ──────────────────────────────────────────────
# 免 key 的公共临时邮箱 provider(实测可用,互为 fallback 消灭单点):
#  - 独立 API:tempmail.lol  （x.ai Create 常过；优先且多试）
#  - mail.tm 同协议:mail.tm / mail.gw / duckmail.sbs（域易被 ban，仅作兜底）
# handle 编码 provider,供 poll_code 分派;新增 provider 只要在这两处加一段即可。
TEMPMAIL_BASES = ["https://api.mail.tm", "https://api.mail.gw", "https://api.duckmail.sbs"]
# x.ai CreateEmailValidationCode domain wall — never hand these to P.
# Extend via TEMPMAIL_BANNED_DOMAINS=a.com,b.com
_TEMPMAIL_BANNED_DEFAULT = (
    "duckmail.sbs",
    "web-library.net",
    "mail.tm",
    "mail.gw",
    "baldur.edu.kg",  # duckmail edu 壳，Create 实测 grpc=3
)
TEMPMAIL_BANNED_DOMAINS = {
    d.strip().lower()
    for d in (
        ",".join(_TEMPMAIL_BANNED_DEFAULT)
        + ","
        + (os.environ.get("TEMPMAIL_BANNED_DOMAINS") or "")
    ).split(",")
    if d.strip()
}
# lol free tier 并发易 429；串行+间隔+长退避，绝不当机切到已 ban 的 mail.tm。
TEMPMAIL_LOL_RETRIES = max(1, min(20, _env_int("TEMPMAIL_LOL_RETRIES", 8)))
TEMPMAIL_LOL_MIN_INTERVAL_MS = max(0, min(5000, _env_int("TEMPMAIL_LOL_MIN_INTERVAL_MS", 400)))
_TEMPMAIL_LOL_LOCK = threading.Lock()
_TEMPMAIL_LOL_NEXT_OK = 0.0


def _email_domain(email: str) -> str:
    if not email or "@" not in email:
        return ""
    return email.rsplit("@", 1)[-1].strip().lower()


def _domain_banned(email_or_domain: str) -> bool:
    dom = email_or_domain.strip().lower()
    if "@" in dom:
        dom = _email_domain(dom)
    if not dom:
        return True
    if dom in TEMPMAIL_BANNED_DOMAINS:
        return True
    # also match parent labels listed in ban set (e.g. ban mail.tm hits a.mail.tm)
    parts = dom.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in TEMPMAIL_BANNED_DOMAINS:
            return True
    return False


def _extract_code(text):
    """多层兜底提取验证码,抗邮件模板变化。"""
    for pat in (r'>([A-Z0-9]{3}-[A-Z0-9]{3})<', r'>([A-Z0-9]{6})<', r'\b([A-Z0-9]{3}-?[A-Z0-9]{3})\b'):
        m = re.search(pat, text)
        if m:
            return m.group(1).replace('-', '')
    return None

def _mailtm_create(base, password):
    """mail.tm 同协议建箱;返回 (handle, email)。跳过 x.ai 已知 ban 域。"""
    d = req.get(f'{base}/domains', timeout=12).json()
    d = d.get('hydra:member', d) if isinstance(d, dict) else d
    if not isinstance(d, list):
        d = []
    doms = [
        x['domain']
        for x in d
        if isinstance(x, dict)
        and x.get('isActive', True)
        and not x.get('isPrivate', False)
        and not _domain_banned(str(x.get('domain') or ''))
    ]
    if not doms:
        raise RuntimeError('no domain')
    # 打散，别永远钉第一个（常是 web-library.net）
    random.shuffle(doms)
    last_err = 'no domain'
    for dom in doms[:6]:
        email = f'oc{secrets.token_hex(5)}@{dom}'
        try:
            req.post(f'{base}/accounts', json={'address': email, 'password': password}, timeout=12)
            tok = req.post(f'{base}/token', json={'address': email, 'password': password}, timeout=12).json().get('token', '')
            if not tok:
                last_err = 'no token'
                continue
            return f'mt|{base}|{tok}', email
        except Exception as exc:
            last_err = str(exc)[:80]
            continue
    raise RuntimeError(f'mailtm create failed: {last_err}')

def _lol_create():
    """tempmail.lol 建箱;返回 (handle, email)。进程内串行+最小间隔，抗 free-tier 429。"""
    global _TEMPMAIL_LOL_NEXT_OK
    with _TEMPMAIL_LOL_LOCK:
        now = time.monotonic()
        wait = _TEMPMAIL_LOL_NEXT_OK - now
        if wait > 0:
            time.sleep(wait)
        r = req.post('https://api.tempmail.lol/v2/inbox/create', timeout=12)
        # success is 200 or 201
        try:
            data = r.json() if r.content else {}
        except Exception as exc:
            _TEMPMAIL_LOL_NEXT_OK = time.monotonic() + 1.0
            raise RuntimeError(f'lol non-json status={r.status_code}: {exc}') from exc
        if r.status_code == 429 or (isinstance(data, dict) and 'rate limit' in str(data).lower()):
            # free tier; backoff 3–12s under lock so all P workers share the cool-down
            retry_after = 0.0
            try:
                retry_after = float(r.headers.get('Retry-After') or 0)
            except Exception:
                retry_after = 0.0
            cool = max(3.0, min(12.0, retry_after or 5.0))
            _TEMPMAIL_LOL_NEXT_OK = time.monotonic() + cool
            raise RuntimeError(f'lol rate limited status={r.status_code} cool={cool:.1f}s')
        addr = (data or {}).get('address', '') if isinstance(data, dict) else ''
        tok = (data or {}).get('token', '') if isinstance(data, dict) else ''
        if not addr or not tok:
            _TEMPMAIL_LOL_NEXT_OK = time.monotonic() + 0.8
            raise RuntimeError(f'lol create failed status={r.status_code} body={str(data)[:80]}')
        if _domain_banned(addr):
            _TEMPMAIL_LOL_NEXT_OK = time.monotonic() + (TEMPMAIL_LOL_MIN_INTERVAL_MS / 1000.0)
            raise RuntimeError(f'lol domain banned: {_email_domain(addr)}')
        _TEMPMAIL_LOL_NEXT_OK = time.monotonic() + (TEMPMAIL_LOL_MIN_INTERVAL_MS / 1000.0)
        return f'lol|{tok}', addr

def create_email():
    """custom 用自建域名(本地 webhook);tempmail 优先 lol（串行抗 429），mail.tm 仅未 ban 域兜底。"""
    if EMAIL_MODE == 'custom':
        email = f'oc{secrets.token_hex(5)}@{EMAIL_DOMAIN}'
        password = rand_str()
        return email, email, password  # 地址即用,验证码经 CF Worker POST 到本地 webhook

    password = rand_str()
    # 1) tempmail.lol — Create 实测常 grpc=0；free tier 429 时在锁内 cool-down 再试。
    last_lol_err = ''
    for attempt in range(TEMPMAIL_LOL_RETRIES):
        try:
            handle, email = _lol_create()
            return handle, email, password
        except Exception as exc:
            last_lol_err = str(exc)[:120]
            # 429 cool-down already slept under lock; small extra jitter only
            if attempt + 1 < TEMPMAIL_LOL_RETRIES:
                time.sleep(0.05 * (attempt + 1))
            continue
    # 2) mail.tm 族兜底（过滤 ban 域；当前多数域 Create grpc=3，仅作最后手段）
    for base in TEMPMAIL_BASES:
        try:
            handle, email = _mailtm_create(base, password)
            return handle, email, password
        except Exception:
            continue
    raise RuntimeError(f'所有临时邮箱 provider 均不可用 (lol: {last_lol_err})')
def _tempmail_fetch(handle):
    """按 handle 前缀分派,取该邮箱当前邮件全文(subject+text+html);无则 None。"""
    kind = handle.split('|', 1)[0]
    if kind == 'lol':
        tok = handle.split('|', 1)[1]
        data = req.get(f'https://api.tempmail.lol/v2/inbox?token={tok}', timeout=10).json()
        items = data.get('emails') or data.get('messages') or []
        if not items:
            return None
        return '\n'.join(f"{i.get('subject','')}\n{i.get('body','')}\n{i.get('html','')}"
                         for i in items if isinstance(i, dict))
    # mail.tm 同协议:handle = "mt|base|token"
    _, base, tok = handle.split('|', 2)
    hdr = {'Accept': 'application/json', 'Authorization': f'Bearer {tok}'}
    data = req.get(f'{base}/messages', headers=hdr, timeout=10).json()
    msgs = data if isinstance(data, list) else data.get('hydra:member', [])
    if not msgs:
        return None
    mid = str(msgs[0].get('id') or '')
    detail = req.get(f'{base}/messages/{mid}', headers=hdr, timeout=10).json()
    parts = [str(detail.get(k, '')) for k in ['subject', 'intro', 'text', 'html']]
    if isinstance(detail.get('html'), list):
        parts.append('\n'.join(str(x) for x in detail['html']))
    return '\n'.join(parts)

def poll_code(handle, max_wait=90):
    """轮询验证码:custom 查本地 webhook /check;tempmail 按 provider 取信。"""
    if EMAIL_MODE == 'custom':
        for _ in range(max_wait):
            time.sleep(1)
            try:
                resp = req.get(f'{LOCAL_EMAIL_API}/check/{handle}', timeout=5)
                if resp.status_code == 200 and resp.json().get('code'):
                    return resp.json()['code']
            except Exception:
                pass
        return None

    for _ in range(max_wait):
        time.sleep(1)
        try:
            text = _tempmail_fetch(handle)
            if text:
                code = _extract_code(text)
                if code:
                    return code
        except Exception:
            pass
    return None


async def _create_email_async(loop):
    """在线程池中创建邮箱,避免阻塞 asyncio 事件循环。"""
    return await loop.run_in_executor(POLL_EXECUTOR, create_email)


async def _poll_code_async(loop, handle):
    """在线程池中轮询验证码。"""
    return await loop.run_in_executor(POLL_EXECUTOR, poll_code, handle)


async def _acquire_many(sem, count):
    """一次预留多个许可；取消或异常时回滚已获取许可。"""
    acquired = 0
    try:
        for _ in range(count):
            await sem.acquire()
            acquired += 1
        return acquired
    except BaseException:
        for _ in range(acquired):
            sem.release()
        raise


class _NoopAsyncSemaphore:
    async def acquire(self):
        return True

    def release(self):
        return None


async def _send_q_request_batch(browser, physical_sem, p_send_sem, requests, metrics=None):
    """发送一批 CreateEmailValidationCode。

    PROTOCOL_HTTP=1：curl_cffi，不占 Physical_Sem / 浏览器页。
    默认：单页 batch 发码，持有 Physical_Sem 仅在发码阶段。
    返回每个请求的 sent 状态。等 OTP 不在此函数内。
    """
    # ── 纯 HTTP 路径：无 page、无 physical_sem；池内并行 Create ──
    if PROTOCOL_HTTP:
        p_send_acquired = False
        await p_send_sem.acquire()
        p_send_acquired = True
        try:
            # 预热 pool / clearance（失败则整批 sent=False）
            try:
                await _get_http_client()
            except asyncio.CancelledError:
                raise
            except Exception:
                return [{**item, "sent": False} for item in requests]
            send_started = time.time()

            async def _one(item):
                sent = False
                try:
                    sent = await grpc_create_code(None, item["email"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    sent = False
                return {**item, "sent": sent}

            # Parallel Create across pool slots (was sequential → P wall under load).
            results = list(await asyncio.gather(*[_one(it) for it in requests]))
            if metrics is not None:
                metrics.p_send_count += 1
                metrics.p_send_seconds += time.time() - send_started
            return results
        finally:
            if p_send_acquired:
                p_send_sem.release()

    p_send_acquired = False
    physical_acquired = False
    physical_wait_started = None
    physical_hold_started = None
    page = None
    await p_send_sem.acquire()
    p_send_acquired = True
    try:
        physical_wait_started = time.time()
        await physical_sem.acquire()
        physical_acquired = True
        physical_hold_started = time.time()
        if metrics is not None:
            metrics.p_physical_count += 1
            metrics.p_physical_wait_seconds += physical_hold_started - physical_wait_started
        page = await _prepare_browser_page(browser)
        await page.set_viewport_size({"width": 800, "height": 600})
        try:
            stage_started = time.time()
            await _prepare_signup_page(page, redirect=True, timeout=30000)
            if metrics is not None:
                metrics.p_page_prepare_count += 1
                metrics.p_page_prepare_seconds += time.time() - stage_started
        except asyncio.CancelledError:
            raise
        except Exception:
            return [{**item, "sent": False} for item in requests]

        results = []
        send_started = time.time()
        for item in requests:
            sent = False
            try:
                sent = await grpc_create_code(page, item["email"])
            except asyncio.CancelledError:
                raise
            except Exception:
                sent = False
            results.append({**item, "sent": sent})
        if metrics is not None:
            metrics.p_send_count += 1
            metrics.p_send_seconds += time.time() - send_started
        return results
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        if physical_acquired:
            if metrics is not None and physical_hold_started is not None:
                metrics.p_physical_hold_seconds += time.time() - physical_hold_started
            physical_sem.release()
        if p_send_acquired:
            p_send_sem.release()


async def _poll_and_admit_q(
    request,
    inventory,
    q_pending_sem,
    q_slot_sem,
    metrics,
    *,
    q_batch_lease=None,
    admission_gate=None,
):
    """等待单个 Q 返回并入库；每个请求独立释放 pending/inflight。"""
    loop = asyncio.get_event_loop()
    release_reservation = True
    try:
        try:
            code = await asyncio.wait_for(
                _poll_code_async(loop, request["handle"]),
                timeout=P_REQUEST_TIMEOUT,
            )
        except asyncio.CancelledError:
            # poll_code 仍在运行时取消，底层轮询可能继续持有该请求；此处
            # 不能提前归还 pending/inflight，否则新请求会超量进入。
            release_reservation = False
            raise
        except asyncio.TimeoutError:
            code = None

        if code is None:
            metrics.q_discarded += 1
            return False

        metrics.q_returned += 1
        returned_at = time.time()
        q_env = None
        try:
            q_env = await ResourceEnvelope.create_with_slot(
                'Q',
                {
                    'email': request["email"],
                    'password': request["password"],
                    'code': code,
                },
                q_slot_sem,
                expires_at=returned_at + Q_MAX_AGE,
            )
            await inventory.put_q(q_env)
            debug_log('[P] verification code admitted')
            return True
        except asyncio.CancelledError:
            if q_env is not None and not q_env.released:
                q_env.discard()
            raise
        except Exception:
            if q_env is not None and not q_env.released:
                q_env.discard()
            metrics.q_discarded += 1
            return False
    finally:
        if release_reservation:
            # poll 已终止（含超时/网络/解析异常），或 Q 已返回后任务在
            # 入库阶段取消：请求所有权已经回到本协程，必须归还许可。
            q_pending_sem.release()
            if q_batch_lease is not None:
                await q_batch_lease.release_one()
            if admission_gate is not None:
                await admission_gate.notify_changed()


def _observe_background_task(task):
    """Consume detached task failures so background settlement is not silent."""
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as e:
        debug_log(f'[P] background settle err: {sanitize_terminal_error(e)}')


# ──────────────────────────────────────────────
#  CSP Worker
# ──────────────────────────────────────────────

class _CHotPageLease:
    def __init__(self, browser, metrics=None):
        self.browser = browser
        self.metrics = metrics
        self.context = None
        self.page = None

    async def __aenter__(self):
        started = time.time()
        try:
            self.context, self.page = await _acquire_c_page(self.browser, self.metrics)
            return self.page
        finally:
            if self.metrics is not None:
                self.metrics.c_page_acquire_count += 1
                self.metrics.c_page_acquire_seconds += time.time() - started

    async def __aexit__(self, exc_type, exc, tb):
        await _release_c_page(self.context, self.page, healthy=exc_type is None)
        self.context = None
        self.page = None
        return False


_c_hot_page_pool = []
_c_hot_page_lock = asyncio.Lock()
_c_hot_page_pool_size = derive_c_hot_page_pool_size(PHYSICAL_CAP or 1, C_WORKERS or 1)


async def _new_c_hot_page(browser):
    context = await _prepare_browser_context(browser)
    page = await context.new_page()
    await page.set_viewport_size({"width": 800, "height": 600})
    await _prepare_signup_page(page, redirect=True, timeout=30000)
    return context, page


async def _acquire_c_page(browser, metrics=None):
    if C_HOT_PAGE_POOL:
        async with _c_hot_page_lock:
            if _c_hot_page_pool:
                if metrics is not None:
                    metrics.c_hot_page_hits += 1
                return _c_hot_page_pool.pop()
        if metrics is not None:
            metrics.c_hot_page_misses += 1
        return await _new_c_hot_page(browser)

    page = await _prepare_browser_page(browser)
    await page.set_viewport_size({"width": 800, "height": 600})
    await _prepare_signup_page(page, redirect=True, timeout=30000)
    return None, page


async def _release_c_page(context, page, *, healthy):
    if not C_HOT_PAGE_POOL or context is None:
        try:
            await page.close()
        except Exception:
            pass
        return

    if healthy:
        try:
            if "/sign-up" not in (getattr(page, "url", "") or ""):
                healthy = False
            else:
                await context.clear_cookies()
                await page.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
                )
                async with _c_hot_page_lock:
                    if len(_c_hot_page_pool) < _c_hot_page_pool_size:
                        _c_hot_page_pool.append((context, page))
                        return
        except asyncio.CancelledError:
            try:
                await context.close()
            except Exception:
                pass
            raise
        except Exception:
            pass

    try:
        await context.close()
    except Exception:
        try:
            await page.close()
        except Exception:
            pass


def _c_page_lease(browser, metrics=None):
    return _CHotPageLease(browser, metrics)


async def _close_c_hot_page_pool():
    async with _c_hot_page_lock:
        items = list(_c_hot_page_pool)
        _c_hot_page_pool.clear()
    for context, page in items:
        try:
            await context.close()
        except Exception:
            try:
                await page.close()
            except Exception:
                pass


async def s_worker(wid, browser, inventory, physical_sem, t_slot_sem, metrics, admission_gate=None):
    """S_Worker: 生成 T 并入库。

    browser provider：占 Physical_Sem + Playwright page。
    lite/capsolver/http provider：纯 HTTP mint，不占 Physical_Sem（突破本地浏览器天花板）。
    """
    # Back off hard on TargetClosed / browser thrash so S doesn't spin 4k fails/min.
    fail_streak = 0
    while not STOP.is_set():
        t_lease = None
        try:
            if admission_gate is not None:
                t_lease = await admission_gate.acquire_t_production()

            need_physical = TURNSTILE_USES_BROWSER
            physical_wait_started = time.time()
            physical_hold_started = None
            if need_physical:
                await physical_sem.acquire()
                physical_hold_started = time.time()
                metrics.s_physical_count += 1
                metrics.s_physical_wait_seconds += physical_hold_started - physical_wait_started
            token = None
            trace = {}
            solve_started = time.time()
            thrash = False
            try:
                try:
                    token, trace = await asyncio.wait_for(
                        solve_one_turnstile_with_trace(browser),
                        timeout=SOLVER_HARD_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    thrash = True
                    debug_log(f'[S] {wid} solver timeout after {SOLVER_HARD_TIMEOUT}s')
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    msg = sanitize_terminal_error(exc)
                    thrash = "TargetClosed" in msg or "Target closed" in msg or "browser has been closed" in msg
                    debug_log(f'[S] {wid} solver error: {msg}')
            finally:
                solve_elapsed = time.time() - solve_started
                _record_solver_trace(metrics, trace, solve_elapsed, token)
                if need_physical and physical_hold_started is not None:
                    metrics.s_physical_hold_seconds += time.time() - physical_hold_started
                    physical_sem.release()

            if token is None:
                metrics.t_discarded += 1
                if thrash:
                    fail_streak = min(fail_streak + 1, 8)
                else:
                    fail_streak = min(fail_streak + 1, 4)
                if t_lease is not None:
                    await t_lease.release()
                    t_lease = None
                # 0.5 → ~8s on thrash streak; stops 4k TargetClosed/min death spiral
                await asyncio.sleep(0.5 * (2 ** max(0, fail_streak - 1)))
                continue

            fail_streak = 0
            metrics.t_produced += 1
            now = time.time()
            t_env = None
            try:
                t_env = await ResourceEnvelope.create_with_slot(
                    'T', token, t_slot_sem, expires_at=now + T_MAX_AGE
                )
                await inventory.put_t(t_env)
                if admission_gate is not None:
                    await admission_gate.notify_changed()
            except asyncio.CancelledError:
                if t_env is not None and not t_env.released:
                    t_env.discard()
                metrics.t_discarded += 1
                raise
            except Exception:
                if t_env is not None and not t_env.released:
                    t_env.discard()
                metrics.t_discarded += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # A single browser/page failure must not terminate the permanent
            # producer.  The next iteration acquires a fresh solver page.
            metrics.t_discarded += 1
            fail_streak = min(fail_streak + 1, 8)
            debug_log(f'[S] {wid} worker error: {sanitize_terminal_error(exc)}')
            await asyncio.sleep(0.5 * (2 ** max(0, fail_streak - 1)))
        finally:
            if t_lease is not None:
                await t_lease.release()
        await asyncio.sleep(0.15)


async def p_worker(
    wid,
    browser,
    inventory,
    physical_sem,
    q_pending_sem,
    q_slot_sem,
    metrics,
    admission_gate=None,
    p_send_sem=None,
    max_batch=1,
):
    """P_Worker: 创建邮箱 + 发码 + 轮询 + 入库。"""
    loop = asyncio.get_event_loop()
    p_send_sem = p_send_sem or _NoopAsyncSemaphore()
    while not STOP.is_set():
        q_lease = None
        pending_owned = 0
        settle_tasks = []
        try:
            if admission_gate is not None:
                q_lease = await admission_gate.acquire_q_batch(max_batch=max_batch)
                batch_count = q_lease.count
            else:
                batch_count = 1

            pending_owned = await _acquire_many(q_pending_sem, batch_count)

            requests = []
            for _ in range(batch_count):
                email_started = time.time()
                try:
                    handle, email, password = await _create_email_async(loop)
                    metrics.p_email_create_count += 1
                    metrics.p_email_create_seconds += time.time() - email_started
                    requests.append({"handle": handle, "email": email, "password": password})
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    metrics.p_email_create_count += 1
                    metrics.p_email_create_seconds += time.time() - email_started
                    debug_log(f'[P] {wid} create email err: {sanitize_terminal_error(e)}')
                    metrics.q_discarded += 1
                    q_pending_sem.release()
                    pending_owned -= 1
                    if q_lease is not None:
                        await q_lease.release_one()

            if not requests:
                continue

            results = await _send_q_request_batch(
                browser, physical_sem, p_send_sem, requests, metrics
            )
            metrics.q_send_batches += 1
            metrics.q_send_batch_items += len(results)

            for item in results:
                if not item["sent"]:
                    metrics.q_discarded += 1
                    q_pending_sem.release()
                    pending_owned -= 1
                    if q_lease is not None:
                        await q_lease.release_one()
                    continue

                metrics.q_sent += 1
                pending_owned -= 1
                task = asyncio.create_task(
                    _poll_and_admit_q(
                        item,
                        inventory,
                        q_pending_sem,
                        q_slot_sem,
                        metrics,
                        q_batch_lease=q_lease,
                        admission_gate=admission_gate,
                    )
                )
                task.add_done_callback(_observe_background_task)
                settle_tasks.append(task)

            if settle_tasks:
                await asyncio.gather(*(asyncio.shield(task) for task in settle_tasks))

        except asyncio.CancelledError:
            for _ in range(pending_owned):
                q_pending_sem.release()
                if q_lease is not None:
                    await q_lease.release_one()
            raise
        except Exception as e:
            debug_log(f'[P] {wid} err: {sanitize_terminal_error(e)}')
            metrics.q_discarded += 1
            for _ in range(pending_owned):
                q_pending_sem.release()
                if q_lease is not None:
                    await q_lease.release_one()
        await asyncio.sleep(0.2)


def _pair_is_expired(pair, now=None):
    """检查已 claim 的 T/Q 是否在等待期间失效。"""
    now = time.time() if now is None else now
    return any(
        bool(check(now))
        for envelope in (pair.t, pair.q)
        if (check := getattr(envelope, "is_expired", None)) is not None
    )


def _append_registration_line(path, line, mode=None, *, durable=False):
    with open(path, "a") as stream:
        stream.write(line)
        if durable:
            stream.flush()
            os.fsync(stream.fileno())
    if mode is not None:
        os.chmod(path, mode)


def _persist_registration(email, password, sso, session_cookies):
    if session_cookies:
        document = json.dumps(
            {"email": email, "cookies": session_cookies},
            separators=(",", ":"),
        )
        _append_registration_line(
            "keys/auth-sessions.jsonl",
            document + "\n",
            mode=0o600,
            durable=True,
        )
    _append_registration_line("keys/accounts.txt", f"{email}:{password}:{sso}\n")
    _append_registration_line("keys/grok.txt", sso + "\n")


async def _consume_pair(
    browser,
    physical_sem,
    pair,
    metrics,
    task_id=None,
    recovery_probe=None,
):
    """执行一次 C 消费。返回 True 表示业务成功,False 表示消费失败。"""
    global success_count
    email = pair.q.value['email']
    password = pair.q.value['password']
    code = pair.q.value['code']
    token = pair.t.value

    # c_worker 在单次消费超时开始前完成冷却等待。保留这里的默认路径，
    # 供直接调用者使用，同时避免把 60 秒冷却计入 60 秒消费超时。
    if recovery_probe is None:
        recovery_probe = await REGISTRATION_RATE_LIMIT_CIRCUIT.wait()

    # 直接调用路径也可能在 circuit.wait() 中跨过资源有效期。尚未开始
    # 注册时只让出探针，不把本地过期误判成一次恢复探测失败。
    if _pair_is_expired(pair):
        if recovery_probe:
            REGISTRATION_RATE_LIMIT_CIRCUIT.release_probe(recovery_probe)
        return False

    physical_acquired = False
    physical_hold_started = None
    recovery_completed = False
    try:
        # PROTOCOL_HTTP: C 不占浏览器/Physical_Sem，只吃 T+Q 库存。
        use_http = PROTOCOL_HTTP
        if not use_http:
            physical_wait_started = time.time()
            await physical_sem.acquire()
            physical_acquired = True
            physical_hold_started = time.time()
            metrics.c_physical_count += 1
            metrics.c_physical_wait_seconds += physical_hold_started - physical_wait_started
        t0 = time.time()
        try:
            if use_http:
                sso = None
                session_cookies = []
                verify_started = time.time()
                try:
                    verified = await grpc_verify_code(None, email, code)
                finally:
                    metrics.c_verify_count += 1
                    metrics.c_verify_seconds += time.time() - verify_started
                if (
                    verified
                    and not _pair_is_expired(pair)
                    and REGISTRATION_RATE_LIMIT_CIRCUIT.can_submit(recovery_probe)
                ):
                    register_started = time.time()
                    try:
                        registration = await server_action_register(
                            None,
                            email,
                            password,
                            code,
                            token,
                            include_session=True,
                        )
                        if registration:
                            sso, session_cookies = registration
                    finally:
                        metrics.c_register_count += 1
                        metrics.c_register_seconds += time.time() - register_started
            else:
                async with _c_page_lease(browser, metrics) as page:
                    sso = None
                    session_cookies = []
                    verify_started = time.time()
                    try:
                        verified = await grpc_verify_code(page, email, code)
                    finally:
                        metrics.c_verify_count += 1
                        metrics.c_verify_seconds += time.time() - verify_started
                    # 限流可能由另一项并发任务在本任务验证码校验期间触发。
                    # 非当前 probe 不得在 circuit 打开后开始新的注册提交；同时
                    # 避免使用校验过程中刚过期的 T/Q。
                    if (
                        verified
                        and not _pair_is_expired(pair)
                        and REGISTRATION_RATE_LIMIT_CIRCUIT.can_submit(recovery_probe)
                    ):
                        register_started = time.time()
                        try:
                            registration = await server_action_register(
                                page,
                                email,
                                password,
                                code,
                                token,
                                include_session=True,
                            )
                            if registration:
                                sso, session_cookies = registration
                        finally:
                            metrics.c_register_count += 1
                            metrics.c_register_seconds += time.time() - register_started
        except RegistrationRateLimited:
            if REGISTRATION_RATE_LIMIT_CIRCUIT.trip():
                log(
                    format_user_registration_event(
                        "rate_limited",
                        wait_seconds=REGISTRATION_RATE_LIMIT_CIRCUIT.remaining_seconds(),
                    )
                )
            return False

        if sso:
            elapsed = time.time() - t0
            async with file_lock:
                _persist_registration(email, password, sso, session_cookies)
                metrics.record_success()
                success_count = metrics.success_count
                count = metrics.success_count
            log(
                format_user_registration_event(
                    "success",
                    task_id=task_id,
                    count=count,
                    rate_per_minute=metrics.runtime_average_success_rate(),
                )
            )
            if recovery_probe:
                recovered_after = REGISTRATION_RATE_LIMIT_CIRCUIT.consume_recovery_seconds(
                    recovery_probe
                )
                recovery_completed = True
                if recovered_after is not None:
                    log(
                        format_user_registration_event(
                            "recovered", wait_seconds=round(recovered_after)
                        )
                    )
            return True

        log(format_user_registration_event("failed", task_id=task_id))
        return False
    finally:
        if recovery_probe and not recovery_completed:
            REGISTRATION_RATE_LIMIT_CIRCUIT.defer_probe(recovery_probe)
        if physical_acquired:
            metrics.c_physical_hold_seconds += time.time() - physical_hold_started
            physical_sem.release()


async def c_worker(wid, browser, inventory, physical_sem, metrics, admission_gate=None):
    """C_Worker: claim pair 并执行注册。"""
    while not STOP.is_set():
        recovery_probe = False
        task_id = None
        try:
            async with inventory.claim_pair() as pair:
                if admission_gate is not None:
                    await admission_gate.notify_changed()

                # 必须先 claim，再通过冷却闸门。否则多个 worker 会在限流前
                # 通过闸门，随后于冷却期陆续拿到 pair 并漏闸。等待发生在
                # wait_for 之外，因此不计入单次 C 消费超时。
                recovery_probe = await REGISTRATION_RATE_LIMIT_CIRCUIT.wait()
                if _pair_is_expired(pair):
                    if recovery_probe:
                        REGISTRATION_RATE_LIMIT_CIRCUIT.release_probe(recovery_probe)
                        recovery_probe = False
                    continue

                task_id = metrics.next_registration_task()
                log(
                    format_user_registration_event(
                        "started",
                        task_id=task_id,
                        remaining=max(TARGET - metrics.success_count, 0) if TARGET else None,
                    )
                )
                try:
                    ok = await asyncio.wait_for(
                        _consume_pair(
                            browser,
                            physical_sem,
                            pair,
                            metrics,
                            task_id=task_id,
                            recovery_probe=recovery_probe,
                        ),
                        timeout=C_CONSUME_TIMEOUT,
                    )
                    if ok:
                        metrics.pair_consumed_ok += 1
                    else:
                        metrics.pair_consumed_fail += 1
                except asyncio.TimeoutError:
                    metrics.pair_consumed_fail += 1
                    log(format_user_registration_event("failed", task_id=task_id))

        except asyncio.CancelledError:
            raise
        except Exception as e:
            if task_id is not None:
                log(format_user_registration_event("failed", task_id=task_id))
            debug_log(f'[C] {wid} err: {sanitize_terminal_error(e)}')
            metrics.pair_consumed_fail += 1
        finally:
            if recovery_probe:
                # 覆盖等待 pair、获取物理许可或其他边界处的取消/异常。
                # 已成功或已延期的 probe 在这里会是幂等 no-op。
                REGISTRATION_RATE_LIMIT_CIRCUIT.defer_probe(recovery_probe)
        await asyncio.sleep(0.2)


# ──────────────────────────────────────────────
#  只读监控
# ──────────────────────────────────────────────
async def monitor(inventory, sems, metrics, interval=8):
    """定期输出系统状态。"""
    while not STOP.is_set():
        await asyncio.sleep(interval)
        if REGISTER_LOG_MODE == "debug":
            log(metrics.snapshot(inventory, sems))
        if TARGET and metrics.success_count >= TARGET:
            log(f'[*] 已达目标 {TARGET} 个,停止。'); STOP.set()


# ──────────────────────────────────────────────
#  主入口
# ──────────────────────────────────────────────
async def main():
    global TARGET, _c_hot_page_pool_size
    max_mem_arg = None
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--max-mem' and i + 1 < len(sys.argv):
            max_mem_arg = sys.argv[i + 1]
        elif arg == '--target' and i + 1 < len(sys.argv):
            TARGET = int(sys.argv[i + 1])

    resources = get_system_resources(max_mem_arg)
    cpu = resources['cpu']
    capacity_profile = load_capacity_profile()

    # 自动派生容量
    physical_cap, s_workers, p_workers, c_workers = derive_capacity(
        cpu,
        resources['max_mem'],
        profile_physical_cap=capacity_profile.get("physical_cap"),
    )
    # 外接 Turnstile：S 不吃浏览器，默认把 S 并发抬到 T 槽位量级，避免仍按 Physical 缩水。
    if not TURNSTILE_USES_BROWSER and S_WORKERS <= 0:
        s_workers = max(s_workers, min(32, max(8, T_SLOT_CAP * 2)))
    p_send_cap = P_SEND_CAP if P_SEND_CAP > 0 else 0
    admission_watermarks = derive_admission_watermarks(physical_cap)
    if not TURNSTILE_USES_BROWSER and _T_HIGH_WATER_OVERRIDE is None:
        # 外接 mint 的 T 高水位跟槽位走，不再被 Physical_Sem 卡住。
        admission_watermarks = dict(admission_watermarks)
        admission_watermarks["t_high"] = max(
            admission_watermarks["t_high"],
            min(T_SLOT_CAP, max(T_TARGET, s_workers, physical_cap)),
        )
        if admission_watermarks["t_low"] > admission_watermarks["t_high"]:
            admission_watermarks["t_low"] = max(0, admission_watermarks["t_high"] // 2)
    _c_hot_page_pool_size = derive_c_hot_page_pool_size(physical_cap, c_workers)

    # 校验邮箱模式配置
    if EMAIL_MODE not in ('tempmail', 'custom'):
        log("[!] 配置错误：EMAIL_MODE 应为 tempmail 或 custom"); return 2
    if EMAIL_MODE == 'custom' and not EMAIL_DOMAIN:
        log("[!] 配置错误：custom 模式需在 .env 设置 EMAIL_DOMAIN"); return 2

    debug_log("=" * 50)
    debug_log(f"  Grok Free Register (CSP Architecture)")
    debug_log(f"  CPU: {cpu} cores  Memory: {resources['available_mem']}/{resources['total_mem']}MB")
    debug_log(f"  MaxMemForAuto: {resources['max_mem']}MB  MemReserve: {MIN_FREE_MEM_MB}MB  PhysicalMemBudget: {PHYSICAL_MEM_MB}MB")
    if capacity_profile:
        debug_log(f"  CapacityProfile: {CAPACITY_PROFILE} physical_cap={capacity_profile.get('physical_cap')}")
    debug_log(f"  EmailMode: {EMAIL_MODE}")
    debug_log(
        f"  ProtocolHTTP={'on' if PROTOCOL_HTTP else 'off'}"
        f"  CastleToken={'set' if CASTLE_TOKEN else 'off'}"
        f"  TurnstileProvider={TURNSTILE_PROVIDER}"
        f"  TurnstileBrowser={'on' if TURNSTILE_USES_BROWSER else 'off'}"
    )
    debug_log(f"  Physical_Sem={physical_cap}  T_Slot={T_SLOT_CAP}  Q_Slot={Q_SLOT_CAP}  Q_Pending={Q_PENDING_CAP}")
    debug_log(
        f"  Admission: T_LOW/HIGH={admission_watermarks['t_low']}/{admission_watermarks['t_high']}  "
        f"Q_LOW/HIGH={admission_watermarks['q_low']}/{admission_watermarks['q_high']}"
    )
    debug_log(f"  P_BatchMax={P_BATCH_MAX}  P_Send_Sem={'disabled' if p_send_cap == 0 else p_send_cap}")
    debug_log(
        f"  C_HotPagePool={'on' if C_HOT_PAGE_POOL else 'off'}"
        f" size={_c_hot_page_pool_size if C_HOT_PAGE_POOL else 0}"
        f" setCookieViaRequest={'on' if C_SET_COOKIE_VIA_REQUEST else 'off'}"
    )
    debug_log(f"  Workers: S={s_workers} P={p_workers} C={c_workers}")
    debug_log(
        f"  Timeouts: Solver={SOLVER_HARD_TIMEOUT}s SolverCleanup={SOLVER_CLEANUP_TIMEOUT}s "
        f"P_Request={P_REQUEST_TIMEOUT}s C_Consume={C_CONSUME_TIMEOUT}s"
    )
    debug_log(
        f"  SolverMouseClick: retries={SOLVER_MOUSE_CLICK_RETRIES} "
        f"interval={SOLVER_MOUSE_CLICK_INTERVAL_MS}ms "
        f"earlyPoll={SOLVER_EARLY_POLL_ATTEMPTS}x{SOLVER_EARLY_POLL_MS}ms"
    )
    if TARGET:
        debug_log(f"  Target: {TARGET}")
    reg_proxy = register_proxy_url() or "(direct)"
    debug_log(f"  RegisterProxy: {reg_proxy}")
    debug_log("=" * 50)

    # 上次异常退出可能残留 CloakBrowser；先收尸再 launch，避免叠 3–4 份吃光内存
    reaped = await asyncio.to_thread(reap_orphan_register_browsers)
    if reaped:
        log(f"[*] 已回收 {reaped} 个残留注册浏览器进程")

    # CF prewarm via external FlareSolverr (:8191) — separate from REGISTER_PROXY egress
    prewarm = await asyncio.to_thread(prewarm_clearance)
    log(format_prewarm_log(prewarm))

    await fetch_config()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**_launch_options())
        debug_log(f'[*] Browser launched (proxy={reg_proxy})')

        # CSP 组件
        metrics = Metrics()
        inventory = Inventory(metrics=metrics)
        admission_gate = AdmissionGate(
            inventory,
            t_low=admission_watermarks["t_low"],
            t_high=admission_watermarks["t_high"],
            q_low=admission_watermarks["q_low"],
            q_high=admission_watermarks["q_high"],
        )
        physical_sem = asyncio.Semaphore(physical_cap)
        p_send_sem = asyncio.Semaphore(p_send_cap) if p_send_cap > 0 else None
        t_slot_sem = asyncio.Semaphore(T_SLOT_CAP)
        q_slot_sem = asyncio.Semaphore(Q_SLOT_CAP)
        q_pending_sem = asyncio.Semaphore(Q_PENDING_CAP)

        sems = {
            'physical': physical_sem,
            't_slot': t_slot_sem,
            'q_slot': q_slot_sem,
            'q_pending': q_pending_sem,
            'admission': admission_gate,
        }
        if p_send_sem is not None:
            sems['p_send'] = p_send_sem

        tasks = []

        # S_Workers
        for i in range(s_workers):
            tasks.append(asyncio.create_task(
                s_worker(i, browser, inventory, physical_sem, t_slot_sem, metrics, admission_gate)
            ))

        # P_Workers
        for i in range(p_workers):
            tasks.append(asyncio.create_task(
                p_worker(
                    i,
                    browser,
                    inventory,
                    physical_sem,
                    q_pending_sem,
                    q_slot_sem,
                    metrics,
                    admission_gate,
                    p_send_sem,
                    P_BATCH_MAX,
                )
            ))

        # C_Workers
        for i in range(c_workers):
            tasks.append(asyncio.create_task(
                c_worker(i, browser, inventory, physical_sem, metrics, admission_gate)
            ))

        # Monitor
        tasks.append(asyncio.create_task(monitor(inventory, sems, metrics)))

        debug_log(f'[*] CSP up: S={s_workers} P={p_workers} C={c_workers} workers')
        log(
            format_user_registration_event(
                "service_started",
                remaining=max(TARGET - metrics.success_count, 0) if TARGET else None,
            )
        )

        try:
            await asyncio.gather(*tasks)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            for t in tasks:
                t.cancel()
            with suppress(Exception):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3)
            await _close_c_hot_page_pool()
            await _close_http_client()
            await _close_browser_hard(browser, timeout=5.0)
            # 双保险：close 后仍扫一遍孤儿
            leftover = await asyncio.to_thread(reap_orphan_register_browsers)
            if leftover:
                log(f"[*] 退出时补杀 {leftover} 个残留浏览器")
            log(format_user_registration_event("stopped", count=metrics.success_count))
    return 0

if __name__ == "__main__":
    try:
        REGISTER_LOG_MODE = resolve_register_log_mode(sys.argv[1:])
    except ValueError:
        log("[!] 配置错误：REGISTER_LOG_MODE 应为 user 或 debug")
        raise SystemExit(2)
    try:
        exit_code = asyncio.run(main())
    except ValueError as exc:
        log(f"[!] 配置错误：{sanitize_terminal_error(exc)}")
        exit_code = 2
    except KeyboardInterrupt:
        # asyncio.run 外层再兜一层；收尸防止 kill -2 叠进程
        try:
            n = reap_orphan_register_browsers()
            if n:
                print(f"[*] 中断收尸 {n} 个浏览器", flush=True)
        except Exception:
            pass
        exit_code = 130
    raise SystemExit(exit_code)
