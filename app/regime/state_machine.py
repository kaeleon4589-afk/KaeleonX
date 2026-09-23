from __future__ import annotations

import os

DEFAULT_CONFIRM_BARS = 3
DEFAULT_COOLDOWN_BARS = 2
DEFAULT_MIN_ACTIVE_BARS = 3
UNKNOWN = "UNKNOWN"


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 0)
    except Exception:
        return default


def _normalise(prev):
    p = dict(prev or {})
    return {
        "active_regime": str(p.get("active_regime") or p.get("active") or UNKNOWN),
        "candidate_regime": str(p.get("candidate_regime") or p.get("candidate") or UNKNOWN),
        "pending_regime": str(p.get("pending_regime") or p.get("pending") or ""),
        "pending_count": max(int(p.get("pending_count") or 0), 0),
        "bars_in_active": max(int(p.get("bars_in_active") or p.get("bars") or 0), 0),
        "cooldown_remaining": max(int(p.get("cooldown_remaining") or p.get("cooldown") or 0), 0),
        "transitions": max(int(p.get("transitions") or 0), 0),
    }


def advance(candidate, prev=None, confirm_bars=None, cooldown_bars=None, min_active_bars=None):
    """Source state machine with compatibility aliases for KAELEON logs."""
    confirm_bars = max(int(confirm_bars if confirm_bars is not None else _env_int("REGIME_CONFIRM_BARS", DEFAULT_CONFIRM_BARS)), 1)
    cooldown_bars = max(int(cooldown_bars if cooldown_bars is not None else _env_int("REGIME_COOLDOWN_BARS", DEFAULT_COOLDOWN_BARS)), 0)
    min_active_bars = max(int(min_active_bars if min_active_bars is not None else _env_int("REGIME_MIN_ACTIVE_BARS", DEFAULT_MIN_ACTIVE_BARS)), 0)
    candidate = str(candidate or UNKNOWN).strip().upper() or UNKNOWN

    p = _normalise(prev)
    active = p["active_regime"]
    pending = p["pending_regime"]
    count = p["pending_count"]
    bars = p["bars_in_active"]
    cooldown = max(p["cooldown_remaining"] - 1, 0)
    transitions = p["transitions"]
    changed = False

    if active == UNKNOWN and bars <= 0:
        if candidate != UNKNOWN:
            active = candidate
        bars = 1
        pending = ""
        count = 0
    elif candidate == active:
        bars += 1
        pending = ""
        count = 0
    else:
        if candidate == pending:
            count += 1
        else:
            pending = candidate
            count = 1

        required = 1 if active == UNKNOWN and candidate != UNKNOWN else confirm_bars
        can_switch = cooldown <= 0 and bars >= min_active_bars
        if active == UNKNOWN:
            can_switch = True
        if candidate == UNKNOWN:
            can_switch = can_switch and count >= confirm_bars

        if can_switch and count >= required:
            active = candidate
            bars = 1
            cooldown = cooldown_bars
            transitions += 1
            changed = True
            pending = ""
            count = 0
        else:
            bars += 1

    return {
        # Source names.
        "active_regime": active,
        "candidate_regime": candidate,
        "pending_regime": pending,
        "pending_count": count,
        "bars_in_active": bars,
        "cooldown_remaining": cooldown,
        "transitions": transitions,
        "changed": changed,
        # Compatibility aliases consumed by existing observability/UI code.
        "active": active,
        "candidate": candidate,
        "pending": pending,
        "bars": bars,
        "cooldown": cooldown,
    }
