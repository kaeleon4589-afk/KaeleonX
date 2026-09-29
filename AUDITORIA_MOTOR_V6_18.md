# KAELEON v6.18 — Fresh Execution Quote

## Evidencia de producción

Con v6.17 el funnel nuevo expuso por primera vez el bloqueo real del tramo posterior a riesgo:

- `armed = 1`
- `triggered = 1`
- `signal_accepted = 1`
- `risk_approved = 1`
- `submitted = 0`
- `filled = 0`
- `execution_rejected = 1`
- `top_execution_blockers = execution_rejected:market_unavailable`

El mismo bloqueo quedó acumulado en todos los runtimes de usuario, por lo que el fallo es compartido por el snapshot de mercado y no depende de una cuenta concreta.

## Raíz encontrada

`MarketCoordinator.snapshot()` ya descargaba las velas primero y pedía el bid/ask al final, evitando cotizaciones envejecidas durante llamadas lentas a CoinW.

`MarketCoordinator.armed_snapshot()` hacía algo diferente: lanzaba en paralelo el depth/quote, las velas 1m y el contexto 5m. Si `depth()` respondía rápido pero `klines()` tardaba varios segundos, `quote_received_ms` quedaba marcado al principio. El snapshot solo se entregaba después de que terminaran las velas y podía llegar al guard de ejecución con más de 10 segundos de antigüedad.

El orquestador entonces rechazaba una señal ya aprobada como `market_unavailable` antes de `ORDER_SUBMITTED`.

## Corrección

En `armed_snapshot()`:

1. Se obtienen primero 1m y contexto 5m.
2. Solo después se solicita el order book ejecutable.
3. El bid/ask entregado al pipeline queda temporalmente pegado al momento de ejecución.

No se falsea ni se reescribe `quote_received_ms`; se obtiene una cotización realmente nueva.

Además, `EXECUTION_REJECTED market_unavailable` ahora incluye:

- `market_bid`
- `market_ask`
- `quote_received_ms`
- `quote_age_ms`
- `orderbook_valid`

## Qué NO cambia

No se modifican:

- BREAKOUT_RETEST
- LIQUIDITY_SWEEP
- RR mínimo
- SL / TP
- confirmación 1m / 5m
- HTF
- anti-chase
- universo 30/5
- tamaño de posición
- modo DEMO/LIVE

## Validación

- prueba dirigida de orden temporal: el depth se solicita después de terminar 1m y 5m
- prueba de quote age: el snapshot ARMED termina con cotización fresca
- telemetría de quote age en rechazo
- suite completa: `255 passed`
- `python -m compileall -q app scripts`: OK

## Resultado esperado

Una señal válida debe poder recorrer:

`ARMED -> TRIGGERED -> SIGNAL_ACCEPTED -> RISK_APPROVED -> ORDER_SUBMITTED -> FILLED`

Si CoinW devuelve realmente un book inválido o el quote vuelve a envejecer, el evento `EXECUTION_REJECTED` mostrará el bid, ask y `quote_age_ms` exactos.
