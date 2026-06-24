"""Self-diagnostic report for the presence engine (yorishiro fork).

Phase 2 of the occupancy work: turns the raw presence log
(``~/.stackchan/presence_log.jsonl``, one JSONL line per poll) into a
health report — how cleanly the TMOS separates "in room" from "empty",
how long the in-room *valleys* (presence dropouts) run, and what
``absent_after_s`` those valleys actually justify. It is the porting of
the hand-run ``scratch/analyze_presence.py`` into a tested, callable
module so the dashboard and a daily writer can both consume it.

Design:

- **Pure functions, no I/O.** :func:`build_report` takes a list of
  already-parsed records plus an injected ``now`` (and timezone), and
  returns a plain dict. The log reading and file writing live in
  :mod:`presence` (the log's owner); rendering to Markdown is
  :func:`render_markdown`. This keeps the aggregation trivially testable
  with synthetic records.
- **No import of :mod:`presence`.** ``presence`` imports this module for
  the daily writer, so the dependency must stay one-way. The absent
  debounce clamp bounds are mirrored as local constants (kept in sync
  with ``presence.MIN/MAX_ABSENT_AFTER_S`` by review, not by import).
- **Valleys match the engine's semantics.** ``presence._occupied`` keeps
  the room occupied while ``monotonic - last_present_mono <
  absent_after_s``. So a "valley" is the wall-clock span from the *last*
  ``present=True`` observation to the *next* one — exactly the gap
  ``absent_after_s`` must bridge. Valleys whose span contains a sampling
  outage (gateway down, ``gap > OUTAGE_GAP_S``) are dropped: a stopped
  gateway is not a person stepping out, and counting it would inflate the
  recommendation. The recommendation is built from **ACTIVE-only**
  valleys, because the sleep latch already bridges the QUIET (sleeping)
  window — ``absent_after_s`` only needs to cover waking-hours absences.
- **Diagnosis only.** The report *recommends* an ``absent_after_s`` but
  never applies it (``auto_apply`` is always False). Closing the loop
  (an approve-and-apply UI) is Phase 3; this upholds the human-in-the-loop
  design principle.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

#: Fixed JST offset (no DST), used only for the hour-of-day buckets and
#: the human-readable timestamps. Injected into :func:`build_report` so
#: tests can pin it to UTC for stable string assertions.
JST = timezone(timedelta(hours=9))

#: A sampling gap larger than this (seconds) is treated as an outage
#: (gateway restart / log truncation), not real occupancy data. Valleys
#: spanning such a gap are excluded from the statistics.
OUTAGE_GAP_S = 60.0

#: Margin applied to the ACTIVE valley p99 when recommending the absent
#: debounce — absorbs the upper tail beyond p99 so a slightly longer
#: real absence does not flap the gate to ABSENT.
RECO_MARGIN = 1.25

#: Recommendation is rounded up to this granularity (seconds) for
#: readability and to stay coarse relative to the poll cadence.
RECO_ROUND_S = 30

#: Tolerance (seconds) below the current threshold within which an ACTIVE
#: valley is treated as *censored* (it hit the absent-debounce ceiling and
#: flipped the room to ABSENT, so its true length is unknown). See
#: :func:`_recommendation`.
CENSOR_TOL_S = 15.0

#: Clamp bounds — mirror ``presence.MIN/MAX_ABSENT_AFTER_S`` (not imported
#: to keep the dependency one-way; keep in sync by review).
MIN_ABSENT_AFTER_S = 5
MAX_ABSENT_AFTER_S = 3600

#: States the engine treats as "someone is in the room".
PRESENT_STATES = ("active", "quiet")


def _fmt(ts: float, tz: timezone, *, with_date: bool = True) -> str:
    """Format an epoch second in the given tz for human display."""
    dt = datetime.fromtimestamp(ts, tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%m-%d %H:%M:%S")


def _stats(vals: list[float]) -> dict[str, Any]:
    """Distribution summary (numbers, not a string) for a value list.

    Percentile indexing matches ``scratch/analyze_presence.py`` so the
    ported numbers line up with the hand-run tool. ``p99`` is added for
    the recommendation. Empty input yields ``{"n": 0}``.
    """
    if not vals:
        return {"n": 0}
    vs = sorted(vals)
    n = len(vs)
    return {
        "n": n,
        "min": vs[0],
        "p10": vs[n // 10],
        "median": vs[n // 2],
        "p90": vs[9 * n // 10],
        "p99": vs[min(n - 1, 99 * n // 100)],
        "max": vs[-1],
        "mean": round(sum(vs) / n, 1),
    }


def _ceil_round(x: float, step: int) -> int:
    return int(math.ceil(x / step) * step)


def _clamp(v: int, lo: int, hi: int) -> int:
    return min(max(v, lo), hi)


def _sorted_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep only records with a usable numeric ``ts_unix``, sorted by it."""
    clean = [
        r
        for r in records
        if isinstance(r.get("ts_unix"), (int, float))
        and not isinstance(r.get("ts_unix"), bool)
    ]
    clean.sort(key=lambda r: r["ts_unix"])
    return clean


def _basic(rows: list[dict[str, Any]], now: float, tz: timezone) -> dict[str, Any]:
    t0, t1 = rows[0]["ts_unix"], rows[-1]["ts_unix"]
    return {
        "samples": len(rows),
        "start_unix": t0,
        "end_unix": t1,
        "start_jst": _fmt(t0, tz),
        "end_jst": _fmt(t1, tz),
        "duration_h": round((t1 - t0) / 3600, 2),
        "stale_s": round(now - t1, 1),
    }


def _sampling(
    rows: list[dict[str, Any]],
    gaps: list[float],
    tz: timezone,
    median_gap: float,
) -> dict[str, Any]:
    big = [(rows[i]["ts_unix"], g) for i, g in enumerate(gaps) if g > 30]
    return {
        "median_gap_s": round(median_gap, 1),
        "max_gap_s": round(max(gaps), 1) if gaps else 0.0,
        "gaps_over_30s": len(big),
        "outages": [
            {"at_unix": ts, "at_jst": _fmt(ts, tz, with_date=False), "gap_s": round(g, 1)}
            for ts, g in big[:10]
        ],
    }


def _states(
    rows: list[dict[str, Any]],
    gaps: list[float],
    median_gap: float,
) -> dict[str, Any]:
    """Per-state dwell time/ratio/samples plus the transition count.

    Dwell time credits each sample the span to the next one; an outage
    gap (> 60 s) is clipped to the median gap so a gateway stop is not
    booked as hours of one state (mirrors analyze_presence.py:55-56).
    """
    by_state: dict[str, dict[str, float]] = {
        st: {"hours": 0.0, "ratio": 0.0, "samples": 0}
        for st in ("active", "quiet", "absent", "unknown")
    }
    time_acc: dict[str, float] = {}
    transitions = 0
    prev_state = None
    for i, r in enumerate(rows):
        st = r.get("state", "unknown")
        dt = gaps[i] if i < len(gaps) else median_gap
        if dt > 60:
            dt = median_gap
        time_acc[st] = time_acc.get(st, 0.0) + dt
        bucket = by_state.setdefault(
            st, {"hours": 0.0, "ratio": 0.0, "samples": 0}
        )
        bucket["samples"] += 1
        if prev_state is not None and st != prev_state:
            transitions += 1
        prev_state = st
    total = sum(time_acc.values()) or 1.0
    for st, secs in time_acc.items():
        by_state[st]["hours"] = round(secs / 3600, 2)
        by_state[st]["ratio"] = round(secs / total, 4)
    out: dict[str, Any] = dict(by_state)
    out["transitions"] = transitions
    return out


def _separation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """presence value distribution split by ``pres_flag`` (TMOS's own bit).

    A wide separation (flag-on p10 above flag-off p90) means the binary
    occupancy decision is well grounded; an overlap warns the threshold
    is mushy. ``insufficient`` when either side is too small to judge.
    """
    on = [
        r["presence"]
        for r in rows
        if r.get("pres_flag") and isinstance(r.get("presence"), (int, float))
    ]
    off = [
        r["presence"]
        for r in rows
        if not r.get("pres_flag") and isinstance(r.get("presence"), (int, float))
    ]
    on_s, off_s = _stats(on), _stats(off)
    if on_s["n"] < 20 or off_s["n"] < 20:
        note = "insufficient"
    elif on_s["p10"] > off_s["p90"]:
        note = "good"
    else:
        note = "overlap"
    return {"on": on_s, "off": off_s, "separation_note": note}


def _segments(
    rows: list[dict[str, Any]], states: tuple[str, ...]
) -> list[list[dict[str, Any]]]:
    """Contiguous runs of rows whose ``state`` is in ``states``."""
    segs: list[list[dict[str, Any]]] = []
    cur: list[dict[str, Any]] = []
    for r in rows:
        if r.get("state") in states:
            cur.append(r)
        elif cur:
            segs.append(cur)
            cur = []
    if cur:
        segs.append(cur)
    return segs


def _segment_valleys(seg: list[dict[str, Any]]) -> list[float]:
    """Presence-dropout spans within one present segment.

    A valley is the span from the *last* ``present=True`` to the *next*
    ``present=True`` (the gap ``absent_after_s`` must bridge — see module
    docstring). A run still open at the segment end is measured to the
    last sample. Any valley whose span contains a sampling outage
    (``gap > OUTAGE_GAP_S``) is dropped as unreliable (gateway down, not a
    real absence). A leading run with no prior ``present=True`` is skipped
    (its start is unknown).
    """
    valleys: list[float] = []
    last_true_ts: float | None = None
    run_open = False
    run_has_outage = False
    prev_ts: float | None = None
    for r in seg:
        ts = r["ts_unix"]
        if prev_ts is not None and run_open and (ts - prev_ts) > OUTAGE_GAP_S:
            run_has_outage = True
        if r.get("present"):
            if run_open and last_true_ts is not None and not run_has_outage:
                valleys.append(ts - last_true_ts)
            run_open = False
            run_has_outage = False
            last_true_ts = ts
        elif not run_open:
            run_open = True
            run_has_outage = False
        prev_ts = ts
    if run_open and last_true_ts is not None and not run_has_outage:
        valleys.append(seg[-1]["ts_unix"] - last_true_ts)
    return valleys


def _all_valleys(rows: list[dict[str, Any]], states: tuple[str, ...]) -> list[float]:
    out: list[float] = []
    segs = _segments(rows, states)
    for seg in segs:
        out.extend(_segment_valleys(seg))
    return out


def _valley_report(raw: list[float], segments: int) -> dict[str, Any]:
    s = _stats(raw)
    return {
        "present_segments": segments,
        "n": s["n"],
        "median_s": round(s.get("median", 0), 1) if raw else 0.0,
        "p90_s": round(s.get("p90", 0), 1) if raw else 0.0,
        "p99_s": round(s.get("p99", 0), 1) if raw else 0.0,
        "max_s": round(s.get("max", 0), 1) if raw else 0.0,
        "over_120s": sum(1 for v in raw if v > 120),
        "over_450s": sum(1 for v in raw if v > 450),
    }


def _recommendation(
    active_valleys: list[float],
    current_absent_after_s: int | None,
) -> dict[str, Any]:
    """Recommend (never apply) an ``absent_after_s`` from ACTIVE valleys.

    ACTIVE valleys are **right-censored** at the current threshold: once a
    presence dropout lasts ``absent_after_s`` the room leaves ACTIVE, so a
    valley can never exceed it. Recommending off that censored tail would
    just return ~``current * margin`` and ratchet the threshold up every
    run. So the basis is the **resolved** valleys (presence returned before
    the threshold); the censored count (waking-hour dropouts that hit the
    ceiling = flipped to ABSENT — a real exit, or a sensor blind spot) is
    surfaced separately for the human to judge.
    """
    if not active_valleys:
        return {
            "current_absent_after_s": current_absent_after_s,
            "recommended_absent_after_s": None,
            "rationale": "ACTIVE 帯の谷間サンプルが無く推奨値を算出できません"
            "（在室データの蓄積を待ってください）。",
            "false_absent_risk_at_recommended": 0,
            "censored_active_dropouts": 0,
            "auto_apply": False,
        }
    censored = 0
    basis = active_valleys
    if current_absent_after_s is not None:
        cutoff = current_absent_after_s - CENSOR_TOL_S
        resolved = [v for v in active_valleys if v < cutoff]
        censored = len(active_valleys) - len(resolved)
        if resolved:  # fall back to all valleys only if everything censored
            basis = resolved
    p99 = _stats(basis)["p99"]
    recommended = _clamp(
        _ceil_round(p99 * RECO_MARGIN, RECO_ROUND_S),
        MIN_ABSENT_AFTER_S,
        MAX_ABSENT_AFTER_S,
    )
    # Risk over *all* observed ACTIVE valleys (censored ones sit just under
    # the current ceiling, so a higher recommendation covers them too).
    risk = sum(1 for v in active_valleys if v >= recommended)
    rationale = (
        f"在室中に presence が戻った谷間（resolved, n={len(basis)}）の "
        f"p99={p99:.0f}s に余裕係数 {RECO_MARGIN} を掛け {RECO_ROUND_S}s 丸めで "
        f"{recommended}s。ACTIVE 谷間は現しきい値で打ち切られるため、戻り済みの"
        "谷間のみを母数にしています（就寝帯は sleep latch が埋めるため除外）。"
    )
    if censored:
        rationale += (
            f" ACTIVE 中に {censored} 回、presence が現しきい値"
            f"（{current_absent_after_s}s）まで途切れて不在判定されました"
            "（真の離席かセンサー死角かは要観察）。"
        )
    if current_absent_after_s is not None and recommended:
        ratio = current_absent_after_s / recommended
        too_short_signal = censored >= max(5, len(active_valleys) // 10)
        if ratio >= 1.5:
            rationale += (
                f" 現状 {current_absent_after_s}s は推奨の約 {ratio:.1f} 倍で"
                "過剰に保守的（離席後 ABSENT までが遅い）。"
            )
        elif too_short_signal:
            rationale += (
                f" 現状 {current_absent_after_s}s では打ち切りが多く、"
                "離席でなければしきい値が短すぎる可能性。"
            )
        else:
            rationale += f" 現状 {current_absent_after_s}s は推奨とほぼ整合。"
    return {
        "current_absent_after_s": current_absent_after_s,
        "recommended_absent_after_s": recommended,
        "rationale": rationale,
        "false_absent_risk_at_recommended": risk,
        "censored_active_dropouts": censored,
        "auto_apply": False,
    }


def _hourly(rows: list[dict[str, Any]], tz: timezone) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        key = datetime.fromtimestamp(r["ts_unix"], tz).strftime("%m-%d %H")
        buckets.setdefault(key, []).append(r)
    out: list[dict[str, Any]] = []
    for hour in sorted(buckets):
        rs = buckets[hour]
        n = len(rs)
        pres_rate = sum(1 for r in rs if r.get("pres_flag")) / n
        presv = sorted(
            r["presence"] for r in rs if isinstance(r.get("presence"), (int, float))
        )
        tempv = sorted(
            r["ambient_c"] for r in rs if isinstance(r.get("ambient_c"), (int, float))
        )
        out.append(
            {
                "hour": hour,
                "n": n,
                "pres_rate": round(pres_rate, 3),
                "presence_median": presv[len(presv) // 2] if presv else None,
                "ambient_median_c": round(tempv[len(tempv) // 2], 1) if tempv else None,
            }
        )
    return out


def build_report(
    records: list[dict[str, Any]],
    *,
    now: float,
    tz: timezone = JST,
    current_absent_after_s: int | None = None,
    median_gap_default: float = 10.0,
) -> dict[str, Any]:
    """Aggregate presence log records into a self-diagnostic report.

    ``records`` need not be sorted (sorted internally; records without a
    numeric ``ts_unix`` are dropped). ``now`` is injected for the data
    freshness (``stale_s``) and testability. ``current_absent_after_s``
    lets the recommendation compare against the live threshold.
    """
    rows = _sorted_records(records)
    if not rows:
        return {"ok": True, "empty": True, "generated_at": now, "basic": {"samples": 0}}

    gaps = [rows[i + 1]["ts_unix"] - rows[i]["ts_unix"] for i in range(len(rows) - 1)]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else median_gap_default

    valleys_all = _all_valleys(rows, PRESENT_STATES)
    valleys_active = _all_valleys(rows, ("active",))

    return {
        "ok": True,
        "empty": False,
        "generated_at": now,
        "basic": _basic(rows, now, tz),
        "sampling": _sampling(rows, gaps, tz, median_gap),
        "states": _states(rows, gaps, median_gap),
        "presence_separation": _separation(rows),
        "valleys": _valley_report(valleys_all, len(_segments(rows, PRESENT_STATES))),
        "valleys_active": _valley_report(
            valleys_active, len(_segments(rows, ("active",)))
        ),
        "recommendation": _recommendation(valleys_active, current_absent_after_s),
        "hourly": _hourly(rows, tz),
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render a report dict as a human-readable Markdown document."""
    if report.get("empty"):
        return "# 在室診断レポート\n\nデータがありません（在室ログが空）。\n"
    b = report["basic"]
    s = report["sampling"]
    st = report["states"]
    sep = report["presence_separation"]
    va = report["valleys_active"]
    vall = report["valleys"]
    rec = report["recommendation"]
    lines: list[str] = []
    lines.append("# 在室診断レポート")
    lines.append("")
    lines.append(f"- 期間: {b['start_jst']} 〜 {b['end_jst']} JST（{b['duration_h']}h）")
    lines.append(f"- サンプル数: {b['samples']}　鮮度: {b['stale_s']}s 前")
    lines.append(
        f"- サンプル間隔: 中央 {s['median_gap_s']}s / 最大 {s['max_gap_s']}s"
        f"　欠測(>30s): {s['gaps_over_30s']} 件"
    )
    lines.append("")
    lines.append("## state 別 滞在")
    for key in ("active", "quiet", "absent", "unknown"):
        d = st.get(key, {})
        lines.append(
            f"- {key}: {d.get('hours', 0)}h ({d.get('ratio', 0) * 100:.1f}%)"
            f" n={d.get('samples', 0)}"
        )
    lines.append(f"- 遷移回数: {st.get('transitions', 0)}")
    lines.append("")
    lines.append("## presence 分離度（在室判定の確かさ）")
    lines.append(f"- 判定: **{sep['separation_note']}**")
    lines.append(f"- pres_flag=True : {sep['on']}")
    lines.append(f"- pres_flag=False: {sep['off']}")
    lines.append("")
    lines.append("## 在室中の谷間（presence 喪失の連続時間）")
    lines.append(
        f"- 全体(active+quiet): n={vall['n']} median={vall['median_s']}s"
        f" p90={vall['p90_s']}s p99={vall['p99_s']}s max={vall['max_s']}s"
    )
    lines.append(
        f"- ACTIVE 限定: n={va['n']} median={va['median_s']}s"
        f" p90={va['p90_s']}s p99={va['p99_s']}s max={va['max_s']}s"
        f"（>450s: {va['over_450s']} 件）"
    )
    lines.append("")
    lines.append("## 推奨 absent_after_s（提示のみ・自動適用しない）")
    lines.append(f"- 現状: {rec['current_absent_after_s']}s")
    lines.append(f"- 推奨: {rec['recommended_absent_after_s']}s")
    lines.append(
        f"- 推奨採用時の誤 ABSENT リスク: {rec['false_absent_risk_at_recommended']} 件"
    )
    lines.append(
        f"- ACTIVE 中の打ち切り（しきい値到達）: {rec.get('censored_active_dropouts', 0)} 回"
    )
    lines.append(f"- 根拠: {rec['rationale']}")
    lines.append("")
    return "\n".join(lines) + "\n"
