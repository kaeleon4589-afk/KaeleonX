from __future__ import annotations

from app.execution.paper import PaperExecutionEngine


class DemoExecutionEngine(PaperExecutionEngine):
    """KAELEON internal demo account.

    It uses the same simulator mechanics as the former paper engine, while its
    market input comes from live CoinW data. No CoinW order is ever submitted.
    """

    mode = "demo"
