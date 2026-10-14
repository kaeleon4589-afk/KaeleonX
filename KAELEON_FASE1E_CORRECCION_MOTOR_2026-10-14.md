# KAELEON 1E — Corrección del núcleo de entrada BREAKOUT_RETEST

Fecha: 2026-10-14.
Base: `KAELEON_FASE1E_CAPITAL_RR_SOPORTES_2026-10-14.zip`.
**Solo Fase 1E.** La Fase 1F no fue modificada.

## Problema demostrado en STRK

Posición DEMO `PAPER-87eb73bd1f9d4401`, 2026-10-14 UTC:
- 09:30:35: ARMED a SHORT, disparador ~0.07873 y stop ~0.079358.
- 09:30:56: cotización ~0.07841, `setup_extended_wait_reentry`.
- 09:32:06: RR temporalmente insuficiente.
- 09:34:58: disparo con precio ejecutable ~0.07866 y vela bajista de 1m cerrada en 0.07847, de ~58 s de antigüedad.
- 09:35:01: operación abierta a ~0.078634.
- 09:47:49: cierre en SL a ~0.079376.

El motor V2 exigía una vela de 1m bajista y que el precio siguiera por debajo del disparador de 5m, pero podía aceptar cotizaciones que **ya hubieran rebotado significativamente en contra del cierre bajista de 1m**. También aceptaba setups ARMED sin comprobar una reversión multitemporal importante porque la ruta de monitoreo prioritario omite deliberadamente el análisis completo y stateful del régimen.

El log no incluye el histórico completo de velas ni la zona precisa de soporte para validar retrospectivamente si esa operación se encontraba en un soporte confirmado. Un precio que rebotó no demuestra por sí solo un soporte estructural.

## Cambios efectivos en el motor

- `app/strategy/entry_engine_v2.py`: comprobación **en el disparo** de que la cotización LIVE sigue respaldando la dirección de la última vela cerrada de 1m para BREAKOUT_RETEST. Si la cotización se aleja más de 0.20 ATR en contra del cierre confirmado, queda ARMED esperando nueva confirmación (`breakout_live_confirmation_diverged`), en vez de ejecutar con confirmación desfasada.
- El mismo motor revalida de forma limitada el contexto direccional de 5m, 15m y 1h **sin alterar la máquina de estados del régimen**. Solo cancela el setup (`breakout_regime_reversed`) cuando **al menos dos marcos** muestran un giro contrario con confianza >= 0.80; lecturas neutras o falta de velas no se consideran giros probados.
- Ambos ajustes se aplican únicamente a BREAKOUT_RETEST. LIQUIDITY_SWEEP, gestión de capital, comisiones, TP/SL, API, frontend, persistencia y bases de datos permanecen tal como estaban en la entrega anterior.
- El porcentaje 0.20 ATR es el valor por defecto. Se puede ajustar de forma opcional con `V2_BREAKOUT_CONFIRM_MAX_ADVERSE_ATR` (rango 0.10 a 0.40), pero **no hay nuevas variables obligatorias** y no se recomienda cambiarlo durante las primeras pruebas.

## Pruebas

- Pruebas nuevas: reproducción de los precios publicados de STRK, inversión LONG simétrica, reversión clara en dos timeframes y no interferencia con Liquidity Sweep.
- Una prueba existente de V2 se adaptó para que la cotización de continuidad respete también la vela de 1m (el test antiguo esperaba que se abriera con una discrepancia de ~0.42 ATR).
- Suite completa: **354 pasadas, 2 fallidas**. Son dos fallas heredadas de la versión previa, no nuevas: `test_breakout_adaptive_confirmation_rejects_stop_inside_5m_noise` y `test_breakout_final_execution_rr_floor_is_stricter_than_global_floor`.
- El test de STRK valida la debilidad de precio/confirmación. **No** prueba la rentabilidad futura de la modificación ni reconstruye toda la sesión de mercado.

## Despliegue

- Mantener la ejecución de LIVE deshabilitada; esta entrega está validada con tests, no con fondos reales.
- Pausar nuevas entradas de 1E durante el despliegue; verificar protección de posiciones abiertas; hacer copia del ZIP desplegado.
- Desplegar el ZIP completo como **reemplazo** de la entrega anterior, NO superponer diferentes Fases.
- No reiniciar estadísticas, MongoDB ni API; no borrar datos.
- Observar `SETUP_ARMED_PROGRESS` con `breakout_live_confirmation_diverged`, `SETUP_CANCELLED` con `breakout_regime_reversed`, y tasas de entradas frente a antes.
- Comparar por setup/símbolo, no por usuario: una señal compartida puede producir muchas operaciones independientes.

## Límite de la conclusión

Este es un arreglo de dos fallos reales del disparador V2, no una certificación de calidad estratégica ni una promesa de eliminar rachas negativas. Falta revisar con datos históricos completos la geometría de BREAKOUT_RETEST, el retest y los filtros de Liquidity Sweep antes de concluir que el motor es consistentemente rentable.
