from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_frontend_exposes_both_password_recovery_methods_and_first_login_gate():
    auth = (ROOT / "frontend/src/pages/AuthPage.tsx").read_text()
    app = (ROOT / "frontend/src/App.tsx").read_text()
    gate = (ROOT / "frontend/src/components/RecoveryCodeGate.tsx").read_text()
    regenerate = (ROOT / "frontend/src/components/RecoveryCodeRegenerateModal.tsx").read_text()
    dashboard = (ROOT / "frontend/src/pages/DashboardPage.tsx").read_text()
    api = (ROOT / "frontend/src/lib/api.ts").read_text()

    assert "¿Olvidaste tu contraseña?" in auth
    assert "RECUPERACIÓN · TELEGRAM" in auth
    assert "RECUPERACIÓN · CÓDIGO KAE" in auth
    assert "requestTelegramPasswordRecovery" in api
    assert "resetPasswordWithRecoveryCode" in api
    assert "user.recovery_code_required" in app
    assert "He guardado mi código · Continuar" in gate
    assert "navigator.clipboard.writeText" in gate
    assert "Seguridad / Recovery Code" in dashboard
    assert "regenerateRecoveryCode" in regenerate
