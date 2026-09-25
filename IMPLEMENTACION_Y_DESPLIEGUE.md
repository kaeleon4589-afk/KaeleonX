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
