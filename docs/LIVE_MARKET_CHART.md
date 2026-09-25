# Gráfico nativo de mercado CoinW

## Alcance

El dashboard incorpora un gráfico financiero propio de KAELEON debajo de las operaciones activas. El renderizado se hace con KLineChart 10.0.3; no se incrusta TradingView ni otro iframe. El catálogo y las velas provienen de las APIs públicas de CoinW Futures.

El buscador no usa una lista fija: consulta `/v1/perpum/instruments`, conserva únicamente instrumentos `online` y permite localizar cualquier contrato perpetuo disponible en el catálogo de CoinW, incluyendo mercados USDT y USDC soportados por ese endpoint.

## Datos

- Histórico: backend autenticado `GET /market/candles` -> CoinW `/v1/perpumPublic/klines`.
- Catálogo: backend autenticado `GET /market/instruments` -> CoinW `/v1/perpum/instruments`.
- Tiempo real: WebSocket público de CoinW `wss://ws.futurescw.com/perpum`, canales `candles_swap_utc` y `mark_price`.
- Fallback: si el WebSocket se corta, el frontend sigue actualizando la última vela mediante REST cada 5 segundos y reintenta el socket con backoff.

El WebSocket del navegador usa exclusivamente canales públicos de mercado; no se envían API Key, API Secret ni credenciales del usuario a CoinW desde el frontend.

## Funciones de interfaz

- Temporalidades: 1m, 3m, 5m, 15m, 30m, 1H, 4H y 1D.
- Velas japonesas y vista de área.
- Zoom, desplazamiento, crosshair y retorno a tiempo real.
- Last Price y Mark Price.
- Indicadores principales MA, EMA y BOLL.
- Panel inferior VOL, MACD o RSI.
- Pantalla completa y diseño responsive.
- Búsqueda dinámica de cualquier contrato CoinW Futures online.
- Al abrir una operación, el gráfico sigue automáticamente el par principal.
- Si el usuario explora otro par, aparece un botón para volver a la operación activa sin bloquear el buscador.
- ENTRY, SL, TP/TP1/TP2 se dibujan como líneas bloqueadas dentro del gráfico, con etiqueta y precio. No pueden arrastrarse, porque reflejan los niveles reales registrados por el motor.

## Seguridad y comportamiento

Los endpoints REST del gráfico requieren la sesión existente de KAELEON. Las entradas de `symbol`, `timeframe` y `limit` se validan en backend. El símbolo solicitado debe existir en el catálogo online de CoinW antes de obtener velas.

Los datos del gráfico son informativos y no modifican órdenes, tamaño, apalancamiento, PnL ni niveles de riesgo. Los overlays leen los valores ya guardados en la posición.

## Escalado pendiente

La versión actual conecta cada navegador directamente al WebSocket **público** de CoinW y usa el backend para histórico, búsqueda y fallback. Esto simplifica el despliegue y evita manejar secretos en el socket. Si el número de usuarios concurrentes crece de forma significativa, conviene sustituirlo por un hub WebSocket backend compartido para multiplexar suscripciones y reducir conexiones hacia CoinW.
