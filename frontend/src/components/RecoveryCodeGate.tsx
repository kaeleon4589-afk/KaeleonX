import { useState } from 'react';
import Brand from './Brand';
import { api, humanizeError } from '../lib/api';
import { generateRecoveryCode } from '../lib/recoveryCode';

export default function RecoveryCodeGate({ onConfigured }: { onConfigured: () => void }) {
  const [code] = useState(() => generateRecoveryCode());
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState('');

  async function copyCode() {
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      setError('No se pudo copiar automáticamente. Mantén pulsado el código para copiarlo.');
    }
  }

  async function finish() {
    if (!confirmed || busy) return;
    setBusy(true);
    setError('');
    try {
      await api.enrollRecoveryCode(code);
      onConfigured();
    } catch (err) {
      setError(humanizeError(err));
    } finally {
      setBusy(false);
    }
  }

  return <main className="recovery-gate-shell">
    <section className="recovery-gate-card">
      <Brand />
      <div className="recovery-shield">🔐</div>
      <span className="eyebrow">SEGURIDAD DE CUENTA</span>
      <h1>Guarda tu código de recuperación</h1>
      <p className="recovery-lead">Este código te permitirá recuperar tu cuenta aunque no tengas acceso a Telegram. Se configura una sola vez y KAELEON no podrá volver a mostrarte este mismo código.</p>
      <div className="recovery-code-box" role="group" aria-label="Código de recuperación">
        <span>Tu código personal</span>
        <strong>{code}</strong>
        <button type="button" className="secondary-btn" onClick={copyCode}>{copied ? '✓ Copiado' : 'Copiar código'}</button>
      </div>
      <div className="recovery-warning"><strong>Importante</strong><span>Guárdalo fuera de KAELEON: gestor de contraseñas, papel o lugar privado. No lo envíes por chat ni lo compartas con nadie.</span></div>
      <label className="recovery-confirm"><input type="checkbox" checked={confirmed} onChange={e => setConfirmed(e.target.checked)} /><span>Confirmo que he guardado mi código de recuperación en un lugar seguro.</span></label>
      {error && <div className="form-error">{error}</div>}
      <button type="button" className="primary-btn recovery-continue" disabled={!confirmed || busy} onClick={finish}>{busy ? 'Protegiendo cuenta…' : 'He guardado mi código · Continuar'}</button>
      <small>Solo se guarda una huella criptográfica del código. El código original no se almacena en la base de datos.</small>
    </section>
  </main>;
}
