# Implementación y despliegue — 2026-09-25

## Cambios
- Corrección del libro de CoinW: lectura de campos p/m, validación y ordenación de precios/cantidades. El formato anterior podía dejar el libro vacío y bloquear las entradas.
- Velas cerradas, continuidad y frescura de datos; caché y reintentos de consultas públicas. Los filtros de estrategia siguen vigentes: no se fuerza una operación cuando falta información.
- DEMO usa datos del mercado real con ejecución simulada. El saldo inicial más los resultados netos realizados (ganancias, pérdidas y comisiones registradas) determina el saldo actual. Los cierres se contabilizan sin duplicarlos y sobreviven a reinicios. El PnL abierto no se suma al saldo realizado.
- Apalancamiento configurado en 10x tanto en DEMO como en LIVE; validación de margen, tamaño y precios ejecutables.
- Supervisión independiente de posiciones abiertas y órdenes pendientes, incluso fuera de la lista de símbolos del escáner. Recuperación de estado, protección contra escrituras antiguas y exclusión entre workers.
- LIVE conserva órdenes con resultado incierto para conciliarlas, y espera la liquidación confirmada del exchange antes de publicar el resultado final. Conversión de contratos a cantidad base.
- Cola persistente de notificaciones de Telegram con reintentos. La interfaz muestra saldo demo actual, rechazos, frescura del mercado y liquidaciones pendientes.

## Antes de desplegar
1. Cambiar FIXED_LEVERAGE a 10 en Railway y en cualquier servicio API/worker. El valor antiguo 5 ya no es válido. Si se omite, el valor predeterminado es 10.
2. Conservar la base de datos y las credenciales existentes. El saldo DEMO usa un capital inicial persistente y el historial registrado; no puede reconstruir operaciones nunca guardadas. PAPER_INITIAL_EQUITY define la semilla para cuentas que todavía no la tienen.
3. Instalar dependencias Python desde requirements.txt. En frontend ejecutar npm ci y npm run build.
4. Mantener una única instancia activa del motor: integrado en API mediante TRADING_WORKER_ENABLED=true, o worker separado ejecutando python -m app.main y API con TRADING_WORKER_ENABLED=false. La exclusión mediante base de datos añade protección contra instancias concurrentes.
5. Verificar después del despliegue la llegada de datos, el motivo de los rechazos, un ciclo completo de operación DEMO, su saldo y la entrega de Telegram. No rebajar filtros para obligar al bot a operar.

## Validación y límites
- Backend: 110 pruebas aprobadas; un aviso de deprecación de Starlette/httpx.
- Frontend: compilación TypeScript/Vite correcta.
- Consulta pública CoinW: libro válido y series de velas completas con el parser corregido.
- No se enviaron órdenes reales ni mensajes de Telegram. Falta la validación integrada con MongoDB, Railway, Telegram y la cuenta privada de CoinW del despliegue.
- DEMO simula precios ejecutables, comisiones y deslizamiento configurado; no reproduce íntegramente el matching, la financiación o las liquidaciones del exchange.
- Telegram utiliza reintentos: puede repetirse un mensaje si el envío se acepta pero falla la confirmación local.
- Una orden LIVE incierta puede permanecer pendiente. Verificar su estado en CoinW antes de cualquier intervención; no borrar pendientes ni reenviar a ciegas.
- El alcance de esta entrega es el circuito de trading y su visibilidad; no certifica otros módulos de facturación o autenticación.

Ver ARCHIVOS_TOCADOS.txt para la lista exacta comparada con el ZIP original.

## Reinicio global de estadísticas DEMO/LIVE
En Administración → Estadísticas seleccionar DEMO o LIVE, escribir el nombre de la etapa y confirmar. Solo el administrador configurado por ADMIN_PHONE ve el botón y el servidor exige su sesión válida.

Cada reinicio inicia una etapa independiente y persiste su fecha, administrador, nombre y resumen previo. Se muestran los últimos diez reinicios; todos los registros se conservan en statistics_periods. Se cuentan únicamente operaciones abiertas a partir del inicio de la etapa, y el resultado solo se incorpora una vez cerradas y liquidadas. Las posiciones antiguas que cierren después quedan excluidas de la etapa nueva. Las operaciones sin fecha de apertura quedan excluidas después de un reinicio.

El reinicio de DEMO devuelve cada cuenta virtual a su saldo inicial persistido (PAPER_INITIAL_EQUITY se aplica solo a cuentas nuevas), y reinicia sus métricas, PnL y operaciones recientes visibles. LIVE reinicia esas métricas, pero el saldo disponible proviene de CoinW y no se mueve dinero ni se llama a endpoints de retiro. El historial anterior sigue guardado para auditoría. DEMO y LIVE son independientes. Se exige cerrar las posiciones y resolver órdenes pendientes del modo seleccionado antes de reiniciar; si no, la API devuelve 409. El reinicio no cambia los parámetros del setup ni los límites de riesgo.

API autenticada como administrador:
- GET /admin/trading/statistics?mode=demo (o live).
- POST /admin/trading/statistics/reset con mode, label (1–100 caracteres), confirm=true y request_id UUID. Reintentar el mismo request_id no crea otra etapa; reutilizarlo con otro contenido devuelve 409.

Se crean automáticamente dos índices de statistics_periods al iniciar el backend con MongoDB. No se necesita borrar datos ni migrar saldos. Los reinicios antiguos sin global_reset mantienen su comportamiento original; el primer reinicio con esta versión activa el nuevo alcance. El usuario MongoDB debe poder crear índices, como ya exige la aplicación.

Validación de esta ampliación: 129 pruebas backend aprobadas, incluida autorización, validación de modo, reinicio de saldo DEMO, independencia LIVE, exclusión de operaciones antiguas y reintentos idempotentes. Frontend TypeScript/Vite compilado. No se ha validado visualmente en navegador ni contra MongoDB o CoinW de producción.

Para probar localmente: python -m pip install -r requirements.txt; python -m pytest -q. En frontend: npm ci y npm run build. Tras desplegar backend y frontend, abrir Administración → Estadísticas y verificar una nueva etapa DEMO antes de usar el reinicio LIVE.

## Precisión de precios y RR ejecutable — 2026-09-25
La vista de operaciones abiertas usa precisión dinámica para precios de mercado; por ejemplo, la operación BB muestra entrada 0.010456, SL 0.010372 y TP 0.010526, en lugar de 0.01 en todos los campos. Los saldos y el PnL conservan dos decimales. Si no existe un precio actual registrado muestra «—». Cuando la estrategia no configura toma parcial, solo aparece «TP», sin TP1 ficticio.

Además se vuelve a validar el ratio recompensa/riesgo después del precio ejecutable (bid/ask y deslizamiento DEMO). Tanto DEMO como LIVE rechazan una nueva entrada si su RR ejecutable cae por debajo de 0.95, el mínimo que ya aplicaban las estrategias durante la generación de señal. La operación BB previa mostró RR 0.8268 y revela que faltaba esta segunda validación. Una posición ya abierta no se cierra ni se altera por el cambio. Si el RR baja, se registra SIGNAL_REJECTED con motivo execution_rr_too_low y valores de entrada, SL, TP y RR.

Validación: 115 pruebas backend y compilación TypeScript/Vite. La visualización final debe verificarse tras desplegar el frontend actualizado; esta entrega no cambia el teléfono ni las preferencias de notificación de Telegram.

## Margen configurado completo y SL/TP estructurales — 2026-09-25

Cada usuario abre como máximo una operación a la vez. Antes de enviar una orden se comprueban también posiciones abiertas y órdenes pendientes persistidas para ese usuario, incluso de otro símbolo. Tras cerrarla y liquidarla, el motor puede estudiar una nueva señal. Los usuarios no comparten posiciones ni capital.

`operating_capital` es el margen destinado a la siguiente operación. Con 50 USDT configurados y 10x, el tamaño nominal es 500 USDT si el saldo disponible paga además la comisión de entrada; si esos 50 USDT constituyen todo el saldo disponible, se reserva la comisión y el nominal será algo menor. El apalancamiento no altera el porcentaje de movimiento del precio. El SL puede perder varios USDT o más, según la distancia estructural y la ejecución, porque el antiguo límite de 1% de riesgo ya no reduce el tamaño. Los valores heredados `RISK_PER_TRADE` y `MAX_MARGIN_FRACTION` no tienen efecto; retíralos del despliegue. `FIXED_LEVERAGE` sigue siendo 10 en esta versión.

`BREAKOUT_RETEST`: SL detrás del extremo del retroceso de 5 minutos con colchón ATR; TP en la siguiente resistencia/soporte reciente de 5 o 15 minutos, o proyección de la amplitud del último rango si ya superó esos niveles. `LIQUIDITY_SWEEP`: SL más allá del extremo barrido con colchón ATR; TP en el extremo de liquidez opuesto anterior al barrido. No se recortan a un porcentaje fijo. Se mantienen las validaciones de dirección, calidad de datos, geometría y RR para evitar órdenes incoherentes; una señal sin recorrido suficiente puede rechazarse.

El panel muestra exposición y margen estimado de la posición abierta. La comisión y el deslizamiento DEMO siguen descontándose del PnL. El historial y las posiciones ya abiertas conservan sus niveles y tamaño originales; los nuevos cálculos aplican a nuevas entradas tras desplegar. Validación local: 132 pruebas backend y compilación TypeScript/Vite. Faltan pruebas integradas con CoinW, MongoDB y una sesión de mercado real antes de activar LIVE.

## Imagen de marca — 2026-09-25

La imagen KAELEON suministrada se sirve desde `frontend/public/images/kaeleon-trading-art.jpg` (el archivo de origen era JPEG aunque su nombre terminaba en `.png`). Se muestra completa en la pantalla de acceso de escritorio y móvil, y en la tarjeta lateral del dashboard. El acceso mantiene el formulario separado y legible; no se recorta el logotipo de la imagen. Comprobado con `npm run build` y 132 pruebas backend. Revisar visualmente después de desplegar en teléfono y escritorio.

## Indicador de PnL de posición abierta — 2026-09-25

La línea bajo el PnL actual en «Operaciones Activas» pasa a verde si el PnL es positivo, rojo si es negativo y gris neutro si es cero o todavía no hay dato. El texto del PnL usa el mismo criterio. No cambia el tamaño de la posición, la salida ni el cálculo financiero. Comprobado con `npm run build` y 132 pruebas backend; falta la comprobación visual en el despliegue móvil.

## ROE en vivo — 2026-09-25

En «Operaciones Activas» se muestra ROE en vivo = PnL no realizado / margen inicial estimado × 100. El margen inicial estimado se calcula como cantidad base × precio de entrada / apalancamiento. El ROE usa el mismo color del PnL y muestra «—» si faltan los datos necesarios. En LIVE puede diferir del ROE que publique CoinW si el exchange aplica margen ajustado, comisiones u otra base de cálculo. No altera órdenes ni la contabilidad. Compilación TypeScript/Vite y 132 pruebas backend aprobadas; pendiente verificación visual tras despliegue.

## Gráfico nativo CoinW — 2026-09-25

Se añadió un gráfico financiero propio de KAELEON debajo de «Operaciones Activas». Usa KLineChart 10.0.3, histórico de velas desde el backend y datos públicos en vivo de CoinW Futures. Incluye 1m/3m/5m/15m/30m/1H/4H/1D, velas o área, crosshair, zoom/pan, pantalla completa, Last Price, Mark Price, VOL/MACD/RSI, MA/EMA/BOLL y fallback REST si el WebSocket se interrumpe.

El buscador consulta dinámicamente el catálogo de instrumentos `online` de CoinW Futures; no existe una whitelist local, por lo que un contrato nuevo queda disponible sin cambiar código cuando CoinW lo publica en ese endpoint. El backend valida que el símbolo exista antes de servir velas. Los contratos USDT y USDC se normalizan respetando el formato requerido por CoinW.

Cuando hay una operación activa, ENTRY, SL y TP/TP1/TP2 aparecen como líneas y etiquetas bloqueadas dentro del gráfico. Al abrir una nueva operación, el gráfico vuelve automáticamente al par operado. El usuario puede buscar otro mercado y después regresar con «Ver operación». El gráfico no modifica los niveles ni la ejecución: solo visualiza los valores persistidos por el motor.

Seguridad: el histórico y el catálogo pasan por endpoints autenticados `/market/instruments` y `/market/candles`. El socket del navegador se conecta únicamente a canales públicos `candles_swap_utc` y `mark_price`; no expone API Key ni API Secret. Para una escala alta de usuarios concurrentes queda recomendado migrar ese socket público a un hub backend compartido.

Validación local de esta ampliación: `python -m compileall -q app` y `pytest -q`: **137 pruebas aprobadas**. La compilación frontend final requiere descargar la nueva dependencia `klinecharts@10.0.3`; el entorno de esta revisión no tuvo acceso operativo al registro npm, por lo que el `npm install/npm run build` de esta ampliación queda pendiente de ejecutar en un entorno con red (por ejemplo Railway o local). Ejecutar `cd frontend && npm install && npm run build` antes de promover a producción.

## Microestructura y herramientas avanzadas del gráfico — 2026-09-25

Se amplió el gráfico nativo CoinW con un panel de microestructura sincronizado con el mercado seleccionado. El frontend se suscribe además a `index_price`, `funding_rate`, `depth`, `fills` y `ticker_swap`. Se añadieron Order Book, spread, presión bid/ask derivada, Recent Trades, Depth Chart, Last/Mark/Index, datos 24h, funding y cuenta regresiva cuando el WebSocket entrega el próximo timestamp `nt`. La presión de compra/venta no se presenta como cantidad de personas: se calcula solo con la liquidez visible del libro.

El backend incorpora `GET /market/snapshot`, que valida el símbolo contra el catálogo online y obtiene en paralelo depth, trades, ticker y último funding REST para arrancar el panel sin esperar a la primera actualización del WebSocket. Los fallos parciales del snapshot no tumban el gráfico: cada bloque vuelve vacío y el stream público puede completarlo después.

La operación activa incorpora zona visual de beneficio/riesgo, historial de entradas y cierres sobre velas, y break-even. Si el motor entrega `break_even_price` se usa ese valor; si no, solo se muestra `BE EST.` cuando existen cantidad y `entry_fee`, usando la tasa efectiva de entrada como estimación del fee de salida. No se añadió liquidación local para evitar mostrar un valor no validado contra CoinW.

También se añadieron herramientas de dibujo de KLineChart (tendencia, segmento, horizontal, línea/canal de precio, paralelas, Fibonacci, anotación y pincel), edición de parámetros MA/EMA/BOLL/VOL/MACD/RSI, perfil de volumen aproximado de las velas y alertas de precio locales por símbolo. Las alertas se guardan en `localStorage` y pueden emitir una notificación del navegador si el usuario concede permiso mientras la página está activa; no existe todavía monitorización push en segundo plano ni sincronización entre dispositivos.

Validación de esta ampliación: `python -m pytest -q` -> **138 pruebas aprobadas**. La instalación npm del entorno de revisión quedó bloqueada esperando al registro, por lo que la compilación TypeScript/Vite de esta ampliación debe repetirse con acceso npm antes de producción: `cd frontend && npm ci && npm run build`. La comprobación visual final debe hacerse después del deploy, especialmente en móvil, fullscreen y con un par que tenga una operación activa.

## Gestión adaptativa TP / break-even / profit lock — 2026-09-25

Las nuevas operaciones ya no colocan el TP exactamente sobre la resistencia/soporte estructural. El objetivo ejecutable queda por defecto al 92% del recorrido hacia ese nivel. El objetivo estructural se conserva para auditoría y el RR se vuelve a validar después del ajuste.

Cuando una posición alcanza el 55% del recorrido al TP, el motor mueve el SL a un break-even que contempla fees y colchón de ejecución. Al 80% del recorrido, activa `PROFIT_LOCK` y bloquea por defecto el 35% de la distancia al TP. El stop solo se mueve a favor de la operación.

En LIVE, cada cambio del stop se sincroniza con el TPSL de CoinW. Los precios se normalizan a la precisión del instrumento y las actualizaciones fallidas quedan pendientes para reintento. La conciliación con CoinW nunca puede aflojar un stop ya gestionado. En DEMO se aplica la misma lógica internamente.

Variables opcionales de Railway (los valores indicados ya son los defaults):
- `TRADE_TARGET_FRONT_RUN_RATIO=0.92`
- `TRADE_BREAK_EVEN_ACTIVATION_RATIO=0.55`
- `TRADE_PROFIT_LOCK_ACTIVATION_RATIO=0.80`
- `TRADE_PROFIT_LOCK_CAPTURE_RATIO=0.35`
- `TRADE_EXIT_FEE_RATE_ESTIMATE=0.0006`
- `TRADE_BREAK_EVEN_BUFFER_BPS=3.0`

La implementación aplica a operaciones nuevas. Una posición antigua conserva su TP existente, pero puede adoptar la protección dinámica al ser restaurada. Antes de activar LIVE, verificar una operación DEMO completa y revisar que `management_stage` pase `INITIAL -> BREAK_EVEN -> PROFIT_LOCK` cuando corresponda.

Validación backend: **143 pruebas aprobadas** y `compileall` sin errores. No se modificó frontend en esta ampliación.
