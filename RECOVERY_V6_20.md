# KAELEON v6.20 — apertura controlada de detección previa al armado

Base exacta: ZIP `KaeleonX-v6.18-fresh-execution-quote.zip` entregado en esta conversación. No incorpora otra entrega v6.19. Fecha: 2026-09-30.

## Diagnóstico verificado

El archivo `logs.1790745402374.log.txt` tiene 1.066 eventos entre 05:04:16 y 05:15:52 UTC. Sus contadores acumulados parten aproximadamente de las 21:54 UTC del día anterior: unas 7 horas y 20 minutos.

El último contador de un usuario registra 1.831 análisis, 1.722 rechazos de estrategia, 55 seguimientos, 54 seguimientos cancelados, un armado cancelado, cero señales activadas y cero órdenes enviadas o ejecutadas. No se suman los contadores de distintos usuarios como si fueran oportunidades independientes.

Principales motivos acumulados: 1.205 `sweep_level_not_reached`, 320 `regime_sweep_not_allowed`, 136 `liquidity_sweep_no_directional_trend`, 38 `atr_out_of_range`; además, 24 cancelaciones `retest_too_deep`. Los dos primeros representan aproximadamente el 88,6 % de los 1.722 rechazos de estrategia de ese contador.

Ese porcentaje describe el motivo final registrado, no demuestra que todos esos casos hubieran sido operaciones válidas. El router priorizaba el motivo de sweep y ocultaba en el contador el fallo previo de breakout. En los 331 eventos STRATEGY_EVALUATED del fragmento aparecen también 136 `breakout_not_detected`, 33 conflictos H1/15m, 24 rechazos ATR de breakout y 16 rechazos por agotamiento de temporalidades superiores.

La causa comprobable de la sequía está en la detección y promoción de estructuras, antes del envío de órdenes: extremos de liquidez de 34 velas, exclusión de RANGE y varios filtros acumulativos. No hay evidencia aquí de errores de envío al exchange. No es posible reconstruir un backtest de rentabilidad a partir de este log: no contiene las series OHLCV completas.

## Qué cambia

El perfil `recovery` queda activo por defecto. `TRADE_PREARM_PROFILE=strict` recupera los umbrales anteriores de detección. Un valor desconocido usa `strict`. Los cambios aplican al motor armado, tanto al descubrimiento completo como al seguimiento y promoción; el evaluador legacy conserva su comportamiento.

| Regla | strict | recovery |
|---|---|---|
| Estructura breakout | 20 velas 5m | 12 velas 5m |
| Nivel de liquidez sweep | Extremo de 34 velas 5m | Extremo de 12 velas 5m |
| Proximidad para iniciar seguimiento de sweep | 0,45 ATR | 0,80 ATR |
| Volumen relativo mínimo del breakout | 0,95 | 0,70 |
| Cuerpo mínimo del breakout / rango de vela | 0,30 | 0,22 |
| Posición mínima de cierre LONG / máxima SHORT | 0,64 / 0,36 | 0,58 / 0,42 |
| Extensión máxima de la vela de ruptura | 0,60 ATR | 0,85 ATR |
| Conflicto H1/15m | Una temporalidad contraria veta | Vetan si ambas son contrarias |
| Régimen RANGE | No evalúa entradas | Evalúa breakout/sweep si hay dirección clara |
| Mecha del retest | 0,30 ATR; 0,50 solo con contexto fuerte | Hasta 0,50 ATR con alineación direccional y retest vigente, sin exigir además ADX 20 |

La extensión de la vela de ruptura NO es permiso para perseguir el precio al ejecutar: se mantienen la zona de entrada, RR mínimo, invalidación por cierre, SL, límites de antigüedad, confirmación de continuación del sweep, confirmación de entrada y controles de cotización/spread. Se mantienen los filtros ATR y de agotamiento superior. RANGE no implica operar en dirección neutral ni invertir la dirección del motor.

Adicionalmente:

- El router muestra `prearm_profile` y `rejection_reasons` de ambas ramas. El motivo principal prioriza breakout cuando fue evaluado; por ello sus contadores futuros no son directamente comparables con los antiguos que priorizaban sweep.
- Un `hard_block` impide descubrir setups en cualquier régimen; UNKNOWN sigue bloqueado.
- `trigger()` verifica los setups consumidos antes de volver a emitir una intención. Antes guardaba el consumo al activar, pero no lo verificaba si recibía otra llamada con el mismo setup. La protección de consumo y de hard_block permanece también en strict.

## Despliegue

1. Sube el contenido del proyecto al mismo repositorio que despliegas. Incluye los archivos nuevos de `app/strategy`; no subas el ZIP como sustituto del código fuente.
2. En el servicio que ejecuta el motor, establece explícitamente:

```dotenv
TRADE_PREARM_PROFILE=recovery
TRADE_ARMED_ENTRY_ENABLED=true
TRADE_LEGACY_ENTRY_FALLBACK_ENABLED=false
TRADING_WORKER_ENABLED=true
```

3. Conserva las variables existentes de conexión, credenciales, capital y apalancamiento. Esta entrega no cambia esas asignaciones ni necesita migración de base de datos. No inicies un segundo worker para aplicar el cambio.
4. Despliega/reinicia ese servicio para cargar el nuevo código. El comando de arranque del backend permanece en `railway.toml`.
5. Comprueba `prearm_profile: recovery` dentro de STRATEGY_EVALUATED. Después busca el avance `SETUP_WATCHING`, `SETUP_ARMED`, `SETUP_TRIGGERED` y los contadores `submitted`/`filled` de REJECTION_FUNNEL. Un seguimiento no es una operación. El contador de ejecución es el que confirma el resultado.

El proyecto también usa recovery si omites la nueva variable. Dejarla explícita permite saber qué perfil está seleccionado. No borres saldos, operaciones ni colecciones para aplicar esta entrega. Los seguimientos persistidos mantienen sus niveles hasta resolver o expirar.

Para cerrar de nuevo los filtros: cambia únicamente `TRADE_PREARM_PROFILE=strict` y reinicia. No hay relajación automática adicional por tiempo sin operar.

## Instalación y pruebas locales

Desde la carpeta del proyecto, con Python 3.12:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q
TRADE_PREARM_PROFILE=strict python -m pytest -q
python -m compileall -q app tests
```

En Windows, activa el entorno con `.venv\Scripts\activate`; configura la variable del perfil con la sintaxis correspondiente a tu terminal.

## Validación y límites

- Base original antes de editar: 255 pruebas aprobadas.
- Entrega: 269 pruebas aprobadas con recovery por defecto; 269 con strict como entorno general (los nuevos casos fijan explícitamente el perfil bajo prueba).
- Compilación Python de app y tests correcta.
- Dos casos nuevos recorren la estrategia real, RiskManager y PaperExecutionEngine, con capital configurable de 3 USDT y 10x, para LONG y SHORT. Usan velas y cotizaciones sintéticas, no órdenes enviadas a CoinW.
- Cubiertos: estructura local frente a un extremo lejano, RANGE direccional, veto cuando H1 y 15m se oponen juntas, promoción coherente, dirección neutral/conflictiva, hard_block, perfil inválido, tolerancia de mechas y rechazo de una segunda activación del setup consumido.
- Se mantienen las pruebas existentes de ejecución, cotización, entradas tardías, persistencia y saldos.
- La suite emite un aviso de deprecación de Starlette/httpx; no es un fallo de prueba. No se modificaron dependencias para ocultarlo.
- Frontend sin modificaciones; no se recompiló. No se han realizado despliegues, conexiones autenticadas al exchange ni operaciones reales desde este entorno.

El perfil acepta estructuras menos selectivas y puede aumentar falsas señales y pérdidas. No garantiza una frecuencia de operaciones ni rentabilidad. La comprobación pendiente es observar el avance del nuevo embudo en tu servicio desplegado. Si aparece un nuevo bloqueo, `rejection_reasons` permite localizarlo sin atribuirlo automáticamente a sweep.
