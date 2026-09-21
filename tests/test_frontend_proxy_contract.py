from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_uses_same_origin_api_proxy():
    api = (ROOT / 'frontend/src/lib/api.ts').read_text(encoding='utf-8')
    server = (ROOT / 'frontend/server.mjs').read_text(encoding='utf-8')
    assert "const API_BASE = '/api';" in api
    assert "process.env.BACKEND_URL" in server
    assert "replace(/^\\/api" in server
    assert "backend_proxy_unavailable" in server
