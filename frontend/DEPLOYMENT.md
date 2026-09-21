# Railway deployment

Deploy this directory as a separate Railway service with `Root Directory = frontend`.

Build command:

    npm install --no-audit --no-fund && npm run build

Start command:

    npm start

Runtime variable (recommended):

    BACKEND_URL=https://kaeleonx-production.up.railway.app

For compatibility, `VITE_API_BASE_URL` is also accepted by `server.mjs`, but the browser no longer connects to the backend domain directly. The browser calls the frontend's same-origin `/api/*` route, and the Node server proxies it to FastAPI. This removes browser CORS/preflight from the login/dashboard path.

`CORS_ALLOWED_ORIGINS` may remain configured on the backend for other browser clients, but this frontend no longer depends on it.
