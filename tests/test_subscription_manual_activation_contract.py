from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def test_billing_has_user_order_history_route():
    text = (ROOT / 'app/api/billing_api.py').read_text()
    assert '@router.get("/orders")' in text
    assert '"payment_orders"' in text
    assert '"user_id": user["user_id"]' in text

def test_subscription_page_uses_real_billing_endpoints():
    api = (ROOT / 'frontend/src/lib/api.ts').read_text()
    page = (ROOT / 'frontend/src/pages/SubscriptionPage.tsx').read_text()
    assert "'/billing/plans'" in api
    assert "'/billing/orders'" in api
    assert "'/billing/orders/tx'" in api
    assert "'/billing/orders/verify'" in api
    assert 'Verificar pago' in page
    assert 'BNB Smart Chain' in page

def test_admin_manual_activation_is_visible():
    page = (ROOT / 'frontend/src/pages/AdminPage.tsx').read_text()
    assert 'ACTIVACIÓN MANUAL LIVE' in page
    assert 'Activar / extender LIVE' in page
    assert 'api.adminGrantLiveDays' in page
