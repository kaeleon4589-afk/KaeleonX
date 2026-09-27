from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_trade_share_card_contains_personal_referral_qr_and_link():
    share = (ROOT / 'frontend/src/lib/tradeShare.ts').read_text(encoding='utf-8')
    dashboard = (ROOT / 'frontend/src/pages/DashboardPage.tsx').read_text(encoding='utf-8')
    modal = (ROOT / 'frontend/src/components/trading/TradeShareModal.tsx').read_text(encoding='utf-8')
    qr = (ROOT / 'frontend/src/lib/qrCode.ts').read_text(encoding='utf-8')

    assert 'referralCode?: string | null' in share
    assert 'referralUrl?: string | null' in share
    assert 'drawQrCode(ctx, referralUrl' in share
    assert 'Únete con mi enlace de referido' in share
    assert 'window.location.origin}/?ref=' in dashboard
    assert 'referralCode:code,referralUrl' in dashboard
    assert 'share-preview-referral' in modal
    assert 'createQrDataUrl' in modal
    assert 'api.qrserver.com' not in qr
    assert 'https://' not in qr


def test_share_card_referral_identity_is_loaded_from_current_user_summary():
    dashboard = (ROOT / 'frontend/src/pages/DashboardPage.tsx').read_text(encoding='utf-8')
    assert "referrals?.referral_code" in dashboard
    assert 'const fresh=await api.referrals()' in dashboard
    assert "if(!code){setNotice('Tu código de referido todavía no está disponible.')" in dashboard
