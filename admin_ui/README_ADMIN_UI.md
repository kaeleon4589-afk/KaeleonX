# KAELEON ADMIN UI v0.10.3

Admin-only visual layer. Backend authorization remains authoritative.

Sections:
- Dashboard
- Usuarios
- LIVE / Suscripciones
- Pagos / TX
- Referidos
- Operaciones
- Motor KAELEON
- Mercados analizados
- Regímenes / estrategias
- Logs / Auditoría
- Configuración interna

Rules:
- No leverage, pair, or timeframe controls for end users.
- ADMIN data is never exposed to USER routes.
- All sensitive actions must call backend admin endpoints and generate audit events.
- Mobile-first dark trading aesthetic, responsive desktop/tablet.
