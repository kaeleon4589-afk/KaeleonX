# KAELEON v6.17 — Execution Funnel Observability

## Motivo

Los logs de producción mostraron una señal acumulada que alcanzó:

- `triggered = 1`
- `signal_accepted = 1`
- `risk_approved = 1`
- `submitted = 0`
- `filled = 0`

La causa exacta no aparecía en `REJECTION_FUNNEL` porque varios bloqueos posteriores a `RISK_APPROVED` y anteriores a `ORDER_SUBMITTED` emitían `EXECUTION_REJECTED` en el log, pero no incrementaban el funnel.

## Cambio

Se instrumentan en `REJECTION_FUNNEL` todos los bloqueos de ejecución previos al submit:

- `execution_rejected:market_unavailable`
- `execution_rejected:fill_outside_trade_geometry`
- `execution_rejected:open_position_exists`
- `execution_rejected:execution_pending`
- `execution_rejected:setup_already_executed`

También se instrumentan errores de ejecución:

- `execution_error:trading_worker_lease_expired`
- `execution_error:execution_submit_error`
- `execution_error:filled_result_missing_position`
- `execution_error:pipeline_cancelled_after_signal_accept`
- `execution_error:orchestrator_unhandled:<ExceptionType>`
- `execution_error:accepted_signal_without_terminal_event`

El payload de `REJECTION_FUNNEL` ahora incluye además:

- `execution_rejected`
- `execution_error`
- `top_execution_blockers`

## Qué NO cambia

No se modifican:

- BREAKOUT_RETEST
- LIQUIDITY_SWEEP
- RR mínimo
- Stop Loss
- TP
- filtros 1m/5m/HTF
- anti-chase
- universo 30/5
- tamaño de posición
- modo DEMO/LIVE

## Validación

- pruebas específicas de observabilidad de ejecución: 4/4
- suite completa: 254 passed
- `python -m compileall -q app scripts`: OK

## Resultado esperado en próximos logs

Si vuelve a ocurrir `risk_approved > submitted`, el mismo `REJECTION_FUNNEL` debe exponer la causa, por ejemplo:

```text
risk_approved: 2
submitted: 1
execution_rejected: 1
execution_error: 0

top_execution_blockers:
- execution_rejected:market_unavailable = 1
```

Así el tramo `RISK_APPROVED -> ORDER_SUBMITTED` deja de ser una zona ciega.
