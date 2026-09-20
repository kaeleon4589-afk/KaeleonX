# KAELEON v0.12.0 — Revisión e implementación de cuenta CoinW por usuario

## Hallazgo raíz

La versión 0.11.0 todavía no implementaba realmente una cuenta CoinW por usuario.

- `app/config/settings.py` tenía `coinw_api_key` y `coinw_api_secret` como variables globales del servidor.
- `app/coinw/live_adapter.py` leía esas variables globales.
- `app/main.py` construía un único `LiveExecutionEngine` para todo el proceso.
- `app/execution/profile_manager.py` existía, pero era solo memoria local y no estaba conectado a MongoDB, a la autenticación ni al motor de trading.
- `/user/settings` no permitía guardar API Key, API Secret ni capital operativo.
- El `RiskManager` recibía el equity de la cuenta usada por el motor, no un capital operativo configurable por usuario.
- El dashboard de usuario devolvía capital estático si no existía estado persistido.

Por lo tanto, la configuración que el usuario necesitaba —API Key, API Secret y capital operativo— no estaba conectada al flujo LIVE real.

## Implementación v0.12.0

### 1. Credenciales CoinW por usuario

Se creó `user_trading_profiles` en MongoDB.

Cada perfil contiene:

- `user_id`
- `execution_mode`
- `trading_enabled`
- `operating_capital`
- `coinw_api_key_encrypted`
- `coinw_api_secret_encrypted`

Las credenciales se cifran con Fernet antes de persistirse. El único secreto de exchange que permanece en Railway es `CREDENTIAL_ENCRYPTION_KEY`, usado como llave maestra del servidor.

Los endpoints nunca devuelven el API Secret y solo devuelven un API Key enmascarado.

### 2. Capital operativo por usuario

Cada usuario puede configurar el capital que KAELEON está autorizado a usar.

La fórmula LIVE es:

`effective_capital = min(operating_capital, available_coinw_equity)`

Esto permite, por ejemplo, que un usuario tenga 50 USDT disponibles en CoinW y configure 10 USDT como capital operativo. El motor de riesgo usa 10 USDT y no toma automáticamente los 50 USDT como base de riesgo.

El mínimo de plataforma queda en 3 USDT por defecto y no es una variable de usuario.

### 3. Motor multiusuario

`app/main.py` ya no crea un adaptador CoinW global.

Ahora:

`CoinW market data compartida → snapshot → runtime independiente por usuario → risk → execution`

Cada usuario tiene su propio:

- `LiveExecutionEngine`
- `CoinWExecutor`
- `PositionManager`
- `TradingOrchestrator`

La información pública de mercado se comparte para no duplicar descargas de velas/order book.

### 4. Activación explícita

Se agregó `trading_enabled` con valor seguro `false` por defecto.

Un usuario debe activar explícitamente el trading automático.

Desactivar el trading impide nuevas entradas pero no elimina la capacidad del runtime de reconciliar una posición LIVE existente.

### 5. Un solo trade abierto por usuario

El orquestador bloquea nuevas entradas cuando el usuario ya tiene cualquier posición abierta, no solo cuando coincide el símbolo.

### 6. Protección de credenciales

No se permite:

- cambiar credenciales LIVE mientras el usuario está operando;
- cambiar credenciales LIVE mientras existe una posición abierta;
- cambiar LIVE a DEMO mientras existe una posición LIVE abierta;
- eliminar credenciales mientras el perfil permanece LIVE.

Esto evita desincronizar una posición de CoinW con una cuenta distinta.

### 7. Entitlement LIVE

El runtime vuelve a comprobar `live_allowed` antes de permitir nuevas entradas. Así, la expiración del trial/suscripción no deja al bot operando indefinidamente.

Una posición LIVE existente sigue siendo reconciliada/protegida aunque nuevas entradas estén bloqueadas.

### 8. Prueba de conexión CoinW

Se añadió `POST /user/coinw/test` para comprobar las credenciales almacenadas sin enviar órdenes.

El endpoint devuelve únicamente el estado de conexión, equity disponible y API Key enmascarada.

## Endpoints nuevos

- `GET /user/trading-config`
- `PUT /user/trading-config`
- `POST /user/coinw/test`
- `DELETE /user/coinw-credentials`

## Payload de configuración

```json
{
  "execution_mode": "live",
  "trading_enabled": true,
  "operating_capital": 10,
  "coinw_api_key": "...",
  "coinw_api_secret": "..."
}
```

Para actualizaciones sin cambiar credenciales, `coinw_api_key` y `coinw_api_secret` pueden omitirse y se conserva el valor cifrado existente.

## Variables de servidor relacionadas

Ya no existen ni se utilizan como credenciales globales:

- `COINW_API_KEY`
- `COINW_API_SECRET`
- `MODE`

Nueva variable obligatoria para el worker de trading y el módulo de cuenta:

- `CREDENTIAL_ENCRYPTION_KEY`

Las URLs públicas de CoinW siguen siendo valores de servidor no sensibles y mantienen sus defaults.

## Archivos modificados

- `.env.example`
- `README.md`
- `requirements.txt`
- `app/api/admin_api.py`
- `app/api/app.py`
- `app/api/user_api.py`
- `app/coinw/live_adapter.py`
- `app/config/settings.py`
- `app/main.py`
- `app/orchestrator.py`
- `app/position/manager.py`
- `app/storage/database.py`
- `app/execution/profile_manager.py` (compatibilidad existente; no participa en el runtime multiusuario)
- `docs/execution-environments-v0_8.md`
- `docs/execution-environments-v0_8_2.md`
- `tests/test_execution_environments.py`
- `tests/test_user_integration_v0101.py`

## Archivos nuevos

- `app/security/__init__.py`
- `app/security/credential_vault.py`
- `app/trading/profile.py`
- `app/trading/runtime.py`
- `docs/user-trading-config-v0_12.md`
- `tests/test_user_trading_config_v012.py`

## Validación

- `python -m compileall -q app tests` → OK
- Suite completa: **23 passed**

La suite se ejecutó en un entorno local donde `pymongo` no estaba instalado; para poder ejecutar las pruebas sin acceso de red se usó un stub temporal de `pymongo`. `requirements.txt` incluye `pymongo>=4.8`, por lo que el despliegue de Railway instalará la dependencia real.

## Resultado

La arquitectura ahora sí soporta el modelo requerido:

`Usuario → sus credenciales CoinW → su capital operativo → su runtime → su riesgo → sus órdenes/posiciones`

Las credenciales de un usuario no se reutilizan para otro usuario y no se guardan como variables globales del servidor.
