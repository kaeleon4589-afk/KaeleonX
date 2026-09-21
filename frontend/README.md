# KAELEON Frontend

Frontend React + TypeScript + Vite conectado a la API FastAPI de KAELEON.

## Desarrollo

```bash
cp .env.example .env
npm install
npm run dev
```

## Producción

```bash
npm run build
npm run preview
```

Configure `VITE_API_BASE_URL` con la URL pública del backend. En el backend configure `CORS_ALLOWED_ORIGINS` con el dominio final de este frontend.

## Contrato respetado

La UI no permite elegir símbolos, timeframes ni apalancamiento porque el backend actual los controla internamente. Las métricas y operaciones visibles se obtienen de `/user/*`; no se inventan precios ni velas de mercado.

## Railway

Incluye `railway.toml` y un `server.mjs` sin dependencias externas para servir la SPA después del build. Consulta `DEPLOYMENT.md`.
