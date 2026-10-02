from __future__ import annotations

import time
from collections import Counter


class RejectionFunnel:
    """Small in-memory decision funnel for production diagnostics.

    It intentionally stores counters only. No candle/indicator payloads are retained,
    so the runtime can expose exactly which gate is drying the bot without creating a
    second telemetry database.
    """

    def __init__(self, *, emit_seconds: float = 300.0, emit_every: int = 50):
        self.emit_seconds = max(30.0, float(emit_seconds))
        self.emit_every = max(5, int(emit_every))
        self.started_at = time.time()
        self.last_emit = time.monotonic()
        self.total = 0
        self.stages: Counter[str] = Counter()
        self.reasons: Counter[str] = Counter()

    def record(self, stage: str, reason: str | None = None) -> None:
        stage = str(stage or "unknown")
        self.stages[stage] += 1
        if stage == "analyzed":
            self.total += 1
        if reason:
            self.reasons[f"{stage}:{reason}"] += 1

    def should_emit(self) -> bool:
        elapsed = time.monotonic() - self.last_emit
        return bool(self.total and (self.total % self.emit_every == 0 or elapsed >= self.emit_seconds))

    def payload(self) -> dict:
        analyzed = max(self.stages.get("analyzed", 0), 1)
        return {
            "window_started_at": self.started_at,
            "analyzed": self.stages.get("analyzed", 0),
            "armed": self.stages.get("armed", 0),
            "triggered": self.stages.get("triggered", 0),
            "signal_accepted": self.stages.get("signal_accepted", 0),
            "risk_approved": self.stages.get("risk_approved", 0),
            "submitted": self.stages.get("submitted", 0),
            "filled": self.stages.get("filled", 0),
            "post_trigger_rejected": self.stages.get("post_trigger_rejected", 0),
            "execution_rejected": self.stages.get("execution_rejected", 0),
            "execution_error": self.stages.get("execution_error", 0),
            "armed_rate": round(self.stages.get("armed", 0) / analyzed, 4),
            "trigger_rate": round(self.stages.get("triggered", 0) / analyzed, 4),
            "fill_rate": round(self.stages.get("filled", 0) / analyzed, 4),
            "stages": dict(self.stages.most_common()),
            "top_rejections": [
                {"reason": key, "count": count}
                for key, count in self.reasons.most_common(12)
            ],
            "top_post_trigger_blockers": [
                {"reason": key, "count": count}
                for key, count in self.reasons.most_common()
                if key.startswith("post_trigger_rejected:")
            ][:12],
            "top_execution_blockers": [
                {"reason": key, "count": count}
                for key, count in self.reasons.most_common()
                if key.startswith("execution_rejected:") or key.startswith("execution_error:")
            ][:12],
        }

    def mark_emitted(self) -> None:
        self.last_emit = time.monotonic()
