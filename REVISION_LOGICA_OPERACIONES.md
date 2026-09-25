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
