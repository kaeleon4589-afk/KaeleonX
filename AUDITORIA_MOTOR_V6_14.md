# KAELEON v6.14 — Root Bottleneck Fix

## Evidencia post-deploy v6.13

Archivo analizado: `logs.1790693861353.log.txt`.

En el tramo visible (14:43:26–14:57:15 UTC) no hubo `SETUP_ARMED` ni `SETUP_TRIGGERED`. Se observaron 63 eventos `SETUP_WATCHING`, 112 `SETUP_WATCH_PROGRESS` y 70 `SETUP_WATCH_CANCELLED`.

Al agrupar por `lifecycle_id` (eliminando duplicados por usuario), hubo 7 estructuras únicas:

- 4 terminaron en `retest_too_deep`.
- 2 terminaron en `retest_too_extended`.
- 1 terminó por `breakout_retest_window_expired`.

Las penetraciones terminales `retest_too_deep` observadas fueron aproximadamente 0.354, 0.404, 0.467 y 0.619 ATR. En todos esos casos la dirección 5m seguía alineada con el setup y el ADX era aproximadamente 25.9–40.1.

El funnel acumulado incluido en el mismo log alcanzó 530 análisis, 53 WATCHING, 1 ARMED, 0 TRIGGERED y 0 FILLED. El único ARMED acumulado había terminado en `setup_chased`.

## Raíz 1: penetración de wick demasiado rígida

v6.13 usa un máximo fijo de `0.30 ATR` para la penetración del wick del retest. Ese gate se ejecuta aunque la tendencia 5m siga fuertemente alineada. La telemetría muestra un grupo repetido de retests entre ~0.35 y ~0.47 ATR que eran descartados antes de llegar a los filtros de close, stop, RR y micro-confirmación.

### Corrección

- El límite base continúa siendo 0.30 ATR.
- Solo en tendencia fuerte y direccionalmente alineada (ADX >= 20, EMA alignment >= 0.75 y edge direccional >= 0.50) el wick puede penetrar hasta 0.50 ATR.
- La adaptación solo aplica dentro de la ventana estructural de retest existente.
- `RETEST_CLOSE_INVALIDATION_ATR` no cambia.
- `ARM_MIN_RR=1.10` no cambia.
- `ARM_MAX_STOP_ATR=1.80` no cambia.
- HTF exhaustion, anti-chase y confirmación 1m no cambian.
- Penetraciones > 0.50 ATR siguen siendo terminales incluso en tendencia fuerte.

Con los ejemplos del log, 0.354 / 0.404 / 0.467 dejan de morir únicamente por el wick si el resto de condiciones sigue válido. El caso ~0.619 continúa rechazado.

## Raíz 2: respawn del mismo WATCHING cancelado

Un `SetupWatch` terminal se eliminaba de memoria, pero no existía un tombstone consumido equivalente al que ya existe para `ArmedSetup`. Por eso el mismo breakout, con el mismo `watch_id`, podía ser descubierto otra vez y cancelado otra vez.

Ejemplos del log:

- `MARSCOIN:BRW:1790692200000:LONG` reapareció tres veces.
- `REZ:BRW:1790691900000:LONG` reapareció dos veces.

Esto inflaba `watching`, consumía scanner/priority-monitor cycles y no añadía oportunidades nuevas.

### Corrección

- Se añade tombstone de `watch_id` para cualquier watch terminal.
- El mismo `watch_id` no puede volver a entrar en WATCHING hasta que expire el tombstone.
- El tombstone se persiste utilizando el estado terminal ya almacenado en `setup_watches` y se restaura después de un restart.
- Cuando un watch promueve a ARMED también se consume su `watch_id`, evitando que el precursor original vuelva a nacer mientras la estructura armada sigue su ciclo.
- Se añade `watch_cancelled:<reason>` al rejection funnel para que producción muestre directamente qué cancela los WATCHING.

## Archivos modificados

- `app/strategy/armed_entry.py`
- `app/strategy/router.py`
- `app/orchestrator.py`
- `app/trading/runtime.py`
- `tests/test_armed_entry_v6.py`

## Archivo nuevo

- `AUDITORIA_MOTOR_V6_14.md`

## Validación

- Suite completa: `246 passed`.
- `python -m compileall -q app scripts`: OK.
- Pruebas nuevas verifican:
  - contexto fuerte -> techo adaptativo de 0.50 ATR;
  - contexto débil -> se conserva 0.30 ATR;
  - wick ~0.38 ATR puede avanzar;
  - wick ~0.59 ATR sigue cancelándose;
  - un watch terminal consumido no puede reaparecer con el mismo `watch_id`.
