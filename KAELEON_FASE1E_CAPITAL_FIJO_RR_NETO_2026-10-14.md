# KAELEON 1E — corrección: asignación fija y viabilidad neta (14/10/2026)

**Base exacta**: `KaeleonX_FASE1E_ESCALABILIDAD_CONFIRMACION_2026-10-10.zip`.
Este paquete modifica **exclusivamente 1E**. 1F no se toca.

## Capital DEMO
- El usuario elige el capital operativo (por ejemplo 20, 50, 100 USDT); dicho importe permanece guardado **exactamente** hasta que el usuario lo edite.
- Seleccionar el 100% del saldo NO activa interés compuesto.
- Si el saldo aumenta, la asignación **NO aumenta** automáticamente.
- Si el saldo cae por debajo de la asignación, el motor no puede gastar saldo inexistente y limita el margen operativo al saldo disponible. **No cambia la asignación guardada**. No se incorpora ninguna parada adicional por rachas/día/drawdown.
- Se migran automáticamente los perfiles anteriores con `demo_auto_compound=True` a `False` en la primera lectura, manteniendo capital registrado, balances, posiciones y estadísticas.
- Se preserva reserva de comisión de apertura en la lógica actual de tamaño; la asignación es el objetivo de margen máximo, no una garantía de gastarlo exactamente si solo queda justo el saldo.

## RR de ejecución neto
- Para señales DEMO V2, el último guard de ejecución calcula el RR neto aproximado (TP neto / pérdida SL neta), considerando la comisión estimada de entrada+salida y el deslizamiento de salida estimado. Rechaza solo si es menor de 1.0, con causa `net_execution_rr_too_low` y telemetría para observar frecuencia.
- No se alteran SL, TP, estrategias, quality ni confirmaciones; la estimación no asegura rentabilidad ni predice resultados y puede divergir de spreads/deslizamiento reales.

## Qué NO se ha implementado
- Sin cooldowns extra, paradas por pérdidas consecutivas, límites diarios ni drawdown, sin valores fijos forzados (25 USDT u otros), sin cambios LIVE ni a 1F.
- Ninguna limpieza de base de datos, reset de métricas, migración destructiva o servicio nuevo.

- Se permite guardar de nuevo la asignación **sin cambiar su valor** cuando el saldo queda por debajo (por ejemplo, al pausar/activar trading). Nuevas asignaciones mayores que el saldo siguen rechazadas.
- El frontend muestra la nueva regla y elimina el aviso antiguo de capitalización automática.

## Despliegue
1. Hacer backup del servicio 1E y guardar ZIP anterior para rollback.
2. Pausar entradas nuevas de 1E desde el panel antes de desplegar. Comprobar posiciones abiertas. No detener la gestión de salidas sin comprobar protección.
3. Desplegar en el servicio 1E existente, con las variables y MongoDB anteriores, **sin reset de estadísticas**.
4. Verificar en `/user/trading-config` que `demo_auto_compound=false`, el capital configurado es idéntico al anterior y `demo_available_equity` conserva el saldo real.
5. Confirmar que `SIGNAL_REJECTED reason=net_execution_rr_too_low` se registra si ocurre; controlar volumen operativo y motivos de rechazo durante la prueba.

**NOTA**: el RR neto mínimo de 1 es una protección económica de cada entrada, no una parada. Si el mercado no ofrece condiciones válidas, el motor puede operar menos. No se aumentan ni relajan filtros para forzar actividad.
