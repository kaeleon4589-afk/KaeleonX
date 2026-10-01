from __future__ import annotations

# KAELEON rebuilt market-regime engine.  The legacy v6 detector remains in
# app/regime/advanced.py for rollback/reference, but production routing now uses V2.
from app.regime.v2 import RegimeEngineV2


class RegimeEngine(RegimeEngineV2):
    pass
