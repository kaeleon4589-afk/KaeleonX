import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(fileURLToPath(new URL('.', import.meta.url)), 'dist');
const port = Number(process.env.PORT || 4173);
const backendUrl = (process.env.BACKEND_URL || process.env.VITE_API_BASE_URL || 'https://kaeleonx-production.up.railway.app').replace(/\/$/, '');
const types = { '.html':'text/html; charset=utf-8', '.js':'text/javascript; charset=utf-8', '.css':'text/css; charset=utf-8', '.svg':'image/svg+xml', '.png':'image/png', '.ico':'image/x-icon', '.json':'application/json; charset=utf-8' };

async function readRequestBody(req) {
  const chunks = [];
  let total = 0;
  const maxBytes = 1024 * 1024;
  for await (const chunk of req) {
    total += chunk.length;
    if (total > maxBytes) throw new Error('request_body_too_large');
    chunks.push(chunk);
  }
  return chunks.length ? Buffer.concat(chunks) : undefined;
}

async function proxyApi(req, res) {
  try {
    const incomingUrl = new URL(req.url || '/api', 'http://frontend.local');
    const upstreamPath = incomingUrl.pathname.replace(/^\/api(?=\/|$)/, '') || '/';
    const target = `${backendUrl}${upstreamPath}${incomingUrl.search}`;
    const headers = new Headers();
    for (const name of ['authorization', 'content-type', 'accept']) {
      const value = req.headers[name];
      if (typeof value === 'string') headers.set(name, value);
    }
    headers.set('x-forwarded-proto', 'https');
    if (req.headers.host) headers.set('x-forwarded-host', req.headers.host);

    const method = req.method || 'GET';
    const body = method === 'GET' || method === 'HEAD' ? undefined : await readRequestBody(req);
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    let upstream;
    try {
      upstream = await fetch(target, { method, headers, body, redirect: 'manual', signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }

    const responseBody = Buffer.from(await upstream.arrayBuffer());
    const responseHeaders = {
      'Content-Type': upstream.headers.get('content-type') || 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
      'X-Content-Type-Options': 'nosniff',
    };
    res.writeHead(upstream.status, responseHeaders);
    res.end(responseBody);
  } catch (error) {
    console.error('API proxy error:', error instanceof Error ? error.message : error);
    res.writeHead(502, { 'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store' });
    res.end(JSON.stringify({ detail: 'backend_proxy_unavailable' }));
  }
}

createServer(async (req, res) => {
  try {
    if ((req.url || '').startsWith('/api/') || req.url === '/api') {
      await proxyApi(req, res);
      return;
    }

    const raw = decodeURIComponent((req.url || '/').split('?')[0]);
    const safe = normalize(raw).replace(/^(\.\.[/\\])+/, '');
    let file = join(root, safe === '/' ? 'index.html' : safe);
    try { if ((await stat(file)).isDirectory()) file = join(file, 'index.html'); }
    catch { file = join(root, 'index.html'); }
    const body = await readFile(file);
    res.writeHead(200, { 'Content-Type': types[extname(file)] || 'application/octet-stream', 'Cache-Control': extname(file)==='.html' ? 'no-cache' : 'public, max-age=31536000, immutable', 'X-Content-Type-Options':'nosniff' });
    res.end(body);
  } catch {
    res.writeHead(500, { 'Content-Type':'text/plain; charset=utf-8' });
    res.end('KAELEON frontend unavailable');
  }
}).listen(port, '0.0.0.0', () => console.log(`KAELEON frontend listening on :${port}; API proxy -> ${backendUrl}`));
