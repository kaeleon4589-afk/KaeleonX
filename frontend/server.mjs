import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = join(fileURLToPath(new URL('.', import.meta.url)), 'dist');
const port = Number(process.env.PORT || 4173);
const types = { '.html':'text/html; charset=utf-8', '.js':'text/javascript; charset=utf-8', '.css':'text/css; charset=utf-8', '.svg':'image/svg+xml', '.png':'image/png', '.ico':'image/x-icon', '.json':'application/json; charset=utf-8' };

createServer(async (req, res) => {
  try {
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
}).listen(port, '0.0.0.0', () => console.log(`KAELEON frontend listening on :${port}`));
