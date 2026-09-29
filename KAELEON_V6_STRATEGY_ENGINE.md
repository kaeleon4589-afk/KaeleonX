# KAELEON Strategy Engine v6

Base de implementación: `KaeleonX-breakout-retest-v5-exhaustion-stop.zip`.

## Objetivo

La v6 corrige el conflicto de la v5 entre "entrar temprano" y exigir confirmaciones/stop que obligaban a esperar demasiado. El cambio principal es separar la detección del setup de la ejecución:

`5m precursor -> SETUP_WATCHING -> SETUP_ARMED -> 1m confirmación -> precio ejecutable -> orden`

Ya no se necesita esperar una vela 5m desarrollada para crear la orden.

## Flujo v6

1. El régimen sigue aportando contexto, pero no bloquea por duplicado todos los setups.
2. `BREAKOUT_RETEST` puede armarse después de breakout + retest válido, antes de una confirmación 5m tardía.
3. `LIQUIDITY_SWEEP` solo puede progresar con dirección de tendencia clara; `RANGE` es shadow-only y no genera órdenes.
4. El setup guarda trigger, invalidación, zona máxima de entrada, SL, TP, calidad y expiración.
5. La entrada necesita una vela 1m cerrada con dirección, cuerpo, posición de cierre y volumen relativo compatibles.
6. Si el precio invalida la estructura, excede la zona de entrada o expira el setup, se cancela sin ordenar.
7. El orquestador vuelve a validar geometría, RR, spread/orderbook, riesgo operativo y disponibilidad antes de enviar la orden.

## Cambios de protección

- `TRADE_ENTRY_MIN_STOP_ATR` baja de `0.90` a `0.30` en producción. Es un filtro final de ruido, no un segundo sistema de stop.
- La v6 usa el stop estructural propio del setup y no exige el antiguo piso de `0.95 ATR` del modelo v5 para poder armar una entrada.
- El anti-chase continúa activo, pero la v6 además tiene `entry_zone_low` / `entry_zone_high`; si el precio se escapa de esa zona el setup se cancela.
- `UNKNOWN` continúa bloqueado y `RANGE` es shadow-only: no origina órdenes de Liquidity Sweep.
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
  - bloqueo de sweep en `RANGE`
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
1m sigue siendo obligatoria. En v6.1 se exigía que esa vela hubiera cerrado después
del armado; v6.3 añade una excepción estricta para una vela cerrada inmediatamente
antes del armado. En ambos casos su cierre puede quedar hasta `0.08 ATR` alrededor
del trigger. Esto evita cancelar
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


## Fast closed-1m confirmation (v6.3)

Production logs showed that the priority monitor was following ARMED setups quickly,
but several valid structures still lost their entry while waiting for an entirely new
1m candle. v6.3 fixes that timing gap without lowering the structural filters.

A setup may reuse the most recent CLOSED 1m candle only when all of these are true:

- the candle closed no more than `30s` before the setup armed and is still no more than `30s` old at execution check;
- setup quality is at least `78`;
- candle body ratio is at least `0.45`;
- relative volume is at least `0.90`;
- directional close position is strong (`>= 0.72` LONG, `<= 0.28` SHORT);
- the close remains inside the existing trigger/chase band;
- the live executable quote has actually crossed the trigger;
- the order book is valid and contains both bids and asks;
- the existing anti-chase boundary still passes;
- final executable RR still passes the unchanged `1.10R` minimum.

If any fast-confirm condition fails, KAELEON does not reject the setup merely for
that failure: it returns to `waiting_fresh_1m_confirmation` and waits for the normal
post-arm closed 1m path. The normal 1m thresholds are unchanged.

The anti-chase edge also now has a tiny book-derived tolerance for one-tick/spread
rounding. It is inferred from visible depth and hard-capped at `0.02 ATR`; material
chasing still cancels normally. This tolerance is separate from the existing
`0.15 ATR` chase buffer.

Runtime controls (defaults are already in code, so Railway does not require them):

- `TRADE_ARMED_FAST_CONFIRM_ENABLED=true`
- `TRADE_ARMED_FAST_CONFIRM_MAX_AGE_SECONDS=30`

This release does **not** relax MTF bias, HTF exhaustion, ATR range, setup quality
discovery, structural stop, TP caps, consumed setup protection, or executable RR.

## v6.4 — Liquidity Sweep alineado con tendencia

Regla obligatoria de ejecución:

- Tendencia BULLISH / trend_bias=long -> LIQUIDITY_SWEEP solo puede armar LONG.
- Tendencia BEARISH / trend_bias=short -> LIQUIDITY_SWEEP solo puede armar SHORT.
- Tendencia NEUTRAL/UNKNOWN -> LIQUIDITY_SWEEP no puede armarse.
- Si `regime.direction` y `features.trend_bias` entran en conflicto -> no se arma el sweep.
- `RANGE` vuelve a ser shadow-only y no es fuente de órdenes Liquidity Sweep.

En `TREND_CONTINUATION`, `BREAKOUT_RETEST` tiene prioridad. Solo cuando no existe un breakout/retest armable puede probarse un Liquidity Sweep secundario, y siempre en la misma dirección de la tendencia. La ventana de frescura de breakout/retest del motor ARMED se amplía moderadamente de 3 a 4 velas de 5m, manteniendo MTF, HTF exhaustion, estructura de retest, RR, SL y confirmación 1m.

Para evitar que un deploy permita ejecutar setups antiguos creados antes de esta regla, todo nuevo setup Liquidity Sweep guarda `trend_aligned=true`, `trend_direction_at_arm` y `trend_alignment_guard_version=1`. Un setup persistido sin ese sello se cancela antes del trigger.


## v6.5 — Trend-continuation opportunity balance

- LIQUIDITY_SWEEP remains hard-aligned with the detected trend: BULLISH -> LONG only, BEARISH -> SHORT only, neutral/conflict -> blocked.
- In TREND_CONTINUATION, a valid BREAKOUT_RETEST keeps first priority.
- If no fresh BREAKOUT_RETEST is armable, the engine now evaluates the aligned LIQUIDITY_SWEEP directly; it no longer requires an extra `VOLATILE_SWEEP >= 2` score before even checking the sweep structure.
- This does not loosen sweep quality: real liquidity sweep/reclaim geometry, ATR, structural stop, target/RR and 1m execution confirmation remain mandatory.
- The ARMED breakout/retest discovery window is widened from 4 to 5 closed 5m bars. Retest penetration, last-close proximity, extension, MTF alignment, HTF exhaustion, structural stop and minimum RR remain unchanged.


## v6.6 — WATCHING de precursores y BREAKOUT_RETEST de primera clase

El diagnóstico de producción mostró el fallo arquitectónico principal: el scanner
solo podía crear `ARMED` cuando encontraba la figura completa en una visita aislada.
Con rotación de símbolos, un breakout podía ocurrir en una visita y completar el
retest antes de que el scanner regresara; lo mismo ocurría con un precio que se
acercaba a una piscina de liquidez y barría/reclamaba entre dos visitas.

La v6.6 introduce un estado anterior a ARMED:

`scanner -> precursor -> SETUP_WATCHING -> estructura completa -> SETUP_ARMED -> 1m -> TRIGGERED`

### BREAKOUT_RETEST

- Un breakout estructural válido crea `SETUP_WATCHING` inmediatamente, aunque el
  retest todavía no exista.
- El símbolo deja de depender de la rotación normal y entra en un monitor estructural
  propio.
- El monitor sigue 5m/15m/1h cerrados hasta que el retest cumple touch, penetración,
  cierres, distancia, stop estructural, target y RR.
- Si completa esas reglas, pasa a `SETUP_ARMED`; si envejece fuera de la ventana,
  cambia la estructura o aparece conflicto/agotamiento, el watch termina sin orden.
- La autoridad de dirección es única: `regime_direction + trend_bias`. 1H/15M ya no
  necesitan producir un bias no-neutro para que la estrategia exista, pero un bias
  explícitamente opuesto sí bloquea con `mtf_bias_conflict_with_trend`.

### LIQUIDITY_SWEEP

- En tendencia alcista solo se crea WATCHING/ARMED LONG; en tendencia bajista solo SHORT.
- Cuando el precio queda a <= `0.45 ATR` de la piscina de liquidez relevante, el símbolo
  entra en WATCHING antes de que el barrido haya terminado.
- El monitor exige después el mismo sweep/reclaim real, wick, volumen, ATR, stop y RR
  del motor ARMED. WATCHING no constituye una señal ni una orden.
- `RANGE`, tendencia neutral y conflicto direccional continúan bloqueados.

### Monitor y persistencia

- `MultiMarketCoordinator.monitor_watching()` sigue los símbolos WATCHING fuera del scanner.
- Cadencia por defecto: `TRADE_SETUP_WATCH_POLL_SECONDS=15`.
- TTL por defecto: `TRADE_SETUP_WATCH_TTL_SECONDS=1800`.
- WATCHING se persiste en MongoDB (`setup_watches`) con TTL y se restaura después de restart.
- `SETUP_WATCHING`, `SETUP_WATCH_PENDING`, `SETUP_WATCH_CANCELLED` y el `SETUP_ARMED`
  promovido desde watch dejan trazabilidad explícita.
- ARMED conserva su monitor ligero de ~2 s con quote/orderbook + 1m cerrado.

Este cambio no baja RR, no elimina HTF exhaustion, no permite Liquidity Sweep contra
la tendencia y no convierte un precursor en operación. Corrige el problema temporal:
seguir una estructura prometedora mientras se forma en vez de exigir encontrarla ya
terminada en una sola visita del scanner.

Validación de software de esta versión: `228 passed` y `python -m compileall -q app scripts` OK.

## v6.7 — Lifecycle observability end-to-end

Production now exposes one stable `lifecycle_id` for the entire setup journey. The
identifier is created when the precursor enters WATCHING and is propagated unchanged
through ARMED, trigger confirmation, risk, order submission and the opened position.
A complete successful journey is therefore grep-able as:

`SETUP_WATCHING -> SETUP_WATCH_PROGRESS -> SETUP_ARMED -> SETUP_ARMED_PROGRESS -> SETUP_TRIGGERED -> SIGNAL_ACCEPTED -> RISK_APPROVED -> ORDER_SUBMITTED -> POSITION_OPENED`

Key rules:

- `SETUP_WATCHING`, watch progress/cancellation, ARMED progress and restore events are
  now visible at INFO in production logs instead of silently falling below the default
  Railway log level.
- Pending progress is emitted when the reason changes and at most once per 60 seconds
  when the same reason persists, avoiding a 15s/2s log flood.
- Every lifecycle milestone contains `lifecycle_id`, `watch_id`, `setup_id` when known,
  strategy, direction and a numeric `stage_order`.
- A setup promoted from WATCHING copies the precursor lifecycle metadata into the
  `ArmedSetup`; the triggered `TradeIntent` already inherits that metadata, so the same
  identifier reaches risk and execution without creating a second journey.
- Directly-complete setups that skip WATCHING receive `lifecycle_id=setup_id`, so they
  are still traceable from ARMED onward.
- `RISK_APPROVED` and `ORDER_SUBMITTED` are explicit INFO milestones. All post-trigger
  signal/risk/execution rejections include the same lifecycle identifiers and mark the
  journey terminal with the exact reason.
- `POSITION_OPENED` includes `lifecycle_complete=true` and the position stores
  `lifecycle_id`, `watch_id` and `setup_id` for later correlation.
- Restored WATCHING/ARMED state keeps or reconstructs the lifecycle ID across Railway
  restarts.

MongoDB also keeps two diagnostic collections:

- `setup_lifecycles`: compact latest-state summary per user/mode/lifecycle.
- `setup_lifecycle_events`: append-style milestone ledger keyed by `event_id`; this is
  the authoritative historical path because asynchronous writes cannot erase an older
  or newer milestone.

This release is observability-only for the strategy itself: it does not change trend
alignment, BREAKOUT_RETEST rules, Liquidity Sweep rules, ATR, HTF exhaustion, RR,
SL/TP geometry, 1m confirmation, chase protection or risk sizing.

## v6.8 — Adaptive post-arm 1m confirmation (evidence-driven)

The lifecycle data from production showed that discovery was no longer the terminal
bottleneck: setups reached `WATCHING` and `ARMED`, but none reached `TRIGGERED`.
The representative NEAR short armed at quality `73.21` and preserved execution RR
above `1.10R` while a closed 1m candle printed a strong bearish shape (body `0.50`,
close position `0.20`) with moderate relative volume (`0.5229`). The legacy normal
1m rule rejected it only because RVOL was below `0.70`; subsequent candles delayed
confirmation until the price had already left the valid RR window.

v6.8 keeps the original normal 1m rule unchanged and adds one narrow fallback only
for post-arm candles:

- setup quality must be at least `72`;
- the live order book must be valid and populated;
- the live executable quote must already have crossed the trigger and remain inside
  the existing anti-chase boundary;
- the closed 1m candle must be strongly directional: body >= `0.45`, close position
  >= `0.72` for LONG or <= `0.28` for SHORT;
- RVOL must still be >= `0.50`;
- final execution geometry and `ARM_MIN_RR >= 1.10` are still enforced after the
  confirmation, exactly as before.

The fast pre-arm path remains unchanged and strict. The ATR chase protection,
invalidation, target RR caps, structural stop, trend alignment and all discovery
filters are unchanged. Adaptive confirmations are logged as
`confirmation_mode=postarm_closed_1m_strong_shape` and the resulting intent includes
`micro_confirmation_1m_adaptive` so the behavior is fully auditable.

## v6.9 — Stateful WATCHING + live trend revalidation

Production lifecycle logs after v6.8 showed that the remaining bottleneck had moved
back to `WATCHING -> ARMED`: precursors were being created and monitored, but the
watch monitor was re-running the generic discovery functions instead of advancing
the exact structure that originally created each lifecycle. Breakout watches therefore
repeated `no_fresh_breakout_retest_arm` until `breakout_retest_window_expired`.
The same logs also exposed a stale Liquidity Sweep watch whose saved LONG direction
continued to be monitored after the current 5m trend had turned SHORT.

v6.9 changes WATCHING into a genuinely stateful lifecycle:

- BREAKOUT_RETEST stores and follows the original `breakout_candle_ts` and
  `structural_level`. `advance_watch()` no longer rediscovers a new breakout.
- Only candles after that original breakout are evaluated for the retest. Progress is
  explicit: `waiting_retest_touch`, `retest_pullback_insufficient`,
  `retest_close_invalidated`, `retest_too_deep`, `retest_too_extended`,
  `retest_stop_too_tight`, `retest_stop_too_wide`, `retest_rr_too_low`,
  `retest_quality_below_arm_threshold`, or `retest_complete`.
- LIQUIDITY_SWEEP keeps the original watched `liquidity_level` and follows that exact
  pool through sweep/reclaim instead of recomputing a different rolling pool on each
  monitor pass. Its progress now distinguishes waiting for the sweep, wick/volume
  insufficiency, reclaim, RR/stop/quality failures and failed reclaim.
- Every WATCHING poll revalidates the current closed-5m trend. A neutral trend ends a
  directional watch with `watch_direction_lost`; an opposite trend ends it with
  `watch_direction_flipped`. The direction stored when the watch was created is no
  longer treated as current market truth.
- Every new ARMED setup carries `trend_revalidation_required=true`. Before 1m
  confirmation can produce `TRIGGERED`, the current closed-5m trend must still agree
  with the setup direction. Missing 5m context waits with `trend_context_unavailable`;
  neutral/opposite context cancels with `trend_direction_lost` or
  `trend_direction_flipped`.
- The high-frequency ARMED market monitor still fetches quote/orderbook and closed 1m
  on every poll, but now also supplies 5m trend context from a per-symbol 15-second
  cache. This adds the required pre-trigger safety without fetching 5m candles every
  ~2 seconds.

This release does not lower RR, widen anti-chase, remove HTF exhaustion, loosen the
breakout/retest geometry, or allow Liquidity Sweep against trend. It fixes lifecycle
state handling and directional staleness.

Validation: `236 passed` and `python -m compileall -q app scripts` OK.

## v6.10 — Adaptive strong-trend retest pullback floor

Production telemetry after v6.9 confirmed that stateful watch progression and live
trend revalidation were working, but the dominant `WATCHING -> ARMED` bottleneck
moved to the fixed breakout-retest pullback floor. Several otherwise aligned watches
repeated `retest_pullback_insufficient` around `0.089-0.101 ATR` against a hard
`0.12 ATR` minimum, then later either penetrated too deeply or became too extended.

v6.10 keeps `0.12 ATR` as the standard requirement and introduces one narrow adaptive
path. A fresh retest may use a `0.085 ATR` minimum only when all of these conditions
are simultaneously true:

- current closed-5m trend context is available and still agrees with the watch;
- the retest is within the first 3 closed 5m bars after the breakout;
- current 5m ADX is at least `20`;
- EMA stack alignment is at least `0.75`;
- directional EMA alignment edge is at least `+0.50` for LONG or at most `-0.50`
  for SHORT.

If any of those conditions is missing, the original `0.12 ATR` minimum remains in
force. This change does not modify maximum retest penetration, close invalidation,
maximum close distance, HTF exhaustion, MTF bias conflict, structural stop geometry,
minimum RR, arm quality, anti-chase, or post-arm 1m confirmation.

Lifecycle telemetry now records `retest_pullback_mode`, `required_pullback_atr`, the
actual `retest_pullback_atr`, and whether the adaptive rule was eligible. This makes
future production validation explicit instead of inferring the threshold from a
terminal rejection.

Validation: `238 passed` and `python3 -m compileall -q app scripts` OK.
