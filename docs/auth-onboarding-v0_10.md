# KAELEON Auth + Onboarding v0.10.0

## Flujo cerrado
1. Registro: país/código + teléfono + contraseña.
2. Backend normaliza teléfono a E.164 y crea `pending_verification`.
3. Se genera challenge temporal.
4. Usuario abre el bot oficial de Telegram y comparte su propio contacto.
5. Backend compara exactamente el teléfono registrado con el teléfono compartido.
6. Al pulsar VERIFICAR, la cuenta pasa a `active`.
7. Login posterior: **solo teléfono + contraseña**.
8. Sesión opaca con token revocable.
9. `/auth/me` devuelve únicamente datos públicos del usuario.
10. Tutorial se marca mediante `/auth/tutorial`; no afecta la estrategia ni el motor.
11. `/auth/logout` revoca la sesión.

## Reglas
- Telegram no participa en el login.
- No SMS/2FA.
- Contraseñas: scrypt.
- Tokens persistidos solo como SHA-256.
- Estados: `pending_verification`, `active`, `blocked`, `suspended`.
- Roles: `USER`, `ADMIN`.
- No se exponen secretos ni `password_hash`.

## Recorrido visual
`Registro → Telegram → Verificación → Login → Tutorial → Dashboard`

El frontend de referencia está en `web/auth_flow.html`. La URL real del bot debe configurarse antes del lanzamiento; no se inventa ni se fija en código.
