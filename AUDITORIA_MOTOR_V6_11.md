# KAELEON v6.11 — Auditoría del motor y corrección del lifecycle gap

## Resultado ejecutivo

La revisión del pipeline completo no encontró un crash general del motor. El problema principal identificado está antes de la ejecución: el lifecycle `BREAKOUT_RETEST` podía perder setups válidos entre el descubrimiento directo y el estado `WATCHING`.

La versión recibida ya incluía la regla adaptativa v6.10 para permitir un retest de `0.085 ATR` en una tendencia 5m fuerte y fresca, conservando `0.12 ATR` como regla estándar. Sin embargo, esa adaptación solo se aplicaba al camino `WATCHING -> ARMED`. El descubrimiento directo seguía usando un `pullback < 0.12` hard-coded. Además, el precursor de breakout solo se buscaba hasta 2 velas de antigüedad, mientras que la ventana adaptativa considera hasta 3 velas.

Esto creaba un hueco real: un símbolo podía entrar al scanner cuando el breakout ya tenía 3 velas, no cumplir el `0.12 ATR` fijo del camino directo y tampoco poder crear `WATCHING`. El setup desaparecía del lifecycle antes de llegar a `ARMED`.

## Corrección aplicada

En `app/strategy/armed_entry.py`:

- `WATCH_BREAKOUT_MAX_AGE_BARS` cambia de `2` a `3`.
- El descubrimiento directo de `BREAKOUT_RETEST` usa ahora `_adaptive_retest_pullback_requirement(...)`, la misma política utilizada por `WATCHING -> ARMED`.
- El contexto de tendencia 5m se calcula una sola vez por discovery pass, no dentro de cada iteración del retest.
- El setup directo guarda `retest_pullback_atr` y los diagnósticos de la regla adaptativa en metadata.
- Se mantienen sin cambios los guards de penetración, invalidación por cierre, `too_extended`, stop, RR, HTF exhaustion, anti-chase y confirmación 1m.

En `tests/test_armed_entry_v6.py`:

- Se agregó un regression test que demuestra que el discovery directo obedece la regla dinámica en lugar de un `0.12` hard-coded.
- Se agregó un regression test que confirma que un breakout con 3 velas de antigüedad todavía puede entrar en `WATCHING`.

## Pipeline auditado

Se revisaron: activación del worker, scanner/rotación, snapshots MTF, RegimeEngine, state machine, StrategyRouter, `discover_armed`, `discover_watch`, `advance_watch`, `trigger_armed`, confirmación 1m, TTL/cooldowns, guards de ejecución, runtime, open-position guard, reconciliation/pending execution, risk manager y funnel de rechazos.

### Hallazgos adicionales relevantes

`TRADING_WORKER_ENABLED` tiene default `false`. Si el servicio que debe ejecutar el motor no lo sobreescribe con `true`, la API puede estar sana mientras no se analiza ni se opera. Debe verificarse en el servicio worker/motor. No se cambió el default porque en una arquitectura con backend + worker separado activar ambos puede crear una segunda instancia; existe WorkerLease precisamente para evitar duplicados.

`TRADE_LEGACY_ENTRY_FALLBACK_ENABLED=false` significa que no existe una entrada inmediata alternativa cuando `discover_armed` y `discover_watch` no producen lifecycle. Se mantuvo así: habilitar el fallback para “forzar operaciones” saltaría la arquitectura staged y podría degradar la calidad.

El monitor `WATCHING` recibe snapshot MTF completo. El monitor `ARMED` recibe quote + 1m + contexto 5m suficiente para la revalidación de tendencia. No se encontró un bloqueo por ausencia de timeframes en esos caminos.

El `RegimeEngine` solo habilita ejecución en dominios compatibles (`TREND_CONTINUATION` para breakout y `VOLATILE_SWEEP` para sweep). `RANGE`/`UNKNOWN` pueden reducir la frecuencia, pero no representan por sí mismos un deadlock.

El scanner default cubre 12 símbolos con paralelismo 3. Aumentarlo puede mejorar cobertura sin relajar calidad, pero aumenta llamadas a CoinW. No se modificó todavía porque debe medirse después del fix v6.11 antes de aumentar carga.

En LIVE existe un guard de `pending_execution`/reconciliation que puede bloquear nuevas entradas si una orden anterior queda pendiente de reconciliación. No se debe auto-limpiar a ciegas porque puede provocar órdenes duplicadas. Si el problema ocurre en LIVE, debe comprobarse explícitamente ese estado.

Los TTL/cooldowns revisados no explican por sí solos 24 horas de sequía: watch TTL 30m, armed TTL 10m, consumed setup TTL 1h y cooldowns post-loss mucho menores que 24h.

## Verificación local

- `python -m compileall -q app scripts` -> OK.
- `pytest -q` -> `240 passed`.
- Antes de la corrección la misma suite tenía `238 passed`; se agregaron dos pruebas de regresión específicas para el hueco corregido.

Pasar tests no garantiza que el mercado vaya a producir una operación en un período fijo. Sí confirma que el motor ya no pierde setups por la inconsistencia v6.10 encontrada y que no se introdujo una regresión detectable por la suite actual.

## Variables a verificar en Railway antes del deploy

En el servicio que realmente ejecuta el motor:

```env
TRADING_WORKER_ENABLED=true
TRADE_ARMED_ENTRY_ENABLED=true
TRADE_LEGACY_ENTRY_FALLBACK_ENABLED=false
TRADE_ARMED_SETUP_TTL_SECONDS=600
TRADE_SETUP_WATCH_TTL_SECONDS=1800
TRADE_ARMED_MONITOR_POLL_SECONDS=2.0
TRADE_SETUP_WATCH_POLL_SECONDS=15.0
MARKET_SCANNER_DEPTH=12
MARKET_SCANNER_PARALLEL=3
```

No se recomienda modificar todavía RR, `too_extended`, chase, stop o confirmación 1m para intentar generar operaciones.

## Qué observar después del deploy

El objetivo inmediato no es solo `FILLED`; la progresión sana debe verse como:

`ANALYZED -> WATCHING -> ARMED -> TRIGGERED -> SIGNAL_ACCEPTED -> RISK_APPROVED -> SUBMITTED -> FILLED`.

Si vuelve a haber sequía, el siguiente ajuste debe basarse en el primer stage que permanezca en cero. Si `WATCHING` aumenta pero `ARMED=0`, revisar razones de promoción. Si `ARMED>0` pero `TRIGGERED=0`, revisar confirmación 1m / RR / chase. Si `TRIGGERED>0` pero `FILLED=0`, el problema ya está en guards de ejecución, riesgo o broker/reconciliation.

## Siguiente ajuste condicionado por evidencia

Si tras desplegar v6.11 el pipeline progresa pero la cobertura sigue siendo demasiado baja, el siguiente cambio recomendado es aumentar primero el universo del scanner de forma moderada (por ejemplo depth 12 -> 18 y parallel 3 -> 4), midiendo latencia y rate limits. Esto aumenta oportunidades sin rebajar la geometría ni los filtros de riesgo.
