# KAELEON — reporte de fusión (v0.10.6-merged)

## 1. Diagnóstico: qué pasó realmente

Los 7 archivos que diste no eran versiones sucesivas de la misma app completa.
Reconstruyendo el linaje por versión (v0.6 → v0.10.5) a partir del propio
`KAELEON_MASTER_AUDIT_v0_10_6` más los 5 paquetes nuevos, esto es lo que pasó:

1. **Hasta la v0.9.9 existía una app relativamente completa**: dashboard,
   ejecución, operaciones, performance, settings, referidos (versión antigua)
   y una interfaz web con páginas HTML propias (`app/ui/`).
2. **En la v0.10.0 ("auth_onboarding") hubo un reinicio.** Se conservó el
   motor (coinw, risk, regime, storage, position, strategy, market,
   execution) pero se **descartaron por completo**: `dashboard_api.py`,
   `execution_api.py`, `operations_api.py`, `performance_api.py`,
   `settings_api.py`, el módulo `app/performance.py`, y todo `app/ui/`
   (páginas HTML de ejecución/operaciones/performance/settings + `index.html`).
   Nada de esto volvió a aparecer en ninguna versión posterior.
3. **A partir de v0.10.0, el trabajo se dividió en 3 ramas que nunca se
   fusionaron entre sí:**
   - `user_integration` (v0.10.1) — endpoints de usuario (`/user/*`)
   - `billing_referrals` (v0.10.2) — suscripciones y referidos
   - `admin_ui → admin_integration → admin_data_views` (v0.10.3 → v0.10.5) — panel admin

   Verificado directamente: el paquete de billing no tiene ni una sola
   referencia a `admin`, el de admin no tiene ni una a `billing`/`referrals`,
   y el de user_integration no tiene ninguna de las dos. Cada zip que
   recibiste era literalmente solo esa rama — de ahí la sensación de
   "archivos incompletos".

4. **Los archivos compartidos entre las 3 ramas divergieron de forma
   distinta en cada una**, sin que ninguna rama supiera de las otras:
   `app/api/app.py`, `app/config/settings.py`, `.env.example`,
   `app/auth/service.py`, `app/api/auth_api.py`, `app/storage/database.py`,
   `app/orchestrator.py`. En particular, la rama admin (v0.10.3-10.5) se
   había ramificado de un punto **anterior** a que se agregaran los
   endpoints `/auth/me`, `/auth/tutorial` y `/auth/logout` — si se hubiera
   usado esa rama tal cual como base, esos 3 endpoints se habrían perdido
   silenciosamente.

## 2. Cómo se hizo la fusión

Base: rama admin (`v0.10.5`, la más completa en módulos de motor + panel
admin). Sobre esa base:

| Archivo | Resolución |
|---|---|
| `app/auth/service.py` | Versión completa de v0.10.0/10.1 (con `get_current_user`, `public_user`, `set_tutorial_completed`, `revoke_session`) + `authenticate_token` de la rama admin (la usa `admin_api.py`) + chequeo de cuenta suspendida (`status="suspended"`/`ban_until`) de la rama admin + soporte de `referral_code` en `create_registration` de la rama billing |
| `app/api/auth_api.py` | Base completa (incluye `/me`, `/tutorial`, `/logout`, ausentes en la rama admin) + campo `referral_code` en el registro |
| `app/orchestrator.py` | Versión de `user_integration` (etiqueta cada posición/decisión con `user_id`) — **sin este cambio, los endpoints `/user/operations` y `/user/decision/{id}` no habrían encontrado nada**, porque filtran por `user_id` y la rama admin/billing nunca lo escribían |
| `app/storage/database.py` | Versión de la rama admin (tiene `find_many`/`update_many`/`count`, usados por las vistas admin) + los 2 índices de `payment_orders` que le faltaban (únicos por `payment_order_id`, índice sparse por `tx_hash`) que sí traía la rama billing |
| `app/config/settings.py` | Unión de todos los campos de las 3 ramas: base + `fixed_leverage` (rename acordado por billing y admin de forma independiente) + `admin_phone` (admin) + `live_trial_days`/`payment_wallet`/`payment_network`/`bsc_rpc_url`/`usdt_bsc_contract`/`usdt_decimals` (billing) |
| `.env.example` | Unión equivalente. Corregido además un bug real: la rama billing documentaba las variables de suscripción con prefijo `KAELEON_` (`KAELEON_PAYMENT_WALLET`, etc.) pero el código (`Settings`, sin `env_prefix`) las lee **sin** ese prefijo — con el `.env.example` original, esas variables nunca se habrían cargado. |
| `app/api/app.py` | Reescrito para registrar los 4 routers juntos: `auth_router`, `admin_router`, `billing_router`, `user_router` |
| `app/billing/`, `app/referrals/`, `app/api/billing_api.py` | Agregados tal cual de la rama billing (no tenían conflicto con nada más) |
| `app/api/user_api.py`, `web/auth_flow.html` | Agregados tal cual de la rama user_integration |
| `admin_ui/`, `app/admin/`, `app/api/admin_api.py` | Agregados tal cual de la rama admin |
| `requirements.txt` | Idéntico en las 3 ramas, sin cambios |
| tests / docs | Unión de los archivos únicos de cada rama (sin colisiones de nombre con contenido distinto) |

Verificación hecha sobre el resultado:
- **`py_compile` sobre los ~50 archivos `.py` del repo: sin errores de sintaxis.**
- Chequeo estático de todos los `from app.X import Y` internos contra lo
  realmente definido en cada módulo, para detectar imports rotos (ver
  sección 4 — encontró un bug real preexistente, no introducido por la fusión).
- **No pude instalar `fastapi`/`pydantic`/`pymongo` ni correr la suite de
  `pytest` real**, porque este entorno no tiene acceso a red saliente. La
  verificación de imports fue estática (AST), no una ejecución real. Antes
  de desplegar, corré `pip install -r requirements.txt && pytest` vos mismo
  como primer paso — es probable que salga limpio dado que el análisis
  estático no encontró problemas en el código nuevo, pero no es lo mismo
  que verlo pasar de verdad.

## 3. Decisión importante que tomé y que deberías revisar

**No reconstruí `dashboard_api`, `execution_api`, `operations_api`,
`performance_api`, `settings_api` ni `app/ui/`** (el punto 2 del diagnóstico).
Existían en v0.9.9 pero fueron descartados antes de que llegara ninguno de
los 6 paquetes que me diste, así que no tengo una versión "última" de esos
módulos que sea compatible con el sistema de auth actual (ese código viejo
ni siquiera usa `Bearer` tokens de la forma en que auth funciona hoy). Están
disponibles en `KAELEON_MASTER_AUDIT_v0_10_6/sources/KAELEON_settings_v0_9_9/`
si los querés recuperar, pero traerlos tal cual sin adaptarlos al auth
actual los dejaría rotos. Decime si eso era funcionalidad que necesitás y
lo encaramos como tarea aparte.

## 4. Bug preexistente encontrado (no introducido por esta fusión)

`app/regime/breadth.py`, `liquidity.py`, `momentum.py`, `volatility.py` y
`app/regime/structure/engine.py` importan y usan clases (`BreadthResult`,
`LiquidityResult`, `MomentumResult`, `VolatilityResult`, `StructureResult`)
que **no existen** en `app/models/regime.py` (que solo define `EngineSignal`
y `RegimeResult`). Esto ya estaba así desde v0.9.9 — no lo modifiqué.
Confirmé que **hoy no rompe nada en producción** porque ningún otro archivo
del proyecto importa esos 5 módulos (el motor de régimen real que sí está
conectado, `app/regime/regime_engine.py`, usa solo `EngineSignal`/
`RegimeResult`, que sí existen). Es código huérfano de un refactor que
quedó a medio hacer. No lo edité porque no tengo forma de saber qué campos
debían llevar esas clases — lo dejo señalado para que lo completes o lo
borres.

## 5. Qué falta para producción real

- Correr `pip install -r requirements.txt && pytest` de verdad (ver punto 2).
- Variables de entorno reales: `MONGODB_URI`, credenciales de CoinW,
  `PAYMENT_WALLET` y `USDT_BSC_CONTRACT` para cobros en vivo.
- Decidir qué hacer con el bug del punto 4.
- Decidir si se recupera la funcionalidad del punto 3.
- No hay CI configurado, ni Dockerfile, ni definición de despliegue —
  ninguno de los 7 paquetes originales lo traía.
