#!/usr/bin/env python3
"""
sync_acc.py — 把 keys/cpa_ready/ 里「探活通过」号的 access_token 同步进 keys/acc.md

默认只入库 acpa_watchdog 记为 alive 的号（读 keys/cpa_ready/_state.tsv）。
403 forbidden / token_dead / probe_err 等不进 acc.md，避免坏号灌库。

用法:
  python3 sync_acc.py            # 常驻轮询(默认 5s), Ctrl-C 退
  python3 sync_acc.py --once     # 同步一次就退
  python3 sync_acc.py --all      # 忽略探活状态，全量抽 token（排障用）
  python3 sync_acc.py --interval 10

acc.md 每轮全量重写(去重)，0600。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import signal
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent      # grok-free-register/
SRC_DIR = PROJECT / "keys" / "cpa_ready"
STATE_FILE = SRC_DIR / "_state.tsv"
OUT_FILE = PROJECT / "keys" / "acc.md"

# 默认可入库的探活状态（discarded/坏号不在 cpa_ready 主目录，也不在此集合）
ALIVE_STATUSES = frozenset({"alive"})
SKIP_STATUSES = frozenset({
    "discarded", "free_exhausted", "rate_limited", "token_dead",
    "no_quota_paid_end", "probe_cap", "chat_denied", "chat_dead",
    "forbidden_domain_ban", "probe_err", "unknown",
})

RUN = True


def _sig(_s, _f):
    global RUN
    RUN = False


signal.signal(signal.SIGINT, _sig)
signal.signal(signal.SIGTERM, _sig)


def log(msg: str) -> None:
    print(f"[sync_acc {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_alive_names() -> set[str] | None:
    """
    读 _state.tsv → 状态为 alive 且盘上仍有 json 的文件名集合。
    无 state 文件返回 None。幽灵 alive（TSV 有、json 无）不计。
    """
    if not STATE_FILE.exists():
        return None
    alive: set[str] = set()
    for ln in STATE_FILE.read_text(encoding="utf-8").splitlines():
        parts = ln.split("\t")
        if len(parts) < 4:
            continue
        name, status = parts[0], parts[3]
        if status in ALIVE_STATUSES:
            # 只认盘上真实存在的号，避免删库后 state 幽灵把计数钉死
            if (SRC_DIR / name).exists():
                alive.add(name)
        # discarded / 坏状态明确不进（双保险；主目录 json 也应已被 watchdog 挪走）
        elif status in SKIP_STATUSES:
            continue
    return alive


def collect(*, require_alive: bool) -> tuple[list[str], dict[str, int]]:
    """
    返回 (去重 token 列表, 计数)。
    require_alive=True 时只收 _state.tsv 标为 alive 且文件仍在的 xai-*.json。
    """
    files = sorted(glob.glob(str(SRC_DIR / "xai-*.json")))
    alive_names = load_alive_names() if require_alive else None

    seen: set[str] = set()
    out: list[str] = []
    stats = {
        "files": len(files),
        "alive_state": len(alive_names) if alive_names is not None else -1,
        "skipped_not_alive": 0,
        "no_tok": 0,
        "bad": 0,
        "no_state_skip_all": 0,
    }

    if require_alive and alive_names is None:
        # 有 cpa json 但还没 state：宁可不灌，避免 403 进库
        if files:
            log(f"⚠ 无 {STATE_FILE.name}，拒绝全量入库（等 watchdog 探活）")
            stats["no_state_skip_all"] = len(files)
        return [], stats

    for f in files:
        name = Path(f).name
        if require_alive and alive_names is not None and name not in alive_names:
            stats["skipped_not_alive"] += 1
            continue
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except Exception:
            stats["bad"] += 1
            continue
        tok = d.get("access_token")
        if not isinstance(tok, str) or not tok:
            stats["no_tok"] += 1
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out, stats


def write_acc(tokens: list[str]) -> None:
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 固定同目录临时文件，避免 with_suffix 边界情况 + 保证 replace 原子
    tmp = OUT_FILE.parent / (OUT_FILE.name + ".tmp")
    tmp.write_text("\n".join(tokens) + ("\n" if tokens else ""), encoding="utf-8")
    os.replace(tmp, OUT_FILE)
    try:
        os.chmod(OUT_FILE, 0o600)
    except OSError:
        pass


def sync_once(*, require_alive: bool) -> int:
    toks, st = collect(require_alive=require_alive)
    write_acc(toks)
    mode = "alive-only" if require_alive else "all"
    log(
        f"sync → {OUT_FILE.name}: {len(toks)} tokens [{mode}] "
        f"(files={st['files']} state_alive={st['alive_state']} "
        f"skip_dead={st['skipped_not_alive']} no_tok={st['no_tok']} bad={st['bad']})"
    )
    return len(toks)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="cpa_ready access_token → keys/acc.md (default: alive only)"
    )
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="刷一次就退")
    ap.add_argument(
        "--all",
        action="store_true",
        help="忽略探活，全量抽 token（排障；会把 403 也写入 acc.md）",
    )
    args = ap.parse_args()
    require_alive = not args.all

    if not SRC_DIR.exists():
        log(f"⚠ 目录不存在: {SRC_DIR} (watchdog 整备后才会建)")
        if args.once:
            return 0

    sync_once(require_alive=require_alive)
    if args.once:
        return 0

    log(f"常驻轮询 interval={args.interval}s alive_only={require_alive} (Ctrl-C 退)")
    while RUN:
        slept = 0.0
        while slept < args.interval and RUN:
            time.sleep(0.25)
            slept += 0.25
        if not RUN:
            break
        sync_once(require_alive=require_alive)

    log("退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
