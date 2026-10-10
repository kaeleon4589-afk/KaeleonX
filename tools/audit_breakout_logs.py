#!/usr/bin/env python3
"""Summarize BREAKOUT_RETEST telemetry exported from Railway.

Usage: python tools/audit_breakout_logs.py logs*.txt
Only reports evidence present in input; gross PnL in POSITION_CLOSED is NOT net.
"""
from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
import sys


def parse_events(paths: list[str]) -> list[dict]:
    events = []
    for name in paths:
        for line in Path(name).read_text(encoding="utf-8", errors="replace").splitlines():
            start = line.find("payload={")
            if start < 0:
                continue
            try:
                event = json.loads(line[start + len("payload="):])
            except json.JSONDecodeError:
                continue  # Partial or truncated export
            if isinstance(event, dict) and event.get("event"):
                events.append(event)
    return events


def report(events: list[dict]) -> dict:
    # Different users can share a setup; deduplicate by lifecycle *and* user.
    triggered = {}
    opened = {}
    closed = {}
    accepted = set()
    pending = Counter()
    cancel = Counter()
    for e in events:
        if e.get("strategy") != "BREAKOUT_RETEST":
            continue
        event = e.get("event")
        if event == "SETUP_ARMED_PROGRESS":
            pending[str(e.get("reason") or "unknown")] += 1
        elif event == "SETUP_CANCELLED":
            cancel[str(e.get("reason") or "unknown")] += 1
        elif event == "SETUP_TRIGGERED":
            triggered[(e.get("user_id"), e.get("setup_id"))] = e
        elif event == "SIGNAL_ACCEPTED":
            accepted.add(e.get("decision_id"))
        elif event == "POSITION_OPENED":
            opened[e.get("position_id")] = e
        elif event == "POSITION_CLOSED":
            closed[e.get("position_id")] = e
    results = [e for e in closed.values() if e.get("gross_pnl") is not None]
    pnl = sum(float(e["gross_pnl"]) for e in results)
    win = sum(float(e["gross_pnl"]) > 0 for e in results)
    loss = sum(float(e["gross_pnl"]) < 0 for e in results)
    modes = Counter(str((e.get("trace") or {}).get("confirmation_mode") or "unavailable") for e in triggered.values())
    return {
        "records_imported": len(events),
        "distinct_triggered_user_setups": len(triggered),
        "confirmation_modes": dict(modes),
        "accepted_decisions": len(accepted),
        "opened_positions": len(opened),
        "closed_positions": len(closed),
        "wins_gross": win,
        "losses_gross": loss,
        "win_rate_gross_pct": round(100 * win / len(results), 2) if results else None,
        "gross_pnl_usdt_before_fees": round(pnl, 6),
        "pending_poll_counts_not_unique_signals": dict(pending.most_common()),
        "cancel_counts": dict(cancel.most_common()),
        "note": "No se calcula PnL neto: los eventos POSITION_CLOSED contienen PnL bruto. Exportaciones parciales pueden omitir entradas/cierres.",
    }


def main() -> int:
    if len(sys.argv) < 2:
        print("Uso: python tools/audit_breakout_logs.py logs.XXXXXXXX.log.txt [...]", file=sys.stderr)
        return 2
    try:
        output = report(parse_events(sys.argv[1:]))
    except OSError as exc:
        print(f"No se pudo leer un log: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
