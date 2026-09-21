# Implementación KAELEON Frontend

## Funcionalidad conectada

- Registro, verificación Telegram, login, sesión y logout.
- Lectura y actualización de capital operativo.
- Configuración segura de API Key / Secret CoinW a través del backend.
- Prueba de conexión CoinW y eliminación de credenciales.
- Cambio DEMO/LIVE respetando `live_allowed`.
- Activación de prueba LIVE.
- Activar/pausar trading mediante `trading_enabled`.
- Estado de ejecución, estado CoinW y motor.
- Operaciones abiertas/cerradas y métricas reales de rendimiento.
- Refresco automático cada 15 segundos y refresco manual.
- Estados de carga, error, éxito y sesión expirada.
- Diseño responsive para escritorio, tablet y móvil.

## Decisiones de contrato

No se implementaron controles manuales de leverage, símbolos ni timeframes. El backend declara esas funciones como internas/automáticas. Tampoco se muestra un gráfico de velas ficticio; en su lugar se representa la curva de capital derivada de operaciones reales.

## Seguridad

- El token de sesión se guarda en `localStorage`; es el mecanismo compatible con el contrato Bearer actual. Como mejora futura, si el backend migra a cookies HttpOnly/SameSite, conviene eliminar el token del almacenamiento JavaScript.
- Nunca se persiste el API Secret de CoinW en el frontend.
- Las variables `VITE_*` se consideran públicas.
- CORS debe restringirse al dominio real del frontend.
