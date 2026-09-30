# KAELEON v6.21 — cotización fresca dentro del recorrido de ejecución

Base: entrega v6.20 de esta conversación. Conserva íntegramente su perfil recovery. No requiere nuevas variables, cambios de capital, cambios de SL/TP ni migraciones.

## Evidencia del log nuevo

Archivo: `logs.1790773684105.log.txt`.

El fragmento contiene 1.224 eventos entre 12:53:57 y 13:07:57 UTC del 30 de septiembre de 2026. Sus contadores acumulados comienzan aproximadamente a las 06:48:51 UTC. El perfil recovery está confirmado por 168 eventos de evaluación de estrategia.

El contador de un usuario registra 1.150 análisis, 191 seguimientos, 13 armados, 3 señales activadas y aprobadas por riesgo, cero órdenes enviadas y cero operaciones ejecutadas. Las tres terminan en `execution_rejected:market_unavailable`.

El caso detallado es WLDPROPW LONG a las 13:02:42 UTC (09:02:42 de Cuba): bid 0.5453, ask 0.546, libro marcado válido, cotización de 23.050 ms al llegar al primer usuario. Los siguientes usuarios reciben ese mismo libro con antigüedades de hasta 23.681 ms. El límite de ejecución es 10.000 ms. Los ocho eventos corresponden a usuarios evaluando la misma oportunidad, no a ocho oportunidades independientes.

La apertura de filtros v6.20 sí permitió avanzar hasta señal y riesgo. En esta ventana acumulada el impedimento final registrado en todas las señales aprobadas es la antigüedad del precio. Los rechazos de otras estructuras siguen existiendo; no se afirma que toda estructura rechazada sea una oportunidad válida.

## Causa en el código

El libro se adquiría al formar el snapshot. Después podía esperar:

1. A que `asyncio.gather` completara los snapshots de todos los símbolos armados.
2. Al lock compartido de procesamiento del runtime.
3. A refrescos de cuentas/estado y al turno de cada usuario.

Al disparar una señal y aprobar riesgo, el orquestador seguía usando la cotización descargada antes de esas esperas. El cambio v6.18 de descargar depth después de las velas evitaba una espera interna del snapshot, pero no estas esperas posteriores. La prueba v6.20 que llegaba directamente al ejecutor DEMO no reproducía este recorrido completo.

## Corrección

- `monitor_armed` entrega cada símbolo en cuanto termina su descarga, sin esperar a que finalice el símbolo más lento. Cancela y recoge tareas pendientes al detenerse.
- `app/main.py` conecta un proveedor real de cotizaciones al runtime usando el mismo cliente público CoinW ya existente. No abre una cuenta ni añade credenciales.
- Cada orquestador solicita un libro actualizado cuando procesa un setup armado, después de las esperas iniciales y antes de consumirlo.
- Si la consulta inicial falla, expira o devuelve datos inválidos, no se dispara ni consume el setup. Se registra `EXECUTION_QUOTE_PENDING`; podrá reintentarse durante su vigencia normal.
- Las consultas persistentes que impiden posiciones u órdenes pendientes duplicadas se realizan antes de la última renovación del libro. Tras renovar se recalculan entrada, slippage esperado, geometría, RR, distancia a SL, persecución del precio, conflicto de libro y tamaño de posición mediante las comprobaciones existentes.
- La renovación usa una copia del snapshot: conserva velas y contexto y no modifica el snapshot compartido por otros usuarios. No cambia la fecha de un precio viejo para aparentar frescura.
- El nuevo proveedor tiene un timeout de 8 segundos y solo acepta libros válidos, del símbolo correcto, con números finitos y antigüedad de recepción entre 0 y 2 segundos.
- Se conserva el límite de 10 segundos del guard de ejecución. Se vuelve a comprobar la edad inmediatamente antes del envío, después de los awaits de claim/reserva, junto con la validez del lease del worker.
- Si la segunda consulta falla después del disparo, la señal recibe rechazo terminal `execution_quote_refresh_failed`; no se envía una orden con el precio viejo. Si una escritura de claim/reserva tarda tanto que caduca el precio, se registra `execution_quote_expired_before_submit`. En LIVE se limpia la reserva de esa orden aún no enviada.
- Los diagnósticos nuevos son visibles con LOG_LEVEL=INFO y están limitados por frecuencia para evitar saturación.

No se amplió la tolerancia de antigüedad ni se eliminaron filtros de precio. Los filtros de estrategia son los mismos de v6.20. El refuerzo de idempotencia de esa versión sigue presente.

## Archivos que debes actualizar si ya desplegaste v6.20

Reemplazar los cinco archivos de producción:

```text
app/main.py
app/orchestrator.py
app/trading/runtime.py
app/market/coordinator.py
app/logging/logger.py
```

Añadir los tres archivos nuevos:

```text
tests/test_execution_quote_refresh_v621.py
FIX_V6_21.md
ARCHIVOS_TOCADOS_V6_21.txt
```

No se elimina ningún archivo. El ZIP contiene el proyecto completo, incluidos los cambios y archivos nuevos de v6.20; si actualizas desde v6.18, utiliza el proyecto completo o aplica también la lista v6.20.

## Despliegue y comprobación

1. Actualiza esos cinco archivos juntos en el repositorio del servicio que ejecuta el motor y despliega/reinicia ese servicio. `app/main.py` y `app/trading/runtime.py` conectan el proveedor: subir solo el orquestador dejaría incompleta la corrección.
2. Conserva `TRADE_PREARM_PROFILE=recovery`, `TRADE_ARMED_ENTRY_ENABLED=true`, `TRADE_LEGACY_ENTRY_FALLBACK_ENABLED=false` y `TRADING_WORKER_ENABLED=true`. No necesitas una variable nueva.
3. No borres posiciones, saldos, claims ni colecciones. No ejecutes un segundo worker.
4. En logs, `EXECUTION_QUOTE_REFRESHED` confirma que el código nuevo consulta un libro dentro del consumidor; muestra la fecha anterior, la nueva y su edad. Está limitado por frecuencia, por lo que no aparece en cada consulta.
5. La confirmación de éxito operativo es `ORDER_SUBMITTED` seguido de `POSITION_OPENED`, y `submitted`/`filled` mayores que cero en `REJECTION_FUNNEL`. Ver un armado o un refresh por sí solo no confirma una operación.
6. Si vuelve a fallar, el motivo distingue consulta fallida, libro inválido, precio que se alejó, o cotización expirada durante persistencia. No debe interpretarse todo como un filtro de estrategia.

## Pruebas ejecutadas

Desde la carpeta del proyecto, con dependencias de requirements.txt instaladas:

```bash
python -m pytest -q
python -m compileall -q app tests
```

Resultado: **285 pruebas aprobadas**, compilación Python correcta. Se mantiene un aviso de deprecación de Starlette/httpx que no impide las pruebas. Frontend y dependencias no se modificaron; no se ejecutó una compilación frontend.

Los 16 casos nuevos incluyen:

- Reproducción del rechazo con cotización de 23.681 ms sin proveedor de renovación.
- La misma situación atraviesa el orquestador y termina en fill DEMO cuando se renueva el libro.
- Dos pruebas completas LONG/SHORT con StrategyRouter/ArmedEntryEngine reales, RiskManager, TradingOrchestrator y PaperExecutionEngine, incluyendo persistencia de prueba y evento POSITION_OPENED. Capital de prueba: 3 USDT a 10x, en cuenta virtual de 100 USDT.
- Error de red, precio viejo, timestamp futuro, símbolo incorrecto, bid/ask cruzados y NaN conservan el setup antes del disparo; al recuperarse el proveedor puede ejecutar.
- El precio actualizado que excede la tolerancia de persecución se rechaza.
- Una consulta fallida después del disparo tiene resultado terminal sin orden.
- Un claim deliberadamente demorado 11 segundos no permite enviar una orden con precio caducado.
- Un símbolo rápido llega al consumidor aunque otro quede esperando; se cancelan las tareas al detener el monitor.
- Los dos eventos nuevos se ven a nivel INFO/WARNING y no se repiten sin límite.

Las velas, el proveedor de cotizaciones y la base de datos de estas pruebas son locales/sintéticos. No se accedió a tu Railway ni a una cuenta autenticada de CoinW, no se desplegó desde aquí y no se ejecutaron órdenes reales. La corrección está verificada contra el fallo reproducido; la validación pendiente es observar el nuevo despliegue con mercado real. No se garantiza un horario o número de operaciones.
