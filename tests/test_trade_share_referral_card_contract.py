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


def test_trade_share_card_uses_positive_and_negative_brand_images():
    share = (ROOT / 'frontend/src/lib/tradeShare.ts').read_text(encoding='utf-8')
    modal = (ROOT / 'frontend/src/components/trading/TradeShareModal.tsx').read_text(encoding='utf-8')
    assert "kaeleon-share-positive.png" in share
    assert "kaeleon-share-negative.png" in share
    assert "pnlValue ?? 0" in share
    assert "kaeleon-share-positive.png" in modal
    assert "kaeleon-share-negative.png" in modal


def test_platform_brand_uses_primary_logo_and_removes_chameleon_art():
    brand = (ROOT / 'frontend/src/components/Brand.tsx').read_text(encoding='utf-8')
    dashboard = (ROOT / 'frontend/src/pages/DashboardPage.tsx').read_text(encoding='utf-8')
    auth = (ROOT / 'frontend/src/pages/AuthPage.tsx').read_text(encoding='utf-8')
    assert '/images/kaeleon-brand-primary.png' in brand
    assert 'Logo principal de KAELEON' in brand
    assert '/images/kaeleon-brand-primary.png' in dashboard
    assert '/images/kaeleon-brand-primary.png' in auth
    assert '/images/kaeleon-trading-art.jpg' not in dashboard
    assert '/images/kaeleon-trading-art.jpg' not in auth
