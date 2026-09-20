# KAELEON v0.11.0 — Telegram registration integration

## Resultado

Se completó el flujo de verificación de registro mediante un bot real de Telegram usando webhook sobre el mismo FastAPI.

### Flujo implementado

`Registro web → challenge de 1 hora → enlace profundo t.me/<bot>?start=<challenge> → /start del bot → botón request_contact → verificación del teléfono → retorno a KAELEON → /auth/verify → cuenta active`

Telegram documenta que `setWebhook` entrega los updates por HTTPS y permite protegerlos con `secret_token`, enviado después en `X-Telegram-Bot-Api-Secret-Token`. También documenta `request_contact` para compartir el número del usuario en chats privados. citeturn246916search0turn246916search4

## Cambios principales

- Se añadió `TELEGRAM_BOT_TOKEN`.
- Se añadió `TELEGRAM_BOT_USERNAME`.
- Se añadió `TELEGRAM_WEBHOOK_URL`.
- Se añadió `TELEGRAM_WEBHOOK_SECRET`.
- Se añadió `TELEGRAM_ENABLED`.
- Se añadió `TELEGRAM_AUTO_SET_WEBHOOK`.
- Se añadió `TELEGRAM_API_TIMEOUT_SECONDS`.
- El registro devuelve `telegram_verification_url`.
- El frontend muestra un botón real para abrir el bot.
- El backend recibe `POST /telegram/webhook`.
- Ya no se usa un endpoint público `/auth/telegram/contact` para fingir que el request viene de Telegram.
- El bot exige que el contacto compartido pertenezca al mismo Telegram user que inició el challenge.
- El estado del bot guarda el hash del challenge, no el challenge en texto plano.
- El webhook se configura automáticamente al iniciar FastAPI cuando está habilitado.
- `/health` ahora reporta la versión `0.11.0` y el estado de configuración de Telegram.

## Archivos modificados

- `.env.example`
- `README.md`
- `app/api/app.py`
- `app/api/auth_api.py`
- `app/auth/service.py`
- `app/config/settings.py`
- `app/storage/database.py`
- `web/auth_flow.html`
- `tests/test_user_integration_v0101.py`

## Archivos nuevos

- `app/telegram/__init__.py`
- `app/telegram/bot.py`
- `app/api/telegram_api.py`
- `tests/test_telegram_registration.py`
- `docs/telegram-registration-v0_11.md`

`requirements.txt` no necesita una librería de Telegram adicional; se utiliza `httpx`, que ya estaba presente en el proyecto.

## Validación

- `python -m compileall -q app tests` → OK.
- Pruebas Telegram específicas → `2 passed`.
- Flujo de usuario + Telegram + core LIVE → `6 passed`.
- Suite completa disponible en este entorno (usando un stub temporal de `pymongo`, porque el entorno no tiene ese paquete instalado) → `20 passed`.

La suite se ejecutó con un stub únicamente para permitir el import de `pymongo`; el `requirements.txt` ya contiene `pymongo>=4.8` para el entorno real.

## Railway

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=<TOKEN_DE_BOTFATHER>
TELEGRAM_BOT_USERNAME=<USERNAME_DEL_BOT_SIN_@>
TELEGRAM_WEBHOOK_URL=https://<DOMINIO_PUBLICO>/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=<SECRETO_ALEATORIO>
TELEGRAM_AUTO_SET_WEBHOOK=true
TELEGRAM_API_TIMEOUT_SECONDS=10
```

`TELEGRAM_WEBHOOK_URL` debe apuntar al dominio público del servicio FastAPI que exponga `/telegram/webhook`.
