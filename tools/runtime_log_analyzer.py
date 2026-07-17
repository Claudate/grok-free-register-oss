"""
Parse real runtime monitor logs from both CSP and legacy state-machine runs.

The analyzer is intentionally read-only. It does not tune parameters or modify
runtime state; it only turns production logs into comparable stage rates.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Iterable


_CSP_RE = re.compile(
    r"^\[\*\] T:(?P<t>\d+) Q:(?P<q>\d+) phys:(?P<phys>\d+) "
    r"(?:p_send:(?P<p_send>\d+) )?t_slot:(?P<t_slot>\d+) q_slot:(?P<q_slot>\d+) q_pend:(?P<q_pend>\d+)"
    r"(?P<rest>.*)rate:(?P<rate>[0-9.]+)/min #(?P<ok>\d+)"
)

_STATE_RE = re.compile(
    r"^\[\*\] slots:(?P<slots>\d+)/(?P<max_slots>\d+) act:(?P<active>\d+) "
    r"cpu:(?P<cpu>[0-9.]+)% avg:(?P<cpu_avg>[0-9.]+)%/(?P<cpu_target>[0-9.]+) "
    r"mem:(?P<mem>\d+)M T:(?P<t>\d+) Q:(?P<q>\d+) "
    r"sent:(?P<q_sent>\d+) got:(?P<q_ret>\d+)\((?P<q_hit>[0-9.]+)%\) "
    r"rate:(?P<rate>[0-9.]+)/min #(?P<ok>\d+)"
)

_SOLVER_TIMELINE_PREFIX = "[solver_timeline] "


@dataclass(frozen=True)
class MonitorRow:
    kind: str
    t: int
    q: int
    ok: int
    rate: float
    phys: int | None = None
    p_send: int | None = None
    t_slot: int | None = None
    q_slot: int | None = None
    q_pend: int | None = None
    p_batch: float | None = None
    t_prog: int | None = None
    q_inflight: int | None = None
    t_prod: int | None = None
    q_sent: int | None = None
    q_ret: int | None = None
    q_adm: int | None = None
    pair: int | None = None
    fail: int | None = None
    s_phys_wait: float | None = None
    s_phys_hold: float | None = None
    p_phys_wait: float | None = None
    p_phys_hold: float | None = None
    c_phys_wait: float | None = None
    c_phys_hold: float | None = None
    p_email_create: float | None = None
    p_page_prepare: float | None = None
    p_send_stage: float | None = None
    c_page_acquire: float | None = None
    c_verify: float | None = None
    c_register: float | None = None
    c_hot_hits: int | None = None
    c_hot_misses: int | None = None
    solver_goto: float | None = None
    solver_inject: float | None = None
    solver_initial: float | None = None
    solver_click: float | None = None
    solver_wait: float | None = None
    solver_reuse: float | None = None
    solver_visible: float | None = None
    slots: int | None = None
    max_slots: int | None = None
    active: int | None = None

    @property
    def elapsed_min(self) -> float | None:
        if self.ok <= 0 or self.rate <= 0:
            return None
        return self.ok / self.rate


@dataclass(frozen=True)
class SolverTimeline:
    events: list[dict]


def _int_field(rest: str, name: str) -> int | None:
    match = re.search(rf"\b{name}:(\d+)", rest)
    return int(match.group(1)) if match else None


def _float_field(rest: str, name: str) -> float | None:
    match = re.search(rf"\b{name}:([0-9.]+)", rest)
    return float(match.group(1)) if match else None


def _float_pair_field(rest: str, name: str) -> tuple[float | None, float | None]:
    match = re.search(rf"\b{name}:([0-9.]+)/([0-9.]+)", rest)
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def _float_triple_field(rest: str, name: str) -> tuple[float | None, float | None, float | None]:
    match = re.search(rf"\b{name}:([0-9.]+)/([0-9.]+)/([0-9.]+)", rest)
    if not match:
        return None, None, None
    return float(match.group(1)), float(match.group(2)), float(match.group(3))


def _int_pair_field(rest: str, name: str) -> tuple[int | None, int | None]:
    match = re.search(rf"\b{name}:(\d+)/(\d+)", rest)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def parse_monitor_lines(text_or_lines: str | Iterable[str]) -> list[MonitorRow]:
    if isinstance(text_or_lines, str):
        lines = text_or_lines.splitlines()
    else:
        lines = list(text_or_lines)

    rows: list[MonitorRow] = []
    for line in lines:
        csp = _CSP_RE.match(line)
        if csp:
            rest = csp.group("rest")
            s_phys_wait, s_phys_hold = _float_pair_field(rest, "s_phys")
            p_phys_wait, p_phys_hold = _float_pair_field(rest, "p_phys")
            c_phys_wait, c_phys_hold = _float_pair_field(rest, "c_phys")
            p_email_create, p_page_prepare, p_send_stage = _float_triple_field(rest, "p_stage")
            c_page_acquire, c_verify, c_register = _float_triple_field(rest, "c_stage")
            c_hot_hits, c_hot_misses = _int_pair_field(rest, "c_hot")
            rows.append(
                MonitorRow(
                    kind="csp",
                    t=int(csp.group("t")),
                    q=int(csp.group("q")),
                    ok=int(csp.group("ok")),
                    rate=float(csp.group("rate")),
                    phys=int(csp.group("phys")),
                    p_send=int(csp.group("p_send")) if csp.group("p_send") is not None else None,
                    t_slot=int(csp.group("t_slot")),
                    q_slot=int(csp.group("q_slot")),
                    q_pend=int(csp.group("q_pend")),
                    p_batch=_float_field(rest, "p_batch"),
                    t_prog=_int_field(rest, "t_prog"),
                    q_inflight=_int_field(rest, "q_inflight"),
                    t_prod=_int_field(rest, "t_prod"),
                    q_sent=_int_field(rest, "q_sent"),
                    q_ret=_int_field(rest, "q_ret"),
                    q_adm=_int_field(rest, "q_adm"),
                    pair=_int_field(rest, "pair"),
                    fail=_int_field(rest, "fail"),
                    s_phys_wait=s_phys_wait,
                    s_phys_hold=s_phys_hold,
                    p_phys_wait=p_phys_wait,
                    p_phys_hold=p_phys_hold,
                    c_phys_wait=c_phys_wait,
                    c_phys_hold=c_phys_hold,
                    p_email_create=p_email_create,
                    p_page_prepare=p_page_prepare,
                    p_send_stage=p_send_stage,
                    c_page_acquire=c_page_acquire,
                    c_verify=c_verify,
                    c_register=c_register,
                    c_hot_hits=c_hot_hits,
                    c_hot_misses=c_hot_misses,
                    solver_goto=_float_field(rest, "solver_goto"),
                    solver_inject=_float_field(rest, "solver_inject"),
                    solver_initial=_float_field(rest, "solver_initial"),
                    solver_click=_float_field(rest, "solver_click"),
                    solver_wait=_float_field(rest, "solver_wait"),
                    solver_reuse=_float_field(rest, "solver_reuse"),
                    solver_visible=_float_field(rest, "solver_visible"),
                )
            )
            continue

        state = _STATE_RE.match(line)
        if state:
            rows.append(
                MonitorRow(
                    kind="state_machine",
                    t=int(state.group("t")),
                    q=int(state.group("q")),
                    ok=int(state.group("ok")),
                    rate=float(state.group("rate")),
                    q_sent=int(state.group("q_sent")),
                    q_ret=int(state.group("q_ret")),
                    slots=int(state.group("slots")),
                    max_slots=int(state.group("max_slots")),
                    active=int(state.group("active")),
                )
            )
    return rows


def _rate_delta(rows: list[MonitorRow], attr: str) -> float | None:
    candidates = [r for r in rows if r.elapsed_min is not None and getattr(r, attr) is not None]
    if len(candidates) < 2:
        return None
    first = candidates[0]
    last = candidates[-1]
    dt = (last.elapsed_min or 0) - (first.elapsed_min or 0)
    if dt <= 0:
        return None
    return (getattr(last, attr) - getattr(first, attr)) / dt


def _leader(values: dict[str, float | None]) -> str | None:
    if any(value is None for value in values.values()):
        return None
    return max(values, key=lambda key: values[key] or 0)


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 3)


def summarize_monitor_rows(rows: list[MonitorRow], recent_count: int = 6) -> dict[str, float | int | str | None]:
    if not rows:
        return {"rows": 0}

    last = rows[-1]
    recent = rows[-recent_count:]
    summary: dict[str, float | int | str | None] = {
        "rows": len(rows),
        "kind": last.kind,
        "last_ok": last.ok,
        "last_cumulative_rate": last.rate,
        "last_q_minus_t": last.q - last.t,
        "last_phys": last.phys,
        "last_p_send_sem": last.p_send,
        "last_t_slot": last.t_slot,
        "last_q_slot": last.q_slot,
        "last_q_pend": last.q_pend,
        "last_p_batch": last.p_batch,
        "last_t_prog": last.t_prog,
        "last_q_inflight": last.q_inflight,
        "last_q_return_minus_t_prod": (
            last.q_ret - last.t_prod
            if last.q_ret is not None and last.t_prod is not None
            else None
        ),
        "last_slots": last.slots,
        "last_t_prod": last.t_prod,
        "last_q_ret": last.q_ret,
        "last_q_adm": last.q_adm,
        "last_pair": last.pair,
        "last_fail": last.fail,
        "last_s_phys_wait": last.s_phys_wait,
        "last_s_phys_hold": last.s_phys_hold,
        "last_p_phys_wait": last.p_phys_wait,
        "last_p_phys_hold": last.p_phys_hold,
        "last_c_phys_wait": last.c_phys_wait,
        "last_c_phys_hold": last.c_phys_hold,
        "last_p_email_create": last.p_email_create,
        "last_p_page_prepare": last.p_page_prepare,
        "last_p_send": last.p_send_stage,
        "last_c_page_acquire": last.c_page_acquire,
        "last_c_verify": last.c_verify,
        "last_c_register": last.c_register,
        "last_c_hot_hits": last.c_hot_hits,
        "last_c_hot_misses": last.c_hot_misses,
        "last_physical_hold_leader": _leader({
            "s": last.s_phys_hold,
            "p": last.p_phys_hold,
            "c": last.c_phys_hold,
        }),
        "last_physical_wait_leader": _leader({
            "s": last.s_phys_wait,
            "p": last.p_phys_wait,
            "c": last.c_phys_wait,
        }),
        "last_solver_goto": last.solver_goto,
        "last_solver_inject": last.solver_inject,
        "last_solver_initial": last.solver_initial,
        "last_solver_click": last.solver_click,
        "last_solver_wait": last.solver_wait,
        "last_solver_reuse": last.solver_reuse,
        "last_solver_visible": last.solver_visible,
        "recent_ok_per_min": _rate_delta(recent, "ok"),
        "recent_t_prod_per_min": _rate_delta(recent, "t_prod"),
        "recent_q_ret_per_min": _rate_delta(recent, "q_ret"),
        "recent_pair_per_min": _rate_delta(recent, "pair"),
    }
    return summary


def parse_solver_timelines(text_or_lines: str | Iterable[str]) -> list[SolverTimeline]:
    if isinstance(text_or_lines, str):
        lines = text_or_lines.splitlines()
    else:
        lines = list(text_or_lines)

    timelines: list[SolverTimeline] = []
    for line in lines:
        if not line.startswith(_SOLVER_TIMELINE_PREFIX):
            continue
        payload = line[len(_SOLVER_TIMELINE_PREFIX):]
        try:
            events = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(events, list):
            timelines.append(SolverTimeline(events=[e for e in events if isinstance(e, dict)]))
    return timelines


def _last_page_trace(events: list[dict]) -> dict | None:
    for event in reversed(events):
        page_trace = event.get("page_trace")
        if isinstance(page_trace, dict):
            return page_trace
    return None


def summarize_solver_timelines(timelines: list[SolverTimeline]) -> dict[str, float | int | None]:
    if not timelines:
        return {"solver_timeline_count": 0}

    ok_count = 0
    click_calls: list[float] = []
    click_move_ms: list[float] = []
    click_down_up_ms: list[float] = []
    render_to_token_ms: list[float] = []
    token_write_to_poll_done_ms: list[float] = []
    poll_attempts: list[float] = []
    poll_read_avg_ms: list[float] = []
    click_before_count = 0
    center_iframe_hits = 0
    turnstile_iframe_seen = 0
    widget_seen = 0

    for timeline in timelines:
        events = timeline.events
        page_trace = _last_page_trace(events)
        if page_trace:
            render_called = page_trace.get("render_called_at")
            token_written = page_trace.get("token_written_at")
            if isinstance(render_called, (int, float)) and isinstance(token_written, (int, float)):
                render_to_token_ms.append(float(token_written) - float(render_called))

        for event in events:
            if event.get("event") == "click_before":
                click_before_count += 1
                dom = event.get("dom") if isinstance(event.get("dom"), dict) else {}
                widget = dom.get("widget") if isinstance(dom.get("widget"), dict) else {}
                center = dom.get("element_at_center") if isinstance(dom.get("element_at_center"), dict) else {}
                if widget.get("present") and widget.get("visible"):
                    widget_seen += 1
                if center.get("is_iframe"):
                    center_iframe_hits += 1
                if (dom.get("turnstile_iframe_count") or 0) > 0:
                    turnstile_iframe_seen += 1

            if event.get("event") == "click_after":
                call_ms = event.get("click_call_ms")
                if isinstance(call_ms, (int, float)):
                    click_calls.append(float(call_ms))
                trace = event.get("click_trace") if isinstance(event.get("click_trace"), dict) else {}
                move1 = trace.get("mouse_move1_ms")
                move2 = trace.get("mouse_move2_ms")
                down = trace.get("mouse_down_ms")
                up = trace.get("mouse_up_ms")
                move_total = sum(float(v) for v in (move1, move2) if isinstance(v, (int, float)))
                down_up_total = sum(float(v) for v in (down, up) if isinstance(v, (int, float)))
                if move_total:
                    click_move_ms.append(move_total)
                if down_up_total:
                    click_down_up_ms.append(down_up_total)

            if event.get("event") == "poll_done":
                if event.get("ok"):
                    ok_count += 1
                attempts = event.get("poll_attempts")
                if isinstance(attempts, (int, float)):
                    poll_attempts.append(float(attempts))
                read_avg = event.get("poll_read_ms_avg")
                if isinstance(read_avg, (int, float)):
                    poll_read_avg_ms.append(float(read_avg))
                page_trace = event.get("page_trace") if isinstance(event.get("page_trace"), dict) else {}
                token_written = page_trace.get("token_written_at")
                event_t = event.get("t")
                created_at = page_trace.get("created_at")
                if all(isinstance(v, (int, float)) for v in (token_written, event_t, created_at)):
                    token_write_to_poll_done_ms.append((float(event_t) * 1000.0) - (float(token_written) - float(created_at)))

    return {
        "solver_timeline_count": len(timelines),
        "ok_count": ok_count,
        "avg_click_call_ms": _avg(click_calls),
        "avg_click_mouse_move_ms": _avg(click_move_ms),
        "avg_click_down_up_ms": _avg(click_down_up_ms),
        "avg_render_to_token_ms": _avg(render_to_token_ms),
        "avg_token_write_to_poll_done_ms": _avg(token_write_to_poll_done_ms),
        "avg_poll_attempts": _avg(poll_attempts),
        "avg_poll_read_ms": _avg(poll_read_avg_ms),
        "widget_seen_ratio": _ratio(widget_seen, click_before_count),
        "center_iframe_hit_ratio": _ratio(center_iframe_hits, click_before_count),
        "turnstile_iframe_seen_ratio": _ratio(turnstile_iframe_seen, click_before_count),
    }


def analyze_text(text: str) -> dict[str, float | int | str | None]:
    return summarize_monitor_rows(parse_monitor_lines(text))
