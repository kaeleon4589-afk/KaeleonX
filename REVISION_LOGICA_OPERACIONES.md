# Revisión de operaciones DEMO/LIVE — 2026-09-25

## Evidencia y diagnóstico
La revisión se basó en el código de la entrega anterior y en las capturas y mensajes facilitados por el usuario. No hubo acceso a los logs, a los ticks históricos de la cuenta en Railway/MongoDB ni a sus órdenes privadas CoinW. Las tres pérdidas visibles prueban un problema de geometría de entrada en los ejemplos ONDO/XPL y RR deteriorado en BB; por sí solas no prueban una tasa de pérdida del 100% en todo el historial.

| Operación | Entrada | SL | TP | Hallazgo |
| --- | ---: | ---: | ---: | --- |
| ONDO LONG | 0.556311 | 0.555000 | 0.563446 | SL a 0.236% de la entrada; RR mostrado 5.4418. Entrada realizada después de una caída respecto de la vela que definió la señal. |
| XPL LONG | 0.119674 | 0.119273 | 0.121099 | SL a 0.335% de la entrada. El cierre observado a 0.118336 muestra el riesgo de un salto entre cotizaciones. |
| BB LONG | 0.010456 | 0.010372 | 0.010526 | RR ejecutable 0.8268; ya lo bloquea la validación de RR de la revisión anterior. |

### Causa raíz
La estrategia calculaba el SL y TP a partir del cierre de una vela 5m. La orden se ejecutaba con bid/ask posterior. Al sustituir el precio de entrada sin volver a validar la distancia al stop original, una cotización favorable para el precio LONG podía acercar el stop hasta menos de la mitad de su distancia prevista y mostrar un RR ilusoriamente alto. En el sentido opuesto, una entrada perseguida podía alejar demasiado el stop.

Adicionalmente, ambas estrategias limitaban la distancia al SL con un máximo fijo aunque la volatilidad ATR o el nivel estructural requiriese un stop mayor. Esa limitación podía poner el SL dentro del movimiento que justificaba el setup. El tamaño de la posición usaba la pérdida bruta teórica en el SL, sin incluir comisiones de entrada/salida ni deslizamiento previsto. En el monitor, cada cotización de una posición abierta recorría a todos los usuarios, aun cuando solo su propietario necesitaba procesarla.

### Correcciones
1. Se mantiene el SL/TP del setup, pero se verifica antes de mandar una orden que la distancia desde la entrada ejecutable al SL esté entre el 80% y el 125% de la distancia prevista en la vela. Si el precio se aleja, se espera un setup nuevo. Esto afecta por igual DEMO y LIVE, LONG y SHORT. Los casos ONDO y XPL aportados quedarían rechazados por un SL demasiado próximo.
2. El RR se sigue evaluando sobre bid/ask ejecutable con su límite mínimo existente de 0.95. BB habría sido rechazada por esa comprobación, ya incorporada en la revisión anterior.
3. BREAKOUT_RETEST y LIQUIDITY_SWEEP rechazan un setup cuando su stop requerido por estructura/ATR supera el máximo configurado, en vez de recortarlo y simular una protección que la estrategia no pedía.
4. El cálculo de tamaño incorpora una reserva de dos comisiones taker y deslizamiento de salida previsto, usando las variables ya configuradas PAPER_TAKER_FEE y PAPER_SLIPPAGE_BPS. En LIVE son una hipótesis de riesgo, no una afirmación sobre las comisiones efectivas de CoinW. Los huecos de mercado aún pueden superar la pérdida prevista.
5. El monitor procesa las cotizaciones de seguimiento solo para usuarios con una posición abierta u orden pendiente en ese par. La hora de cierre DEMO proviene de la cotización recibida y se registra el retraso de procesamiento, el bid/ask que disparó la salida y la diferencia de ejecución respecto al SL. Los mensajes nuevos de cierre por SL incluyen esa diferencia.
6. El panel muestra razones de rechazo comprensibles y mantiene los precios exactos ya corregidos. No se alteran las posiciones existentes.

## Qué se verificó
- 127 pruebas automatizadas aprobadas, incluida reproducción de entradas deterioradas como ONDO/XPL, coste de riesgo LONG/SHORT, SL mayor que el modelo, cierre desde bid/ask, propietario del monitor y operaciones válidas que todavía abren.
- Frontend TypeScript y Vite compilaron.
- No se colocaron órdenes reales y no se hizo un backtest histórico de la rentabilidad. La estrategia aún puede perder operaciones. La simulación DEMO usa mercado real, pero el stop sigue sujeto al precio observado y a los intervalos del monitor; LIVE utiliza la ejecución y liquidación de CoinW.

## Despliegue y evaluación
- Mantener FIXED_LEVERAGE=10 en todos los servicios y las variables de coste/riesgo actuales. No borrar base de datos ni reiniciar el saldo de una posición abierta.
- Desplegar backend y frontend juntos. Verificar una operación DEMO completa en un mercado de precio bajo y los motivos de descarte en el panel.
- En Administración → Estadísticas, iniciar una nueva etapa DEMO **después** del despliegue para comparar setups con operaciones nuevas. Evaluar win rate, PnL neto y diferencia entre SL y salida después de suficientes cierres. No prometer rentabilidad por las pruebas unitarias.
- Si aparece un cierre anormal, consultar POSITION_CLOSED del ID y sus campos exit_trigger_price, stop_gap_bps, quote_delay_ms, además de la secuencia de precios de Railway/CoinW. Estas mediciones distinguen un salto real de una demora del worker.

Se adjunta ARCHIVOS_TOCADOS.txt con la comparación exacta contra la entrega previa.

## Gestión adaptativa de salidas — 2026-09-25

A partir de las operaciones que avanzaban casi hasta TP y después devolvían todo el recorrido hasta el SL, se cambió la gestión de salida sin alterar la lógica de entrada ni el SL estructural inicial.

### Cambios aplicados
1. **TP delante del nivel estructural**: el TP ejecutable se coloca por defecto al 92% de la distancia entre la entrada y el objetivo estructural. El nivel estructural original se conserva en `structural_target_price` para diagnóstico. La variable `TRADE_TARGET_FRONT_RUN_RATIO` permite ajustar el porcentaje entre 0.80 y 1.00. Una señal sigue siendo rechazada si, después de acercar el TP, su RR ejecutable queda por debajo del mínimo 0.95.
2. **Break-even real y automático**: cuando el mejor precio observado recorre el 55% de la distancia hacia el TP ejecutable, el stop se mueve a un break-even que incorpora comisión de entrada, comisión estimada de salida y un pequeño colchón de ejecución. El movimiento solo puede endurecer el stop; nunca lo afloja.
3. **Profit lock**: al alcanzar el 80% del recorrido hacia TP, el stop avanza para bloquear por defecto el 35% de la distancia total al objetivo. Si el mercado revierte desde esa zona, la operación ya no puede volver al SL estructural original.
4. **DEMO y LIVE**: DEMO aplica el stop dinámico dentro del `PositionManager`. LIVE actualiza el TPSL nativo en CoinW. Si CoinW rechaza o demora una actualización, la posición queda con `protection_update_pending=true` y el motor la reintenta; la conciliación nunca sustituye un stop gestionado por otro menos protector.
5. **Precisión CoinW**: los precios de SL/TP enviados al exchange se normalizan a `pricePrecision`. Para no degradar la protección, un SL LONG se redondea hacia arriba y un SL SHORT hacia abajo; el TP se redondea hacia el lado más cercano a la entrada.
6. **Persistencia**: se guardan `initial_stop_price`, `structural_target_price`, `break_even_price`, `profit_lock_price`, `best_price`, `management_stage` y los ratios de gestión. Tras un reinicio, el motor puede continuar la protección sin volver al SL original.

### Valores predeterminados
- `TRADE_TARGET_FRONT_RUN_RATIO=0.92`
- `TRADE_BREAK_EVEN_ACTIVATION_RATIO=0.55`
- `TRADE_PROFIT_LOCK_ACTIVATION_RATIO=0.80`
- `TRADE_PROFIT_LOCK_CAPTURE_RATIO=0.35`
- `TRADE_EXIT_FEE_RATE_ESTIMATE=0.0006`
- `TRADE_BREAK_EVEN_BUFFER_BPS=3.0`

Las variables son ajustables, pero no conviene cambiarlas con una muestra pequeña. El cambio corrige el patrón de devolución total del beneficio flotante; no garantiza rentabilidad ni convierte una señal débil en una operación ganadora.

### Validación
`python -m compileall -q app` y `pytest -q`: **143 pruebas aprobadas**. Se añadieron pruebas LONG/SHORT de break-even y profit lock, sincronización de protección LIVE y protección contra una conciliación de CoinW que intente devolver el stop a un nivel menos protector. No se enviaron órdenes reales durante esta validación.

## Neutralidad direccional LONG/SHORT — 2026-09-25

Se corrigieron dos sesgos previos que podían reducir artificialmente la aparición de operaciones SHORT aunque las estrategias sí soportaran ambos sentidos.

### Scanner de mercados
El ranking previo daba una contribución mayor a `change_24h` positivo que a un movimiento negativo de igual magnitud. El ranking ahora usa `abs(change_24h)` exclusivamente como magnitud de momentum. Con volumen y Open Interest equivalentes, `+4%` y `-4%` reciben exactamente la misma puntuación. La dirección del mercado ya no interviene en la selección previa; queda reservada al detector de régimen y a las estrategias.

El evento `MARKET_SCAN_DONE` incorpora `eligible_breadth` y `shortlist_breadth` (`up`, `down`, `flat`) y `ranking_mode=direction_neutral_momentum`. Esto permite distinguir entre un mercado realmente dominado por subidas y un problema de filtrado sin imponer cuotas artificiales de LONG/SHORT.

### Régimen EMA
`ema_stack_alignment` se calcula ahora con dos puntuaciones espejo: `ema_bullish_alignment` y `ema_bearish_alignment`. Las cinco condiciones tienen exactamente el mismo peso en ambos sentidos: precio respecto a EMA20, EMA20/EMA50, EMA50/EMA200, pendiente EMA20 y pendiente EMA50. El alignment efectivo es el máximo de ambos lados y `ema_alignment_edge` conserva la diferencia firmada.

El `trend_bias` puede identificar un sesgo LONG o SHORT durante una estructura parcial suficientemente clara, sin exigir que las cinco condiciones ya formen un stack perfecto. Esto elimina la asimetría anterior, en la que los puntos parciales solo se concedían al lado alcista.

### Estrategias y observabilidad
No se fuerza una distribución 50/50 de operaciones. Si el mercado es realmente alcista, pueden seguir predominando los LONG; si es bajista, los SHORT deben tener la misma oportunidad técnica. `BreakoutRetestStrategy` y `LiquiditySweepStrategy` mantienen sus condiciones simétricas existentes.

Los eventos `REGIME_EVALUATED` y `STRATEGY_EVALUATED` incluyen ahora dirección de régimen y dirección seleccionada, además de los alignments bullish/bearish. Esto permite localizar en producción si un SHORT fue descartado en scanner, régimen, setup, riesgo o ejecución.

### Validación
- `python -m compileall -q app`: sin errores.
- `pytest -q`: **149 pruebas aprobadas**.
- Se añadieron pruebas que comprueban igualdad de score para movimientos `+X%/-X%`, simetría del alignment EMA, clasificación de tendencia bajista con `short` bias y generación real de intents `LONG` y `SHORT` sobre setups MTF espejo.
- No se añadieron cuotas de dirección ni se obligó al motor a abrir SHORT cuando no existe un setup válido.

## Protección de calidad de entrada y cooldown post-loss — 2026-09-25

Se corrigió el patrón observado de operaciones que abrían y avanzaban casi de inmediato hacia SL, incluyendo reentradas rápidas tras una pérdida. El objetivo de esta revisión es reducir falsas rupturas, entradas perseguidas y stops situados dentro del ruido normal del mercado. No se amplía artificialmente el SL ni se fuerza una operación: si la geometría deja de ser buena, el setup se descarta.

### BreakoutRetest endurecido
- La ruptura 5m ya no se confirma por una mecha. LONG exige cierre real por encima del máximo previo más un buffer de 0.03 ATR; SHORT exige cierre real por debajo del mínimo previo menos el mismo buffer.
- La vela de confirmación debe tener cuerpo mínimo del 28% de su rango y cerrar en el 38% superior/inferior correspondiente (`close_pos >= 0.62` para LONG, `<= 0.38` para SHORT).
- ADX mínimo sube a 16 (1H), 14 (15m) y 12 (5m).
- Extensión máxima respecto a EMA20 baja de 0.95 ATR a 0.70 ATR.
- El score mínimo sube a 78 y deja de partir automáticamente en 69; ahora se construye desde 55 + calidad real.
- RR mínimo de estrategia y ejecución sube de 0.95 a 1.05.

### LiquiditySweep endurecido
- La antigüedad máxima del sweep baja de 8 a 5 velas.
- Profundidad mínima del sweep sube a 0.15 ATR, wick mínimo a 0.32 y RVOL mínimo del sweep a 0.70.
- Trigger: RVOL mínimo 0.65, cuerpo mínimo 0.24 y cierre fuerte (`>= 0.62` LONG / `<= 0.38` SHORT).
- Extensión máxima desde la liquidez baja de 2.25 ATR a 1.10 ATR.
- Tolerancias de recuperación EMA e invalidación del retest se vuelven más estrictas.
- Score mínimo sube a 78 y RR mínimo a 1.05.

### Guardas justo antes de ejecutar
La señal se calcula sobre una vela 5m cerrada, pero la orden usa bid/ask posterior. Antes de mandar una orden se vuelve a validar:
1. `entry_chased_after_signal`: rechaza si el precio ejecutable continuó más de 0.20 ATR en la dirección de la señal después del cierre.
2. `entry_confirmation_lost_before_fill`: rechaza si el mercado retrocedió más de 0.15 ATR contra la confirmación antes de ejecutar.
3. `stop_inside_market_noise`: rechaza si ENTRY→SL es menor que el máximo entre 0.55 ATR y 3 spreads actuales. El SL no se ensancha para salvar la señal.
4. `orderbook_conflict`: con al menos 5 niveles válidos por lado, rechaza únicamente un conflicto severo del top-20 del libro (imbalance contrario >= 35%). Esto es un veto final, no una señal de entrada por sí solo.

### Cooldown después de una pérdida
- Pérdida cerrada: 15 minutos sin nuevas entradas globales para ese runtime/usuario/modo.
- Mismo símbolo: 30 minutos sin reentrada.
- Si la posición cerró mediante profit lock o break-even con resultado neto positivo, no se considera pérdida y no inicia cooldown.
- El cooldown se registra en cierres DEMO locales y cierres LIVE reconciliados, y se reconstruye desde las posiciones persistidas tras reiniciar el worker. Esto evita `SL -> nueva operación inmediata -> SL` incluso después de un restart.

### Variables de entorno
- `TRADE_POST_LOSS_GLOBAL_COOLDOWN_SECONDS=900`
- `TRADE_POST_LOSS_SYMBOL_COOLDOWN_SECONDS=1800`
- `TRADE_ENTRY_MAX_CHASE_ATR=0.20`
- `TRADE_ENTRY_MAX_ADVERSE_REVERSAL_ATR=0.15`
- `TRADE_ENTRY_MIN_STOP_ATR=0.55`
- `TRADE_ENTRY_MIN_STOP_SPREADS=3.0`
- `TRADE_ENTRY_ORDERBOOK_CONFLICT_THRESHOLD=0.35`

### Validación
- `python -m compileall -q app`: sin errores.
- `pytest -q`: **157 pruebas aprobadas**.
- Nuevas pruebas cubren: falsa ruptura solo por mecha, umbrales endurecidos, anti-chase, invalidación antes del fill, stop dentro del ruido, conflicto severo del Order Book, cooldown global/símbolo y reconstrucción del cooldown después de reiniciar.
- No se enviaron órdenes reales durante esta validación y no se afirma rentabilidad por pruebas unitarias.

## 2026-09-25 — BREAKOUT_RETEST v4: ruptura estructural + retest fresco + confirmación

Se sustituyó la confirmación basada en superar la vela inmediatamente anterior por una secuencia estructural obligatoria de tres fases:

1. Ruptura real por cierre sobre/bajo un nivel de las últimas 20 velas.
2. Retest de 1 a 3 velas que permanezca cerca del nivel roto y no lo invalide.
3. Confirmación inmediata en la vela siguiente al retest; no se aceptan varias velas de continuación antes de entrar.

Protecciones añadidas:
- ruptura máxima de 0.60 ATR sobre el nivel estructural;
- retest con penetración máxima de 0.30 ATR y cierres a no más de 0.28 ATR del nivel;
- entrada máxima a 0.75 ATR del nivel estructural;
- rechazo si ya se consumió más del 45% del recorrido estructural hacia el objetivo;
- SL anclado al extremo real del retest, manteniendo el filtro posterior contra stops dentro del ruido de mercado;
- telemetría de nivel estructural, edad del breakout, barras de retest, extensión y porcentaje de impulso consumido.

El objetivo es impedir entradas tardías en la segunda/tercera vela de un rebote ya desarrollado, como el caso CC observado en DEMO.

## Corrección BREAKOUT_RETEST v5 — Exhaustion guard + stop floor

Motivo: operación ENA LONG observada cerca del máximo de 24h después de un movimiento vertical. La versión v4 validaba un breakout/retest local de 5m, pero no rechazaba un mercado ya exhausto en 1h/15m y permitía stops estructurales demasiado cercanos al ruido de 5m.

Cambios:
- Veto simétrico de agotamiento HTF usando las últimas 24 velas de 1h.
- Rechazo de LONG cerca del extremo superior tras avance fuerte; espejo para SHORT.
- Confirmación adicional de extensión contra EMA20 en 1h y 15m.
- Retest debe tener profundidad mínima real, no una pausa microscópica.
- Stop estructural de BREAKOUT_RETEST debe quedar al menos a 0.95 ATR(5m) de la entrada; si no, se descarta el setup en vez de ensancharlo artificialmente.
- Guardia genérica de ejecución sube de 0.55 ATR a 0.90 ATR.
- Telemetría: 24h move, posición en rango 24h, extensión EMA20 1h/15m, profundidad del retest y stop en ATR.
- Identificador del modelo: structural_breakout_retest_fresh_confirmation_v5.
