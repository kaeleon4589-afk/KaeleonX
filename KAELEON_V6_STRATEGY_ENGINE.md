# KAELEON Strategy Engine v6

Base de implementación: `KaeleonX-breakout-retest-v5-exhaustion-stop.zip`.

## Objetivo

La v6 corrige el conflicto de la v5 entre "entrar temprano" y exigir confirmaciones/stop que obligaban a esperar demasiado. El cambio principal es separar la detección del setup de la ejecución:

`5m estructura -> SETUP_ARMED -> 1m confirmación -> precio ejecutable -> orden`

Ya no se necesita esperar una vela 5m desarrollada para crear la orden.

## Flujo v6

1. El régimen sigue aportando contexto, pero no bloquea por duplicado todos los setups.
2. `BREAKOUT_RETEST` puede armarse después de breakout + retest válido, antes de una confirmación 5m tardía.
3. `LIQUIDITY_SWEEP` puede armarse en `VOLATILE_SWEEP` y también en `RANGE` cuando la estructura del sweep es válida.
4. El setup guarda trigger, invalidación, zona máxima de entrada, SL, TP, calidad y expiración.
5. La entrada necesita una vela 1m cerrada con dirección, cuerpo, posición de cierre y volumen relativo compatibles.
6. Si el precio invalida la estructura, excede la zona de entrada o expira el setup, se cancela sin ordenar.
7. El orquestador vuelve a validar geometría, RR, spread/orderbook, riesgo operativo y disponibilidad antes de enviar la orden.

## Cambios de protección

- `TRADE_ENTRY_MIN_STOP_ATR` baja de `0.90` a `0.30` en producción. Es un filtro final de ruido, no un segundo sistema de stop.
- La v6 usa el stop estructural propio del setup y no exige el antiguo piso de `0.95 ATR` del modelo v5 para poder armar una entrada.
- El anti-chase continúa activo, pero la v6 además tiene `entry_zone_low` / `entry_zone_high`; si el precio se escapa de esa zona el setup se cancela.
- `UNKNOWN` continúa bloqueado. `RANGE` deja de significar "ninguna estrategia posible" para sweeps de alta calidad.
- El modelo de capital no cambia: `RiskManager` sigue usando el capital configurado como margen y no dimensiona por porcentaje de riesgo del stop.

## Micro-confirmación 1m

El coordinador solicita 1m como feed adicional. Las velas abiertas se excluyen y se comprueba frescura/continuidad. Una falla temporal del feed 1m no rompe reconciliación ni monitoreo, pero la v6 no degrada silenciosamente a una entrada sin confirmación: mantiene el setup esperando.

Criterios de confirmación por defecto:

- body ratio >= `0.22`
- relative volume >= `0.70`
- cierre LONG >= 60% del rango de la vela
- cierre SHORT <= 40% del rango de la vela
- precio ejecutable dentro de la zona de entrada

## Rejection funnel

Se agregó un embudo en memoria que contabiliza:

- snapshots analizados
- setups armados
- setups pendientes/cancelados
- triggers
- señales aceptadas
- aprobaciones/rechazos de riesgo
- envíos de orden
- rechazos de ejecución
- fills
- razones principales de rechazo

El resumen se emite como `REJECTION_FUNNEL` y permite identificar en Railway qué etapa está secando el bot sin volcar series completas de indicadores.

## Variables nuevas / modificadas

```env
TRADE_ENTRY_MIN_STOP_ATR=0.30
TRADE_ARMED_ENTRY_ENABLED=true
TRADE_LEGACY_ENTRY_FALLBACK_ENABLED=false
TRADE_ARMED_SETUP_TTL_SECONDS=600
TRADE_ARMED_CHASE_TOLERANCE_ATR=0.15
TRADE_ARMED_TRIGGER_CLOSE_TOLERANCE_ATR=0.08
TRADE_ARMED_CONSUMED_TTL_SECONDS=3600
TRADE_FUNNEL_EMIT_SECONDS=300
TRADE_FUNNEL_EMIT_EVERY=50
```

`TRADE_LEGACY_ENTRY_FALLBACK_ENABLED=false` es intencional. Evita que, cuando la v6 no arma un setup, el sistema vuelva inmediatamente al modelo v5 que podía entrar tarde. Puede activarse temporalmente como rollback operativo.

## Compatibilidad

El router v5 se conserva. Routers de tests o integraciones que no implementen `discover_armed`/`trigger_armed` siguen utilizando el flujo anterior. Esto permite rollback y evita romper integraciones existentes.

## Validación realizada

- `pytest -q`: **187 passed**
- `python -m compileall -q app`: sin errores
- pruebas v6 añadidas para:
  - sweep armable en `RANGE`
  - espera obligatoria de confirmación 1m
  - creación de `TradeIntent` después del trigger
  - cancelación por chase fuera de zona
  - expiración de setup
  - orquestador `ARMED -> TRIGGERED -> FILLED` sin ejecutar el fallback legacy

Esto valida la implementación y regresiones de software. No equivale por sí solo a una validación de rentabilidad con mercado real.

## Replay / backtest offline

Se incluye `app/backtest/replay.py` y el CLI `scripts/replay_strategy.py`. El replay consume CSV OHLCV de 1m/5m/15m/1h y pasa los datos por el mismo ciclo `discover_armed -> trigger_armed` de la v6. Reporta:

- trades, wins/losses y win rate
- profit factor en R
- expectancy y R acumulada
- max drawdown en R
- MAE/MFE promedio
- retorno de margen aproximado con leverage/fees configurables
- razones de setups rechazados/cancelados

Ejemplo:

```bash
python scripts/replay_strategy.py \
  --symbol BTC \
  --1m data/BTC-1m.csv \
  --5m data/BTC-5m.csv \
  --15m data/BTC-15m.csv \
  --1h data/BTC-1h.csv \
  --output replay-report.json
```

El simulador usa OHLC de 1m y, si SL y TP aparecen tocados dentro de la misma vela, asume SL primero. Es una regla conservadora y evita atribuir fills intrabar favorables que no pueden demostrarse con OHLC. Antes de aumentar capital LIVE debe ejecutarse con histórico real y revisar expectancy, drawdown, MAE/MFE y el funnel de rechazos.


## Validation profile: proportional TP + fixed initial SL

For the strategy-effectiveness phase, KAELEON uses a deliberately transparent
exit profile:

- `TRADE_LIQUIDITY_SWEEP_TARGET_RR=1.30`
- `TRADE_BREAKOUT_RETEST_TARGET_RR=1.50`
- `TRADE_DYNAMIC_PROTECTION_ENABLED=false`

The structural target is still calculated and stored for diagnostics, but the
executable TP is capped to the strategy RR above whenever the structural target
is farther away. If structure is closer (and still clears the minimum RR gate),
the closer structural TP is preserved.

With dynamic protection disabled, KAELEON keeps the original stop unchanged; it
does not move it to fee-aware break-even and does not activate profit-lock. The
engine still tracks the best favorable price for analysis. Existing positions
whose stop had already been tightened before disabling this setting are not
automatically loosened.


## Ajuste ARMED -> TRIGGERED (v6.1)

A partir del diagnóstico de producción, la entrada armada tolera hasta `0.15 ATR`
por fuera de la zona original antes de declararse `setup_chased`. La confirmación
1m sigue siendo obligatoria y debe ser una vela cerrada posterior al armado, pero
su cierre puede quedar hasta `0.08 ATR` alrededor del trigger. Esto evita cancelar
setups por unos pocos ticks sin volver al modelo de entrada tardía.

Los `setup_id` cancelados por chase, expiración, invalidación o RR insuficiente,
y también los ya disparados, quedan consumidos durante 3600 s. El mismo evento
estructural no puede rearmarse; una nueva vela estructural genera un `setup_id`
distinto y vuelve a ser elegible.

## ARMED priority monitor + durable setup state (v6.2)

The scanner remains responsible for discovery and keeps the existing 5m/15m/1h quality gates. Once a setup passes those gates and enters `ARMED`, it no longer depends on the scanner rotation to reach `TRIGGERED`.

- `MultiMarketCoordinator.monitor_armed()` watches the union of currently armed symbols independently of the ranked scanner.
- The priority snapshot is intentionally lightweight: executable depth/quote + closed 1m candles only. It does not redownload 5m/15m/1h on every priority poll.
- `TRADE_ARMED_MONITOR_POLL_SECONDS` defaults to `2.0`. Railway does not need the variable unless an override is desired.
- Priority polls do not increment `analyzed`/`armed_pending` on every pass, so the rejection funnel remains a strategy-discovery metric rather than being flooded by high-frequency follow-up checks.
- An ARMED setup is written durably to `armed_setups` before the discovery pass returns. Terminal outcomes are persisted and their setup IDs are written to `armed_consumed_setups` with TTL.
- Worker startup restores valid ARMED setups and consumed setup IDs. A consumed tombstone always wins over a stale ACTIVE row.
- Triggered ARMED entries use an idempotent `signal_claims` key based on `armed_setup_id`, so a worker restart cannot submit the same structural setup twice even when the priority snapshot contains no 5m candles.

This change does not relax MTF bias, HTF exhaustion, ATR, structural stop, minimum RR, TP ratios, chase tolerance, or closed-1m confirmation. It changes *how fast an already-approved setup is followed*, not *what qualifies as a setup*.
