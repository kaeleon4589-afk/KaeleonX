# KAELEON · Fase 1D — Breakout Retest y seguimiento

Fecha: 2026-10-10. Base: `KaeleonX_FASE1C_SWEEP_Y_GRAFICO_2026-10-10.zip`.

## Contexto y alcance

La operación AERO LONG (`PAPER-4ecadf42948a4135`) cerró con SL y PnL neto -8.552026 USDT. El archivo de logs recibido demuestra su cierre, pero **no incluye la secuencia original de apertura**. Por eso no es correcto atribuir con certeza la pérdida de AERO a la ruta rápida de confirmación. Una prueba del motor sí demostró que dicha ruta podía activar un BREAKOUT_RETEST con una vela de 1 minuto contraria.

Esta versión corrige ese comportamiento, sin modificar `BreakoutRetestStrategyV2.scan`, el router, los filtros del scanner, `LIQUIDITY_SWEEP`, el tamaño de posición, el apalancamiento, el cálculo estructural de TP/SL ni la protección existente.

## Qué cambia

1. En `app/strategy/entry_engine_v2.py`, `BREAKOUT_RETEST` ya no se confirma **únicamente** con cotización fresca + Order Book. Exige una vela **cerrada de 1 minuto** que cumpla las condiciones existentes de dirección, cuerpo, posición del cierre y cercanía al trigger.
2. El selector de velas excluye explícitamente cualquier vela en formación y velas cuyo cierre supere los 90 segundos de antigüedad.
3. Sigue activo el control de recuperación del precio ejecutable respecto del trigger (0.12 ATR de tolerancia en la ruta de vela cerrada), RR dinámico, anti-chase, invalidación y protección contra Order Book en conflicto.
4. Si falta confirmación, el setup **permanece ARMED** hasta recuperar condiciones, caducar o invalidarse. No se eleva ninguna puntuación de calidad ni se endurece la búsqueda inicial.
5. `SETUP_ARMED_PROGRESS` registra motivos como `waiting_closed_1m_confirmation`, `micro_confirmation_pending`, `breakout_live_reclaim_pending` y `confirmation_1m_stale`. `SETUP_TRIGGERED` incluye `confirmed_1m_close`, `confirmed_1m_age_ms`, `setup_age_ms` y `confirmation_mode`. Los datos de la vela quedan además en `intent.metadata["micro_confirmation"]` en el momento de la señal.
6. Se añade `tools/audit_breakout_logs.py`, un analizador local para los logs exportados de Railway. No es un servicio de vigilancia automática ni calcula rentabilidad neta.

## Archivos existentes modificados

- `app/strategy/entry_engine_v2.py`
- `tests/test_strategy_engine_v2.py` (actualiza una expectativa antigua de confirmación inmediata)

## Archivos nuevos

- `tests/test_breakout_fase1d_confirmation.py`
- `tests/test_breakout_log_audit.py`
- `tools/audit_breakout_logs.py`
- `KAELEON_FASE1D_BREAKOUT_SEGUIMIENTO.md`

**Archivos de producción eliminados:** ninguno. **Variables de entorno que debes agregar/cambiar/eliminar:** ninguna. Las variables `V2_LIVE_CONFIRM_*` no habilitan la confirmación rápida para Breakout V2 ni Liquidity Sweep V2 en esta fase.

## Pruebas realizadas

- `python -m compileall -q app tests tools`: completado.
- `python -m pytest -q tests`: **331 aprobadas, 2 fallidas**.
- Dos fallas conocidas de compatibilidad legacy ya presentes en Fase 1C: `tests/test_armed_entry_v6.py::test_breakout_adaptive_confirmation_rejects_stop_inside_5m_noise` y `tests/test_entry_geometry_risk.py::test_breakout_final_execution_rr_floor_is_stricter_than_global_floor`.
- `python -m pytest -q tests/test_strategy_engine_v2.py tests/test_breakout_fase1d_confirmation.py tests/test_sweep_confirmed_reversal_rollout.py`: **20 aprobadas**.
- Analizador de logs probado con el log AERO aportado: 3 cierres Breakout con pérdidas brutas; no hay apertura ni señal de esos cierres en esa exportación.
- Pendiente: validación en Railway/DEMO con mercado vivo y comprobación de evolución de rentabilidad. No se modificaron archivos frontend, por lo que no se ejecutó un build frontend en esta fase.

## Despliegue seguro en Railway

1. No subir una versión nueva mientras haya operaciones DEMO abiertas. Realiza copia del ZIP de Fase 1C y de los saldos DEMO antes del despliegue. Comprueba la compatibilidad con las posiciones y órdenes existentes.
2. Actualiza a Fase 1D, manteniendo las variables de entorno actuales. No cambies TP/SL, apalancamiento ni filtros en esta misma prueba.
3. Revisa que el worker arranque, no aparezcan `PIPELINE_ERROR`/`WORKER_CRASHED` y se sigan generando `SETUP_ARMED` para ambas estrategias.
4. Durante las primeras 10–20 operaciones **cerradas de Breakout Retest**, exporta logs y ejecuta:

   ```bash
   python tools/audit_breakout_logs.py logs.XXXXXXXX.log.txt
   ```

   Si dispones de varios archivos, pásalos juntos. El informe separa confirmaciones, entradas, cierres, rechazos y el PnL **bruto** encontrado en los eventos; el PnL **neto** se debe validar desde Telegram/estado de la cuenta, incluyendo comisiones y deslizamiento.
5. Compara con la Fase 1C: frecuencia de `SETUP_ARMED`, cantidad de entradas `POSITION_OPENED`, razones por las que el setup queda en espera, operaciones por día, beneficio/pérdida neta promedio y máximas excursiones MFE/MAE si se dispone de esas series. No evalúes solo el win rate.
6. Si la frecuencia cae sensiblemente, **primero identifica** el principal motivo de espera (`micro_confirmation_pending`, `confirmation_1m_stale`, `breakout_live_reclaim_pending`, etc.). No relajes todo el motor a la vez.

## Límites

- La solución reduce entradas prematuras; **no garantiza que una entrada confirmada vaya a resultar ganadora**.
- El monitor de Railway puede estar limitado temporalmente y los logs exportados pueden omitir eventos anteriores; el script no recupera historial perdido ni sustituye el historial de operaciones de MongoDB.
- Esta fase **no** modifica el capital por operación; con 10x, una pérdida puede seguir afectando significativamente al capital y merece una auditoría de exposición por separado.

### Diferencias exactas del ZIP respecto a Fase 1C

El paquete Fase 1D omite únicamente dos archivos **generados** de bytecode que estaban en el ZIP anterior: `app/storage/__pycache__/__init__.cpython-313.pyc` y `app/storage/__pycache__/database.cpython-313.pyc`. No son código fuente ni contienen cambios del usuario; Python los regenera cuando corresponde. Ningún archivo de código existente fue eliminado.

Si ejecutas `python -m pytest -q` desde la raíz sin indicar `tests/`, pytest detecta dos pares de módulos con el mismo nombre entre raíz y carpeta de tests (error de colección preexistente). Utiliza `python -m pytest -q tests` para la suite de esta versión.
