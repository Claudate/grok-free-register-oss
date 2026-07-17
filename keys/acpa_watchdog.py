#!/usr/bin/env python3
"""
acpa_watchdog.py — 自动 CPA 凭据整备驻守进程

职责:
  1. 盯 xai_enroller (auth-service.sh) 的出货目录:
       $XAI_ENROLLER_LOCAL_AUTH_DIR/authenticated/xai-*.json
       默认 ~/Downloads/grok-free-register-auth/authenticated/
     xai_enroller 产出的 CPA 文件 base_url=api.x.ai/v1 (付费口, 免费号必 402)
     + headers=None (缺 grok-cli 身份头) + email=None。
  2. 每个新号原样不动地「整备」一份到 PROJECT/keys/cpa_ready/xai-*.json:
       - base_url  → https://cli-chat-proxy.grok.com/v1   (免费 Grok 4.5 口)
       - headers   → grok-cli 身份头(x-grok-client-version/identifier 等)
       - email     → 从 id_token /access_token JWT 里挖(便于辨识, 不影响通信用)
       - 权限 0600 (合规: 含 refresh_token)
  3. 探活: 打 cli-chat-proxy /v1/responses model=grok-4.5
       请求对齐 new-api grok-cli 完整指纹（session 族头 + input 数组 + max_output_tokens 极小）。
       首探 ≤MAX_PROBES(默认 1，省 free 2M 额度)；200 → alive。
       新号落盘后 ACPA_PROBE_WARMUP_SEC(默认 3s) 再探，避免 mint 后瞬时 permission-denied。
       单次探针内 403 当场短重试 ACPA_403_IMMEDIATE_RETRIES×SLEEP（默认 2×4s）。
       仍 403 → chat_denied：不丢，RETEST_AFTER_SEC(默认 180s) 后回测 1 次；
         回测活 → alive；仍 403 → chat_dead 丢。
       429 free-usage-exhausted / rate_limited / 401 → 立刻丢。
       alive 永不复探。
  4. 状态: keys/cpa_ready/_state.tsv
       name\tsub\temail\tstatus\tts\tprobes
       每轮 prune：json 在 cpa_ready/_discarded 都不存在的行直接从 TSV 删掉
       （删库跑路后不会残留幽灵 alive；盘上有 json 但无 state 会重新进探活）。

只读 AUTH_DIR, 只写 keys/cpa_ready/。源 CPA 文件不动; 不重铸 device flow;
不做 HTTP 灌库 (入库步骤留给灌库脚本, 此处只把号整备到可直接喂 cliproxy / new-api)。

CLI:
  python3 acpa_watchdog.py                 # 常驻轮询 (默认 2s), Ctrl-C 退
  python3 acpa_watchdog.py --once          # 单次扫一轮现存号就退 (CLI 自检)
  python3 acpa_watchdog.py --interval 4
"""
from __future__ import annotations

import argparse
import base64
import copy
import json
import os
import signal
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# paths — portable: follow project root of this script; AUTH overridable via env
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent  # .../grok-free-register
# Align with xai_enroller.service DEFAULT_LOCAL_AUTH_DIR
# (~/Downloads/grok-free-register-auth/authenticated by default)
_AUTH_BASE = Path(
    os.environ.get(
        "XAI_ENROLLER_LOCAL_AUTH_DIR",
        os.environ.get(
            "XAI_AUTH_SERVICE_LOCAL_DIR",
            str(Path.home() / "Downloads" / "grok-free-register-auth"),
        ),
    )
).expanduser()
AUTH_DIR = _AUTH_BASE / "authenticated"
OUT_DIR = PROJECT_ROOT / "keys" / "cpa_ready"
DISCARD_DIR = OUT_DIR / "_discarded"
STATE_FILE = OUT_DIR / "_state.tsv"

# grok-cli 静态身份头（对齐 new-api common.GrokCLI* / SetupRequestHeader）
# session/req/agent 等 per-request 头只在探针里生成，不写进落盘 json。
CLIPROXY_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CLIPROXY_HEADERS = {
    "x-grok-client-version": "0.2.93",
    "x-xai-token-auth": "xai-grok-cli",
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-authenticateresponse": "authenticate-response",
    "x-grok-client-identifier": "grok-shell",
    "x-compaction-at": "400000",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
}

RUN = True


def _sig(signum, _frame):
    global RUN
    RUN = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[acpa {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def b64url_decode(seg: str) -> bytes:
    seg += "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg)


def jwt_payload(token: str) -> dict:
    if not token or token.count(".") != 2:
        return {}
    try:
        return json.loads(b64url_decode(token.split(".")[1]))
    except Exception:
        return {}


def load_source(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log(f"  ✗ 读 {path.name} 失败: {exc}")
        return None


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass


def fallback_email(src: dict) -> str:
    """xai_enroller 产的 file email 字段是 None。从 JWT 里挖一个标识。"""
    for key in ("id_token", "access_token"):
        pl = jwt_payload(src.get(key) or "")
        for f in ("email", "preferred_username", "sub"):
            v = pl.get(f)
            if v:
                return str(v)
    return ""


def finalize(src: dict) -> tuple[str, dict]:
    """把 xai_enroller 产的 raw CPA 整备成 cli-chat-proxy 可用格式。返回 (sub, dst)。"""
    dst = copy.deepcopy(src)
    dst["base_url"] = CLIPROXY_BASE_URL
    dst["headers"] = dict(CLIPROXY_HEADERS)
    # token_endpoint 不变 (auth.x.ai/oauth2/token, refresh 用)
    dst.setdefault("auth_kind", "oauth")
    dst.setdefault("type", "xai")
    if not dst.get("email"):
        dst["email"] = fallback_email(src)
    sub = dst.get("sub") or jwt_payload(dst.get("access_token", "")).get("sub", "")
    dst["sub"] = sub
    return sub, dst


def write_out(name: str, entry: dict) -> Path:
    ensure_dir(OUT_DIR)
    target = OUT_DIR / name
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(entry, separators=(",", ":"), ensure_ascii=False),
                   encoding="utf-8")
    os.replace(tmp, target)
    os.chmod(target, 0o600)
    return target


# ---------------------------------------------------------------------------
# probe liveness — 使用项目自带 .venv(已装 curl_cffi)
# 完整 grok-cli 指纹；默认每号只首探 1 次（free ~2M，禁止连 ping）
# ---------------------------------------------------------------------------
_VENV_PY = PROJECT_ROOT / ".venv" / "bin" / "python"
_PROBE_SRC = r'''
import json, sys, uuid
try:
    from curl_cffi import requests
except Exception as e:
    print("NO_CURL_CFFI", str(e)); sys.exit(3)

fp = sys.argv[1]
d = json.load(open(fp))
at = d.get("access_token") or ""
if not at:
    print(json.dumps({"status": "token_dead", "http": 401, "snippet": "no access_token"}))
    sys.exit(0)

# 对齐 new-api-fusion relay/channel/grok_cli SetupRequestHeader + common.GrokCLI*
sid = str(uuid.uuid4())
rid = str(uuid.uuid4())
hdrs = {
    "Authorization": "Bearer " + at,
    "Content-Type": "application/json",
    "Accept": "application/json",
    "User-Agent": "grok-shell/0.2.93 (linux; x86_64)",
    "x-xai-token-auth": "xai-grok-cli",
    "X-XAI-Token-Auth": "xai-grok-cli",
    "x-grok-client-identifier": "grok-shell",
    "x-grok-client-version": "0.2.93",
    "x-authenticateresponse": "authenticate-response",
    "x-compaction-at": "400000",
    "Connection": "Keep-Alive",
    "x-grok-session-id": sid,
    "x-grok-conv-id": sid,
    "x-grok-req-id": rid,
    "x-grok-turn-idx": "1",
    "x-grok-agent-id": "agent-" + rid[:8],
    "x-grok-model-override": "grok-4.5",
}
email = d.get("email") or ""
sub = d.get("sub") or ""
if email:
    hdrs["x-email"] = str(email)
if sub:
    hdrs["x-userid"] = str(sub)

base = (d.get("base_url") or "https://cli-chat-proxy.grok.com/v1").rstrip("/")
# 兼容 base 已带 /v1 或只有 host
if base.endswith("/responses"):
    url = base
else:
    url = base + "/responses"

# input 必须是 items 数组；裸字符串会被上游拒 / 误 403
# max_output_tokens 压到极小，free 号额度只花探针这一枪
body = {
    "model": "grok-4.5",
    "store": False,
    "stream": False,
    "max_output_tokens": 16,
    "input": [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ok"}],
        }
    ],
}
try:
    r = requests.post(url, json=body, headers=hdrs, impersonate="chrome", timeout=45)
    txt = r.text or ""
    code = r.status_code
except Exception as e:
    print("EXC", str(e)[:200]); sys.exit(2)

status = "unknown"
low = txt.lower()
if code == 200 and ("output" in txt or "completed" in low or '"status"' in low):
    # 200 即视为通路可用；不要求模型真回 "ok"（省解码/推理）
    status = "alive"
elif (
    code == 429
    or "free-usage-exhausted" in low
    or "used all the include" in low
    or "rate limit" in low
    or "rate_limit" in low
):
    status = "free_exhausted" if ("free-usage" in low or "used all" in low) else "rate_limited"
elif code == 402 or "personal-team-blocked" in txt or "spending-limit" in txt:
    status = "no_quota_paid_end"
elif code == 403 or "permission-denied" in txt or "chat endpoint is denied" in low:
    status = "chat_denied"
elif code == 401:
    status = "token_dead"
elif code == 422:
    # body 形态问题：标 unknown 便于发现探针回归，不直接当号死
    status = "unknown"
elif "forbidden" in low:
    status = "chat_denied"
print(json.dumps({"status": status, "http": code, "snippet": txt[:200]}))
sys.exit(0)
'''

# 省额度：默认首探 1 次；403 当场短重试后再标 soft，默认 3 分钟后延迟回测 1 次。
# alive 永不复探。429 free-usage-exhausted / token_dead 等真坏号立刻丢。
MAX_PROBES = int(
    os.environ.get(
        "ACPA_MAX_PROBES",
        os.environ.get("ACPA_PROBE_RETRIES", "1"),
    )
)
PROBE_RETRY_SLEEP = float(os.environ.get("ACPA_PROBE_RETRY_SLEEP", "1.0"))
# 新号 finalize 后稍等再首探：上游常对刚 mint 的 access 回瞬时 permission-denied
PROBE_WARMUP_SEC = float(os.environ.get("ACPA_PROBE_WARMUP_SEC", "3.0"))
# 单次 probe_one 内 403 当场短重试（不算额外 MAX_PROBES 预算）
IMMEDIATE_403_RETRIES = int(os.environ.get("ACPA_403_IMMEDIATE_RETRIES", "2"))
IMMEDIATE_403_SLEEP = float(os.environ.get("ACPA_403_IMMEDIATE_SLEEP", "4.0"))
RETEST_AFTER_SEC = int(os.environ.get("ACPA_RETEST_AFTER_SEC", "180"))  # 3 分钟
RETEST_MAX = int(os.environ.get("ACPA_RETEST_MAX", "1"))  # 403 延迟回测次数

# 终态：不再探（alive 保留；下列坏号 discard）
TERMINAL_OK = frozenset({"alive"})
TERMINAL_BAD = frozenset({
    "free_exhausted",   # 429 免费额度耗尽 → 丢
    "rate_limited",     # 其它 429 → 丢
    "token_dead",       # 401
    "no_quota_paid_end",
    "chat_dead",        # 5 分钟回测后仍 403 → 真坏，丢
    "discarded",
})
TERMINAL_STATUSES = TERMINAL_OK | TERMINAL_BAD

# 仅这些立刻/确认后丢出 cpa_ready（403 首探不在此列）
DISCARD_STATUSES = frozenset({
    "free_exhausted",
    "rate_limited",
    "token_dead",
    "no_quota_paid_end",
    "chat_dead",
})

# 等 5 分钟回测的软状态（保留 json，不进 acc，不丢）
SOFT_RETEST_STATUSES = frozenset({
    "chat_denied",
    "forbidden_domain_ban",  # 历史误标，按 403 软状态回测
    "probe_cap",             # 历史：首探 2 次 403 被钉死，捞回可回测
})


def probe_one(path: Path) -> dict:
    """用子进程跑探活(隔离 curl_cffi 环境)。返回 {status, http, snippet}."""
    import subprocess
    cp = subprocess.run(
        [str(_VENV_PY), "-c", _PROBE_SRC, str(path)],
        capture_output=True, text=True, timeout=60,
    )
    out = cp.stdout.strip()
    if out.startswith("alive") or out.lstrip().startswith("{"):
        try:
            idx = out.find("{")
            if idx >= 0:
                return json.loads(out[idx:])
        except Exception:
            pass
    return {"status": "probe_err", "http": -1, "snippet": (out or cp.stderr[:200])[:200]}


def probe_one_soft(path: Path) -> dict:
    """
    一次「逻辑探针」：底层请求 + 403/瞬时错误当场短重试。
    仍计为 probe_budgeted 的 1 次 used（不因短重试烧额外额度记账）。
    """
    last = {"status": "probe_err", "http": -1, "snippet": ""}
    attempts = 1 + max(0, IMMEDIATE_403_RETRIES)
    for i in range(attempts):
        last = probe_one(path)
        st = last.get("status")
        if st in (
            "alive", "free_exhausted", "rate_limited",
            "no_quota_paid_end", "token_dead",
        ):
            return last
        if st in ("chat_denied", "forbidden_domain_ban", "probe_err", "unknown"):
            if i + 1 < attempts:
                time.sleep(IMMEDIATE_403_SLEEP)
                continue
        break
    if last.get("status") == "forbidden_domain_ban":
        last = {
            "status": "chat_denied",
            "http": last.get("http", 403),
            "snippet": last.get("snippet", "")[:200],
        }
    return last


def probe_budgeted(path: Path, budget: int) -> tuple[dict, int]:
    """
    在 budget 次内探活。返回 (最后结果, 实际探活次数)。
    alive / 额度终态立刻停；403 可在预算内连探；最终 403 保持 chat_denied（不改 probe_cap）。
    """
    budget = max(0, int(budget))
    if budget <= 0:
        return {"status": "chat_denied", "http": -1, "snippet": "no probe budget"}, 0
    last = {"status": "probe_err", "http": -1, "snippet": ""}
    used = 0
    for i in range(budget):
        last = probe_one_soft(path)
        used += 1
        st = last.get("status")
        if st in (
            "alive", "free_exhausted", "rate_limited",
            "no_quota_paid_end", "token_dead",
        ):
            return last, used
        if i + 1 < budget and st in (
            "chat_denied", "forbidden_domain_ban", "probe_err", "unknown",
        ):
            time.sleep(PROBE_RETRY_SLEEP)
            continue
        break
    # 首探/回测结束仍是 403 → 保持 chat_denied，交给延迟回测或 chat_dead
    if last.get("status") in ("forbidden_domain_ban",):
        last = {
            "status": "chat_denied",
            "http": last.get("http", 403),
            "snippet": last.get("snippet", "")[:200],
        }
    return last, used


# ---------------------------------------------------------------------------
# state  — name \t sub \t email \t status \t ts [\t probes]
# ---------------------------------------------------------------------------
def load_state() -> dict:
    """{name: {sub, email, status, ts, probes}}"""
    if not STATE_FILE.exists():
        return {}
    st = {}
    for ln in STATE_FILE.read_text(encoding="utf-8").splitlines():
        parts = ln.split("\t")
        if len(parts) >= 5:
            name, sub, email, status, ts = parts[:5]
            probes = 0
            if len(parts) >= 6:
                try:
                    probes = int(parts[5] or 0)
                except ValueError:
                    probes = 0
            # 历史行没有 probes：已有终态按已探满处理，避免重启后重烧额度
            if probes <= 0 and status in TERMINAL_STATUSES | {
                "chat_denied", "forbidden_domain_ban", "unknown", "probe_err",
            }:
                probes = MAX_PROBES if status != "alive" else 1
            if probes <= 0 and status == "alive":
                probes = 1
            st[name] = {
                "sub": sub, "email": email, "status": status,
                "ts": ts, "probes": probes,
            }
    return st


def write_state(st: dict) -> None:
    ensure_dir(OUT_DIR)
    lines = []
    for name, v in sorted(st.items()):
        lines.append("\t".join([
            name, v.get("sub", ""), v.get("email", ""),
            v.get("status", ""), str(v.get("ts", "")),
            str(int(v.get("probes") or 0)),
        ]))
    tmp = STATE_FILE.with_suffix(".tsv.tmp")
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    os.replace(tmp, STATE_FILE)
    os.chmod(STATE_FILE, 0o600)


def json_exists_for(name: str) -> bool:
    """cpa_ready 主目录或 _discarded 里是否还有该号 json。"""
    return (OUT_DIR / name).exists() or (DISCARD_DIR / name).exists()


def prune_orphan_state(st: dict) -> int:
    """
    删库跑路 / 手工清 json 后：TSV 里既无 cpa_ready 也无 _discarded 文件的行直接 drop。
    避免幽灵 alive 把 sync/账本钉死。返回删除行数。
    """
    if not st:
        return 0
    drop = [name for name in list(st.keys()) if not json_exists_for(name)]
    if not drop:
        return 0
    for name in drop:
        del st[name]
    write_state(st)
    log(f"  🧹 prune orphan state {len(drop)} 行（盘上无 json）")
    return len(drop)


def should_discard(status: str, probes: int = 0) -> bool:
    """仅真坏号丢：429 额度耗尽 / 401 / 回测后仍 403(chat_dead)。首探 403 不丢。"""
    return status in DISCARD_STATUSES


def discard_bad(name: str, st: dict, *, reason: str = "") -> bool:
    """
    坏号移出 keys/cpa_ready/ → keys/cpa_ready/_discarded/
    state 标记 discarded，sync_acc 只认 alive 故 acc.md 自动剔除。
    返回是否执行了丢弃。
    """
    entry = st.get(name) or {}
    status = entry.get("status", reason or "bad")
    probes = _probes(entry) if entry else MAX_PROBES
    if status == "discarded":
        # 已丢过：确保 json 不在主目录
        p = OUT_DIR / name
        if p.exists():
            ensure_dir(DISCARD_DIR)
            dest = DISCARD_DIR / name
            try:
                os.replace(p, dest)
            except OSError:
                try:
                    p.unlink()
                except OSError:
                    pass
        return False
    if not should_discard(status, probes) and not reason:
        return False

    ensure_dir(DISCARD_DIR)
    src = OUT_DIR / name
    moved = False
    if src.exists():
        dest = DISCARD_DIR / name
        try:
            if dest.exists():
                dest.unlink()
            os.replace(src, dest)
            os.chmod(dest, 0o600)
            moved = True
        except OSError as exc:
            log(f"  ✗ 丢弃移动失败 {name}: {exc}")
            try:
                src.unlink()
                moved = True
            except OSError:
                pass

    st[name] = {
        "sub": entry.get("sub", ""),
        "email": entry.get("email", ""),
        "status": "discarded",
        "ts": int(time.time()),
        "probes": probes if probes else MAX_PROBES,
    }
    # 旁路账本：便于扫为何丢
    try:
        ledger = DISCARD_DIR / "_discarded.tsv"
        with ledger.open("a", encoding="utf-8") as fh:
            fh.write(
                f"{int(time.time())}\t{name}\t{entry.get('email','')}\t"
                f"{status}\t{reason or status}\n"
            )
        os.chmod(ledger, 0o600)
    except OSError:
        pass
    log(
        f"  🗑 discard {name} [{status}]"
        f"{' → _discarded/' if moved else ' (no json)'}"
        f" email={entry.get('email','')}"
    )
    return True


def sweep_discard(st: dict) -> int:
    """扫 state + cpa_ready，丢掉所有真坏号 json。返回丢弃个数。"""
    n = 0
    for name in list(st.keys()):
        if not RUN:
            break
        v = st[name]
        if v.get("status") == "discarded":
            discard_bad(name, st)  # 清残留 json
            continue
        # 软 403 已用完延迟回测次数 → 升格 chat_dead 再丢
        if v.get("status") in SOFT_RETEST_STATUSES:
            if _retests_done(v) >= RETEST_MAX and _age_sec(v) >= RETEST_AFTER_SEC:
                v["status"] = "chat_dead"
                st[name] = v
        if should_discard(v.get("status", ""), _probes(v)):
            if discard_bad(name, st):
                n += 1
    write_state(st)
    return n


def rescue_soft_discards(st: dict) -> int:
    """
    把误丢的 403/probe_cap 从 _discarded 捞回主目录，标 chat_denied，
    立刻可做一次延迟回测（ts 回拨到已到期）。
    账本原因为 soft，或 state 已 discarded 但 json 仍在 _discarded 且无硬坏标记。
    """
    ensure_dir(DISCARD_DIR)
    ensure_dir(OUT_DIR)
    soft_reasons = SOFT_RETEST_STATUSES | {"probe_cap", "chat_denied", "forbidden_domain_ban"}
    soft_names: set[str] = set()
    ledger = DISCARD_DIR / "_discarded.tsv"
    if ledger.exists():
        for ln in ledger.read_text(encoding="utf-8").splitlines():
            parts = ln.split("\t")
            if len(parts) >= 5:
                name, prev_st, reason = parts[1], parts[3], parts[4]
                if prev_st in soft_reasons or reason in soft_reasons:
                    soft_names.add(name)
            elif len(parts) >= 4:
                name, prev_st = parts[1], parts[3]
                if prev_st in soft_reasons:
                    soft_names.add(name)

    n = 0
    for p in sorted(DISCARD_DIR.glob("xai-*.json")):
        if not RUN:
            break
        name = p.name
        # 账本标 soft，或 state 缺/已 discarded 时默认按 soft 捞（旧误丢）
        entry = st.get(name) or {}
        prev = entry.get("status", "discarded")
        if name not in soft_names and prev not in ("discarded", "") and prev not in soft_reasons:
            continue
        if name not in soft_names and prev == "discarded":
            # 无账本线索：仍捞（用户确认 403 常误杀）；真 429 已不在 soft 集合时靠账本
            soft_names.add(name)
        if name not in soft_names:
            continue
        dest = OUT_DIR / name
        try:
            if dest.exists():
                dest.unlink()
            os.replace(p, dest)
            os.chmod(dest, 0o600)
        except OSError as exc:
            log(f"  ✗ rescue 移动失败 {name}: {exc}")
            continue
        # ts 回拨：立刻进入可回测窗口（不额外空等 RETEST_AFTER_SEC）
        st[name] = {
            "sub": entry.get("sub", ""),
            "email": entry.get("email", ""),
            "status": "chat_denied",
            "ts": int(time.time()) - RETEST_AFTER_SEC,
            "probes": MAX_PROBES,  # 留给 1 次回测额度（与当前 MAX_PROBES 对齐）
        }
        n += 1
        log(f"  ↩ rescue {name} → chat_denied (retest due) probes={MAX_PROBES}")
    if n:
        write_state(st)
    return n


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------
def _probes(entry: dict) -> int:
    try:
        return int(entry.get("probes") or 0)
    except (TypeError, ValueError):
        return 0


def _age_sec(entry: dict) -> int:
    try:
        return max(0, int(time.time()) - int(entry.get("ts") or 0))
    except (TypeError, ValueError):
        return RETEST_AFTER_SEC


def _retests_done(entry: dict) -> int:
    """首探预算用 MAX_PROBES；超出部分算延迟回测次数。"""
    return max(0, _probes(entry) - MAX_PROBES)


def should_probe(name: str, st: dict) -> bool:
    """
    新号：首探（≤MAX_PROBES 次当场连探）。
    403 软状态：满首探后等 RETEST_AFTER_SEC，再给 RETEST_MAX 次回测。
    429/真坏/终态：不探。
    """
    if name not in st:
        return True
    cur = st[name].get("status", "")
    if cur == "discarded" or cur in TERMINAL_STATUSES:
        return False
    probes = _probes(st[name])

    if cur in SOFT_RETEST_STATUSES:
        if _retests_done(st[name]) >= RETEST_MAX:
            return False
        # 首探未打满（异常中断）→ 先补完首探，不算延迟回测
        if probes < MAX_PROBES:
            return True
        return _age_sec(st[name]) >= RETEST_AFTER_SEC

    # 新号 / pending / 临时错误：未满首探预算可探
    if probes < MAX_PROBES and cur in ("", "pending", "probe_err", "unknown"):
        return True
    return False


def process_file(name: str, st: dict, *, force_probe: bool = False) -> None:
    src_path = AUTH_DIR / name
    # 回测/整备也可只读 cpa_ready 已有 json（rescue 后 AUTH 可能仍在）
    out_existing = OUT_DIR / name
    if not src_path.exists() and not out_existing.exists():
        return

    src = None
    if src_path.exists():
        src = load_source(src_path)
    if (not src or not src.get("access_token")) and out_existing.exists():
        src = load_source(out_existing)
    if not src:
        return
    if not src.get("access_token"):
        log(f"  ✗ {name} 无 access_token, 跳过")
        return

    if name in st and st[name].get("status") == "discarded":
        return

    sub, entry = finalize(src)
    out_path = write_out(name, entry)

    already = _probes(st[name]) if name in st else 0
    prev_status = st[name].get("status", "") if name in st else ""
    is_soft_retest = (
        prev_status in SOFT_RETEST_STATUSES
        and already >= MAX_PROBES
        and _retests_done(st.get(name, {})) < RETEST_MAX
    )

    if not force_probe and not should_probe(name, st):
        if name in st and should_discard(st[name].get("status", ""), already):
            discard_bad(name, st)
            write_state(st)
            return
        # 软状态未到点：静默整备
        if prev_status in SOFT_RETEST_STATUSES:
            left = max(0, RETEST_AFTER_SEC - _age_sec(st[name]))
            if already >= MAX_PROBES and _retests_done(st[name]) < RETEST_MAX:
                log(
                    f"  • {name} 403 待回测 "
                    f"({left}s / retest={_retests_done(st[name])}/{RETEST_MAX})"
                )
                return
        log(
            f"  • {name} 整备覆盖 "
            f"({prev_status or '-'}/probes={already}) → {out_path.name}"
        )
        return

    if force_probe and already >= MAX_PROBES + RETEST_MAX and not os.environ.get("ACPA_FORCE_IGNORE_CAP"):
        log(f"  • {name} 已 probes={already}，跳过强制重探（省额度）")
        if should_discard(st.get(name, {}).get("status", ""), already):
            discard_bad(name, st)
            write_state(st)
        return

    if is_soft_retest:
        budget = 1
        phase = f"403回测×1 (after {RETEST_AFTER_SEC}s, done={_retests_done(st[name])}/{RETEST_MAX})"
    else:
        budget = max(0, MAX_PROBES - already)
        phase = f"首探×≤{budget} (used={already}/{MAX_PROBES})"

    # 新号首探前 warmup：刚 mint 的 access 常被上游瞬时 403
    if not is_soft_retest and PROBE_WARMUP_SEC > 0:
        time.sleep(PROBE_WARMUP_SEC)

    # 新号首探前 warmup：刚 mint 的 access 常被上游瞬时 403
    if not is_soft_retest and PROBE_WARMUP_SEC > 0:
        time.sleep(PROBE_WARMUP_SEC)

    log(f"  ▸ {name} 整备完成, {phase} ...")
    r, used = probe_budgeted(out_path, budget)
    status = r.get("status", "probe_err")
    snip = r.get("snippet", "")
    new_probes = already + used

    # 延迟回测仍 403 → 真坏 chat_dead
    if is_soft_retest and status in (
        "chat_denied", "forbidden_domain_ban", "probe_cap", "probe_err", "unknown",
    ):
        status = "chat_dead"
        log(
            f"    {name}: [chat_dead] 回测仍 403/失败 http={r.get('http')} "
            f"probes={new_probes} {snip[:70]}"
        )
    else:
        log(
            f"    {name}: [{status}] http={r.get('http')} "
            f"probes={new_probes} {snip[:70]}"
        )

    st[name] = {
        "sub": sub,
        "email": entry.get("email", ""),
        "status": status,
        "ts": int(time.time()),
        "probes": new_probes,
    }
    if should_discard(status, new_probes):
        discard_bad(name, st)
    write_state(st)


def scan_once(st: dict) -> int:
    """扫一轮：新号首探；403 到期回测；429 等真坏丢弃；alive 只刷整备。"""
    if not AUTH_DIR.exists():
        log(f"⚠ 出货目录不存在: {AUTH_DIR} (auth-service.sh 启动后才会建)")
        # 仍处理 cpa_ready 里待回测/rescue 的号
    pruned = prune_orphan_state(st)
    if pruned:
        log(f"本轮 prune 幽灵 state {pruned} 行")
    discarded = sweep_discard(st)
    if discarded:
        log(f"本轮丢弃坏号 {discarded} 个 → {DISCARD_DIR.name}/")

    names: set[str] = set()
    if AUTH_DIR.exists():
        names.update(p.name for p in AUTH_DIR.glob("xai-*.json"))
    # cpa_ready 里所有现存 json 都纳入扫描：
    # - soft 到期回测
    # - 无 state（删 TSV 后残留 / 手工丢入）→ 当新号首探
    # - alive 走 should_probe=False 分支只刷整备
    if OUT_DIR.exists():
        names.update(p.name for p in OUT_DIR.glob("xai-*.json"))

    n = 0
    for name in sorted(names):
        if not RUN:
            break
        if name in st and st[name].get("status") == "discarded":
            continue
        if name in st and not should_probe(name, st):
            if should_discard(st[name].get("status", ""), _probes(st[name])):
                discard_bad(name, st)
                write_state(st)
                continue
            src_path = AUTH_DIR / name if (AUTH_DIR / name).exists() else OUT_DIR / name
            src = load_source(src_path) if src_path.exists() else None
            if src and src.get("access_token"):
                _, entry = finalize(src)
                write_out(name, entry)
            continue
        process_file(name, st)
        n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description="auto CPA 整备驻守")
    ap.add_argument("--interval", type=float, default=2.0)
    ap.add_argument("--once", action="store_true", help="扫一轮就退")
    ap.add_argument(
        "--reprobe-soft",
        action="store_true",
        help="立刻对 chat_denied/probe_cap 等软 403 做回测（仍 1 次/号，受 RETEST_MAX）",
    )
    ap.add_argument(
        "--no-rescue",
        action="store_true",
        help="启动时不从 _discarded 捞回误丢的 403",
    )
    args = ap.parse_args()

    ensure_dir(OUT_DIR)
    ensure_dir(DISCARD_DIR)
    st = load_state()
    n_prune = prune_orphan_state(st)

    # 历史 probe_cap 并入 soft，可走延迟回测
    for name, v in st.items():
        if v.get("status") == "probe_cap":
            v["status"] = "chat_denied"

    soft_n = sum(1 for v in st.values() if v.get("status") in SOFT_RETEST_STATUSES)
    bad_n = sum(
        1 for v in st.values()
        if v.get("status") != "discarded"
        and should_discard(v.get("status", ""), _probes(v))
    )
    log(
        f"启动 | 出货={AUTH_DIR} | 落盘={OUT_DIR} | state={len(st)} 条 "
        f"(启动 prune {n_prune}) | 首探≤{MAX_PROBES}/号 "
        f"| warmup={PROBE_WARMUP_SEC}s 403即重试={IMMEDIATE_403_RETRIES}×{IMMEDIATE_403_SLEEP}s "
        f"| 403回测 after={RETEST_AFTER_SEC}s ×{RETEST_MAX} "
        f"| soft_403={soft_n} | pending_discard={bad_n}"
    )

    if not args.no_rescue:
        n_res = rescue_soft_discards(st)
        if n_res:
            log(f"启动捞回误丢 403 {n_res} 个（将回测）")

    n0 = sweep_discard(st)
    if n0:
        log(f"启动丢弃真坏号 {n0} 个 → {DISCARD_DIR}/")

    if st and not args.reprobe_soft:
        log(
            f"(state 已有: 刷 alive 整备；403 满 {RETEST_AFTER_SEC}s 回测×{RETEST_MAX}；"
            f"429 free_exhausted 立刻丢)"
        )
        for name in list(st.keys()):
            if not RUN:
                break
            if st[name].get("status") != "alive":
                continue
            src_path = AUTH_DIR / name
            if src_path.exists():
                src = load_source(src_path)
                if src and src.get("access_token"):
                    _, entry = finalize(src)
                    write_out(name, entry)

    if args.reprobe_soft:
        # soft 立刻回测一次：拨到期 + 把 probes 收成 MAX_PROBES，
        # 避免旧版 probes=2 在新默认 MAX_PROBES=1 下被算成 retest 已用尽。
        # 只动 soft；alive 绝不复探（省 free 额度）。
        now = int(time.time())
        targets = []
        for name, v in st.items():
            if v.get("status") in SOFT_RETEST_STATUSES:
                v["ts"] = now - RETEST_AFTER_SEC
                v["probes"] = MAX_PROBES
                targets.append(name)
        write_state(st)
        log(f"--reprobe-soft: {len(targets)} 个 403 软状态立刻回测×1（alive 不动）")
        for name in targets:
            if not RUN:
                break
            process_file(name, st, force_probe=True)
        write_state(st)
        alive = sum(1 for v in st.values() if v.get("status") == "alive")
        dead = sum(
            1 for v in st.values()
            if v.get("status") in DISCARD_STATUSES or v.get("status") == "discarded"
        )
        soft = sum(1 for v in st.values() if v.get("status") in SOFT_RETEST_STATUSES)
        log(f"回测结束 | alive={alive} soft_403={soft} dead/discard={dead} state={len(st)}")
        return 0

    while RUN:
        try:
            n = scan_once(st)
            if n:
                log(f"本轮处理 {n} 个号（新号/回测）")
        except Exception as exc:
            log(f"轮询异常: {type(exc).__name__} {exc}")
        if args.once:
            break
        slept = 0.0
        while slept < args.interval and RUN:
            time.sleep(0.25)
            slept += 0.25

    log(f"退出 | state={len(st)} 条 写 {STATE_FILE}")
    write_state(st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
