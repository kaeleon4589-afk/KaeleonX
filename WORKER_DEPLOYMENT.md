# Railway: API + trading worker in one service

Kaeleon has two runtime loops: FastAPI serves HTTP; the trading worker (`app.main`) scans CoinW and runs strategies.
If Railway starts only Uvicorn, trading never executes.

For the lowest-cost single-service deployment set:

```env
TRADING_WORKER_ENABLED=true
LOG_LEVEL=INFO
ENGINE_HEARTBEAT_SECONDS=900
```

Use one backend replica while the embedded worker is enabled. `railway.toml` starts Uvicorn with `--no-access-log` to avoid noisy request logs.

`GET /health` exposes `trading_worker_enabled` and `trading_worker_running`.

Expected startup logs include `WORKER_STARTED`; every 15 minutes `ENGINE_HEARTBEAT` confirms the market loop remains alive. Regime/strategy state changes plus position open/close remain visible at INFO.
