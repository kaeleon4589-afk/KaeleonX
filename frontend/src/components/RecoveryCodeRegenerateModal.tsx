import { FormEvent, useState } from 'react';
import { EyeIcon } from './Icons';
import { api, humanizeError } from '../lib/api';

export default function RecoveryCodeRegenerateModal({onClose}:{onClose:()=>void}){
  const [password,setPassword]=useState('');
  const [showPassword,setShowPassword]=useState(false);
  const [newCode,setNewCode]=useState('');
  const [saved,setSaved]=useState(false);
  const [copied,setCopied]=useState(false);
  const [busy,setBusy]=useState(false);
  const [error,setError]=useState('');

  async function regenerate(e:FormEvent){
    e.preventDefault(); setBusy(true); setError('');
    try{
      const result=await api.regenerateRecoveryCode(password);
      setNewCode(result.recovery_code); setPassword(''); setSaved(false);
    }catch(err){setError(humanizeError(err));}finally{setBusy(false)}
  }
  async function copy(){
    try{await navigator.clipboard.writeText(newCode);setCopied(true);window.setTimeout(()=>setCopied(false),1800)}
    catch{setError('No se pudo copiar automáticamente. Mantén pulsado el código para copiarlo.')}
  }

  return <div className="security-modal-backdrop" role="presentation">
    <section className="security-modal" role="dialog" aria-modal="true" aria-label="Regenerar código de recuperación">
      {!newCode ? <>
        <div className="security-modal-head"><div><span className="eyebrow">SEGURIDAD</span><h2>Regenerar código de recuperación</h2></div><button type="button" onClick={onClose} aria-label="Cerrar">×</button></div>
        <p>El código actual quedará invalidado inmediatamente. Confirma tu contraseña para generar uno nuevo.</p>
        <form className="auth-form" onSubmit={regenerate}>
          <label>Contraseña actual<div className="password-field"><input type={showPassword?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} minLength={8} required autoComplete="current-password"/><button type="button" className="password-eye" onClick={()=>setShowPassword(v=>!v)}><EyeIcon/></button></div></label>
          {error&&<div className="form-error">{error}</div>}
          <button className="primary-btn" disabled={busy||password.length<8}>{busy?'Regenerando…':'Regenerar código'}</button>
          <button type="button" className="text-btn" onClick={onClose}>Cancelar</button>
        </form>
      </> : <>
        <div className="security-modal-head"><div><span className="eyebrow">NUEVO CÓDIGO</span><h2>Guárdalo ahora</h2></div></div>
        <p>Tu código anterior ya no funciona. KAELEON no volverá a mostrar este código nuevo.</p>
        <div className="recovery-code-box compact"><span>Nuevo código de recuperación</span><strong>{newCode}</strong><button type="button" className="secondary-btn" onClick={copy}>{copied?'✓ Copiado':'Copiar código'}</button></div>
        <label className="recovery-confirm"><input type="checkbox" checked={saved} onChange={e=>setSaved(e.target.checked)}/><span>Confirmo que guardé el nuevo código en un lugar seguro.</span></label>
        {error&&<div className="form-error">{error}</div>}
        <button type="button" className="primary-btn" disabled={!saved} onClick={onClose}>Terminar</button>
      </>}
    </section>
  </div>
}
