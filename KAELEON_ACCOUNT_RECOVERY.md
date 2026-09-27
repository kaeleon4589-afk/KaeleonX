# KAELEON — Recuperación segura de cuenta

## Objetivo

KAELEON dispone de dos vías independientes para restablecer una contraseña olvidada:

1. **Telegram verificado**: OTP de 6 dígitos enviado al chat vinculado durante el registro.
2. **Recovery Code KAE**: código personal de alta entropía que el usuario debe guardar fuera de la plataforma.

La contraseña existente nunca se recupera ni se muestra. Ambos métodos crean una contraseña nueva.

## Alta de Recovery Code

- El código se genera en el navegador con `crypto.getRandomValues`.
- Formato: `KAE-XXXX-XXXX-XXXX-XXXX-XXXX`.
- La pantalla bloquea el acceso al dashboard hasta que el usuario confirme que lo guardó.
- El backend recibe el código únicamente para derivar un hash scrypt con salt aleatorio.
- El valor original **no se persiste** en MongoDB.

### Usuarios nuevos

Después de verificar Telegram, KAELEON inicia la sesión y muestra la pantalla obligatoria de Recovery Code antes del dashboard.

### Usuarios existentes

No requieren migración manual. Si un documento `users` no tiene `recovery_code_hash`, `/auth/me` responde `recovery_code_required=true`. En el siguiente login/recarga autenticada, la misma pantalla de seguridad se muestra antes del dashboard.

Si un usuario antiguo ya olvidó su contraseña, puede recuperar primero por Telegram. El reset genera un Recovery Code nuevo y lo muestra una sola vez.

## Recuperación por Telegram

1. El usuario introduce su teléfono.
2. La API responde de forma neutral sin confirmar si la cuenta existe.
3. Si la cuenta está activa y Telegram está vinculado, se genera un OTP de 6 dígitos.
4. El OTP se almacena únicamente como hash scrypt, caduca en 10 minutos y admite como máximo 5 intentos.
5. Al verificar OTP + nueva contraseña:
   - se reemplaza el hash de contraseña;
   - se revocan todas las sesiones anteriores;
   - el challenge OTP queda consumido;
   - se rota el Recovery Code;
   - Telegram recibe una alerta de seguridad.

Las solicitudes de OTP están limitadas por teléfono e IP en una ventana temporal para reducir abuso.

## Recuperación por Recovery Code

1. El usuario introduce teléfono + Recovery Code + nueva contraseña.
2. La respuesta de error es genérica para no revelar qué dato es incorrecto.
3. Los intentos fallidos están limitados por teléfono e IP.
4. Un reset exitoso:
   - revoca todas las sesiones;
   - invalida el Recovery Code utilizado;
   - genera un Recovery Code nuevo;
   - muestra el nuevo código una sola vez;
   - envía alerta por Telegram si la cuenta dispone de chat vinculado.


## Regeneración desde una sesión activa

Desde **Mi cuenta → Seguridad / Recovery Code**, un usuario autenticado puede regenerar su código. La operación exige la contraseña actual, invalida inmediatamente el código anterior, genera uno nuevo y lo muestra una sola vez. Si Telegram está vinculado, se envía además una alerta de seguridad.

## Persistencia

Campos añadidos de manera compatible al documento `users`:

- `recovery_code_hash`
- `recovery_code_created_at`
- `recovery_code_used_at`
- `last_recovery_code_used_at`
- `recovery_code_rotated_at`
- `password_changed_at`
- `last_password_recovery_method`

Colecciones de seguridad:

- `password_recovery_challenges`
- `password_recovery_requests`
- `password_recovery_attempts`
- `security_events`

Los índices de MongoDB se crean automáticamente al iniciar `Database`.

## Variables de entorno

No se añadieron variables obligatorias. La recuperación por Telegram reutiliza la configuración existente del bot:

- `TELEGRAM_ENABLED`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_BOT_USERNAME`
- `TELEGRAM_WEBHOOK_URL`
- `TELEGRAM_WEBHOOK_SECRET`

Si Telegram ya funciona para registro, no hay que añadir una configuración nueva para password recovery.

## Caso sin métodos disponibles

Una cuenta antigua que simultáneamente haya perdido la contraseña, no tenga Recovery Code todavía y tampoco conserve acceso al Telegram vinculado no se restablece automáticamente. Ese caso debe ir a un procedimiento separado de soporte con verificación reforzada; no existe un bypass administrativo silencioso.
