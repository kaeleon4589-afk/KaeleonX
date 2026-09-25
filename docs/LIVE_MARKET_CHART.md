# Gráfico nativo de mercado CoinW

## Alcance

El dashboard incorpora un gráfico financiero propio de KAELEON debajo de las operaciones activas. El renderizado usa KLineChart 10.0.3; no se incrusta TradingView ni otro iframe. El catálogo y los datos de mercado provienen de las APIs públicas de CoinW Futures.

El buscador no usa una lista fija: consulta `/v1/perpum/instruments`, conserva únicamente instrumentos `online` y permite localizar cualquier contrato perpetuo disponible en el catálogo de CoinW, incluidos los mercados USDT y USDC que exponga ese endpoint.

## Flujo de datos

- Histórico: backend autenticado `GET /market/candles` -> CoinW `/v1/perpumPublic/klines`.
- Catálogo: backend autenticado `GET /market/instruments` -> CoinW `/v1/perpum/instruments`.
- Snapshot de arranque/reconexión: backend autenticado `GET /market/snapshot` -> order book, trades, ticker y último funding REST disponible.
- Tiempo real: WebSocket público de CoinW `wss://ws.futurescw.com/perpum`.
- Canales en vivo: `candles_swap_utc`, `mark_price`, `index_price`, `funding_rate`, `depth`, `fills` y `ticker_swap`.
- Fallback de velas: si el WebSocket se corta, el frontend continúa actualizando la última vela mediante REST cada 5 segundos y reintenta el socket con backoff.

El WebSocket del navegador usa exclusivamente canales públicos; no se envían API Key, API Secret ni credenciales privadas de CoinW desde el frontend.

## Funciones del gráfico

- Temporalidades: 1m, 3m, 5m, 15m, 30m, 1H, 4H y 1D.
- Velas japonesas y vista de área.
- Zoom, desplazamiento, crosshair y retorno al precio LIVE.
- Last Price, Mark Price e Index Price.
- Funding Rate en vivo; cuando CoinW entrega `nt`, se muestra cuenta regresiva hasta el siguiente funding. El REST se identifica únicamente como referencia del último settlement.
- Estadísticas 24h: cambio, máximo, mínimo, volumen y volumen cotizado cuando CoinW los entrega.
- Indicadores principales MA, EMA y BOLL.
- Panel inferior VOL, MACD o RSI.
- Parámetros configurables de indicadores desde la interfaz.
- Herramientas de dibujo: tendencia, segmento, horizontal, línea de precio, canal de precio, paralelas, Fibonacci, anotación y pincel; también se pueden borrar los dibujos del usuario.
- Pantalla completa y diseño responsive para móvil/escritorio.
- Búsqueda dinámica de cualquier contrato CoinW Futures online.
- Al abrir una operación, el gráfico sigue automáticamente el par principal; si el usuario explora otro mercado puede regresar con `Ver operación`.

## Niveles y contexto de operación

Para la operación activa se dibujan como overlays bloqueados los niveles disponibles:

- ENTRY.
- SL.
- TP / TP1 / TP2.
- Break-even si el backend entrega un precio de break-even explícito.
- `BE EST.` únicamente cuando puede estimarse a partir de `entry_fee`, cantidad y precio de entrada. La estimación asume la misma tasa efectiva de fee en la salida, por lo que no se presenta como un valor exacto.

El área entre ENTRY y TP se sombrea como zona de beneficio, y el área entre ENTRY y SL como zona de riesgo. Estos overlays no son editables y no cambian ninguna orden.

Las operaciones cerradas del mismo símbolo se muestran sobre las velas con marcador de entrada LONG/SHORT y marcador de cierre con PnL cuando existe la información temporal y de precio necesaria.

## Microestructura de mercado

Debajo del gráfico se añadió un panel sincronizado con el par seleccionado:

- **Order Book:** asks, bids, cantidades, total por nivel y spread.
- **Book Pressure:** relación entre volumen bid/ask de los niveles visibles. Es una métrica derivada de liquidez; no representa un conteo de compradores o vendedores.
- **Trades:** ejecuciones públicas recientes con precio, cantidad, hora y dirección LONG/SHORT informada por CoinW.
- **Depth:** gráfico de profundidad acumulada basado en el libro recibido.
- **Market Data:** Last, Mark, Index, cambio 24h, high/low, volúmenes, funding, spread, leverage máximo y datos de la operación activa.
- **Volume Profile aproximado:** distribuye el volumen de las últimas velas por su precio típico. Está rotulado como aproximación y no se presenta como perfil tick-by-tick.

El snapshot REST permite que Order Book, trades y market data tengan contenido inicial aunque todavía no haya llegado la primera actualización del WebSocket.

## Alertas de precio

El usuario puede crear hasta ocho alertas por símbolo desde el gráfico. Se determina automáticamente si la alerta debe dispararse al cruzar el precio hacia arriba o hacia abajo y se marca visualmente cuando se alcanza.

En esta entrega las alertas son **locales al navegador** mediante `localStorage`. Al crear una alerta se puede solicitar permiso del navegador y, si está concedido, se muestra una notificación del sistema cuando el precio la cruza mientras la página está activa. No existe todavía seguimiento en segundo plano mediante service worker ni sincronización entre dispositivos; para eso se necesita un servicio backend de alertas y notificaciones.

## Seguridad y comportamiento

Los endpoints REST del gráfico requieren la sesión existente de KAELEON. `symbol`, `timeframe` y `limit` se validan en backend y el símbolo debe existir en el catálogo online de CoinW antes de servir datos.

Los datos y herramientas del gráfico son informativos: no colocan, modifican ni cancelan órdenes; tampoco cambian tamaño, apalancamiento, PnL, TP o SL. Toda entrada externa se normaliza y se descartan valores de mercado no finitos o geométricamente inválidos.

No se muestra un precio de liquidación calculado localmente porque no debe inventarse un valor que pueda diferir de CoinW. Solo debe añadirse cuando exista una fuente o fórmula validada contra el exchange.

## Escalado

La versión actual conecta cada navegador directamente al WebSocket **público** de CoinW y usa el backend para histórico, catálogo y snapshots REST. Esto evita secretos en el navegador y mantiene la implementación sencilla. Con muchos usuarios concurrentes conviene sustituir las conexiones individuales por un hub WebSocket backend que multiplexe suscripciones por símbolo y timeframe.
