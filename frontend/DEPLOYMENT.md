# Despliegue del frontend

## Railway

1. Crea un servicio nuevo apuntando a este repositorio y configura el Root Directory como `frontend`.
2. Añade `VITE_API_BASE_URL=https://kaeleonx-production.up.railway.app`.
3. Railway usará `railway.toml` para compilar y servir `dist/`.
4. Cuando Railway asigne el dominio público del frontend, copia ese origen completo en el backend:

   `CORS_ALLOWED_ORIGINS=https://DOMINIO-DEL-FRONTEND`

   Si además se prueba localmente, puede ser una lista separada por comas:

   `CORS_ALLOWED_ORIGINS=https://DOMINIO-DEL-FRONTEND,http://localhost:5173`

5. Redeploy del backend después de cambiar CORS.

Nunca pongas API secrets, CoinW secrets ni tokens en variables `VITE_*`: todo lo que empiece con `VITE_` termina disponible en el navegador.
