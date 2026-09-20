# KAELEON — repositorio consolidado (v0.12.0)

Backend de trading (FastAPI + MongoDB) con autenticación por teléfono/Telegram,
motor de ejecución (demo/live), gestión de riesgo y régimen de mercado,
suscripciones de pago en USDT (BNB Smart Chain), referidos, y panel de
administración.

Este repositorio es el resultado de fusionar 6 entregas parciales que se
habían desarrollado en ramas separadas y nunca se habían integrado entre sí.
Ver `MERGE_REPORT.md` para el detalle completo de qué se fusionó, qué
conflictos hubo y cómo se resolvieron.

## Estructura

```
app/
  admin/        control de acceso del panel admin
  api/          routers FastAPI: auth, admin, billing, user
  auth/         registro, login por teléfono, verificación Telegram, sesiones
  billing/      suscripciones LIVE, órdenes de pago, verificación on-chain
  coinw/        integración con el exchange CoinW
  config/       settings (pydantic-settings, vía .env)
  execution/    motor de ejecución demo/live
  security/     cifrado de credenciales de exchange
  trading/      perfiles y runtimes de trading por usuario
  logging/      logger estructurado
  market/       coordinación de datos de mercado
  models/       modelos de dominio (enums, market, regime, trading)
  position/     gestión de posiciones
  referrals/    códigos de referido y recompensas
  regime/       motor de régimen de mercado (ver nota en MERGE_REPORT.md)
  risk/         cálculo de riesgo por operación
  storage/      capa de persistencia (MongoDB con fallback en memoria)
  strategy/     lógica de estrategia
  main.py       entrypoint
  orchestrator.py  orquestador del ciclo de trading (ahora con user_id)
admin_ui/       frontend estático del panel admin (HTML/JS/CSS)
web/            página de flujo de autenticación
tests/          pytest
docs/           notas de diseño de cada bloque construido
```

## Arrancar en local

```bash
pip install -r requirements.txt
cp .env.example .env   # completar credenciales reales
uvicorn app.api.app:app --reload
```

Sin `MONGODB_URI` configurado, `Database` cae a un almacenamiento en memoria
(pensado para tests, no para producción).

## Endpoints principales

- `POST /auth/register` — alta con teléfono, admite `referral_code` opcional
- `POST /auth/verify` — consume la verificación generada por el bot de Telegram; el webhook público es `POST /telegram/webhook`
- `POST /auth/login`, `GET /auth/me`, `POST /auth/tutorial`, `POST /auth/logout`
- `GET /user/dashboard`, `/user/execution`, `/user/operations`, `/user/performance`, `/user/settings`
- `GET/PUT /user/trading-config`, `POST /user/coinw/test`, `DELETE /user/coinw-credentials`
- `GET /billing/plans`, `/billing/entitlement`, `POST /billing/live/activate-trial`, `/billing/orders`, `/billing/orders/tx`, `/billing/orders/verify`
- `/admin/*` — panel de administración (ver `admin_ui/admin_api_contract.json`)

## Antes de producción

Ver la sección "Qué falta para producción real" en `MERGE_REPORT.md`.

### User CoinW configuration
Each user configures their own CoinW API Key, API Secret, execution mode, trading capital and automation toggle. Exchange credentials are encrypted at rest; only `CREDENTIAL_ENCRYPTION_KEY` is stored on the server. See `docs/user-trading-config-v0_12.md`.

### Telegram registration verification
The registration flow uses the Telegram Bot API webhook. Configure `TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `TELEGRAM_WEBHOOK_URL`, and `TELEGRAM_WEBHOOK_SECRET`. When `TELEGRAM_AUTO_SET_WEBHOOK=true`, the API configures the webhook on startup. See `docs/telegram-registration-v0_11.md`.
