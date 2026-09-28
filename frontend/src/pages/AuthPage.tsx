import { FormEvent, useMemo, useState } from 'react';
import Brand from '../components/Brand';
import { EyeIcon } from '../components/Icons';
import { api, humanizeError, session } from '../lib/api';
import { countries, flagEmoji } from '../lib/countries';
import type { User } from '../types';

type Mode = 'login'|'register'|'verify'|'forgot';
type ForgotMethod = 'choose'|'telegram'|'recovery';

export default function AuthPage({onAuthenticated}:{onAuthenticated:(user:User)=>void}) {
  const [mode,setMode]=useState<Mode>('login');
  const [phone,setPhone]=useState('');
  const [country,setCountry]=useState('+52');
  const [password,setPassword]=useState('');
  const [showPassword,setShowPassword]=useState(false);
  const [referral,setReferral]=useState(()=>new URLSearchParams(window.location.search).get('ref')||'');
  const [challenge,setChallenge]=useState('');
  const [telegramUrl,setTelegramUrl]=useState('');
  const [error,setError]=useState('');
  const [busy,setBusy]=useState(false);
  const [forgotMethod,setForgotMethod]=useState<ForgotMethod>('choose');
  const [telegramRecoveryChallenge,setTelegramRecoveryChallenge]=useState('');
  const [otp,setOtp]=useState('');
  const [recoveryCode,setRecoveryCode]=useState('');
  const [newPassword,setNewPassword]=useState('');
  const [showNewPassword,setShowNewPassword]=useState(false);
  const [rotatedRecoveryCode,setRotatedRecoveryCode]=useState('');
  const [savedRotatedCode,setSavedRotatedCode]=useState(false);
  const [copied,setCopied]=useState(false);
  const countryValue=useMemo(()=>countries.find(c=>c.dial===country)?.iso||'MX',[country]);

  function resetMessages(){ setError(''); }
  function openForgot(){
    setMode('forgot'); setForgotMethod('choose'); setTelegramRecoveryChallenge(''); setOtp(''); setRecoveryCode(''); setNewPassword(''); setRotatedRecoveryCode(''); setSavedRotatedCode(false); resetMessages();
  }
  function returnToLogin(){
    setMode('login'); setForgotMethod('choose'); setTelegramRecoveryChallenge(''); setOtp(''); setRecoveryCode(''); setNewPassword(''); setRotatedRecoveryCode(''); setSavedRotatedCode(false); resetMessages();
  }

  async function submit(e:FormEvent){
    e.preventDefault(); setError(''); setBusy(true);
    try{
      if(mode==='login'){
        const res=await api.login(phone,password,country); session.set(res.access_token); const me=await api.me(); onAuthenticated(me); return;
      }
      if(mode==='register'){
        const res=await api.register({phone,password,country_code:country,referral_code:referral}); setChallenge(res.challenge); setTelegramUrl(res.telegram_verification_url); setMode('verify'); return;
      }
      if(mode==='verify'){
        await api.verify(challenge);
        // Complete registration without making the user type the password twice.
        // App.tsx will block the dashboard until the recovery code is saved.
        const loginResult=await api.login(phone,password,country);
        session.set(loginResult.access_token);
        const me=await api.me();
        onAuthenticated(me);
      }
    }catch(err){setError(humanizeError(err));}finally{setBusy(false)}
  }

  async function requestTelegramRecovery(){
    setError(''); setBusy(true);
    try{
      const result=await api.requestTelegramPasswordRecovery(phone,country);
      setTelegramRecoveryChallenge(result.challenge);
      setError('Si tu cuenta tiene Telegram verificado, te enviamos un código de 6 dígitos.');
    }catch(err){setError(humanizeError(err));}finally{setBusy(false)}
  }

  async function confirmTelegramRecovery(e:FormEvent){
    e.preventDefault(); setError(''); setBusy(true);
    try{
      const result=await api.confirmTelegramPasswordRecovery(telegramRecoveryChallenge,otp,newPassword);
      session.clear(); setRotatedRecoveryCode(result.recovery_code); setSavedRotatedCode(false);
    }catch(err){setError(humanizeError(err));}finally{setBusy(false)}
  }

  async function resetWithRecoveryCode(e:FormEvent){
    e.preventDefault(); setError(''); setBusy(true);
    try{
      const result=await api.resetPasswordWithRecoveryCode(phone,recoveryCode,newPassword,country);
      session.clear(); setRotatedRecoveryCode(result.recovery_code); setSavedRotatedCode(false);
    }catch(err){setError(humanizeError(err));}finally{setBusy(false)}
  }

  async function copyRotatedCode(){
    try{ await navigator.clipboard.writeText(rotatedRecoveryCode); setCopied(true); window.setTimeout(()=>setCopied(false),1800); }
    catch{ setError('No se pudo copiar automáticamente. Mantén pulsado el código para copiarlo.'); }
  }

  const phoneField=<label>Teléfono<div className="phone-row"><select className="country country-select" value={countryValue} onChange={e=>{const c=countries.find(x=>x.iso===e.target.value); if(c)setCountry(c.dial)}} aria-label="País">{countries.map(c=><option key={`${c.iso}-${c.dial}`} value={c.iso}>{flagEmoji(c.iso)} {c.name} {c.dial}</option>)}</select><input value={phone} onChange={e=>setPhone(e.target.value.replace(/[^0-9\s-]/g,''))} placeholder="59494299" inputMode="tel" required minLength={7}/></div></label>;

  return <main className="auth-shell">
    <div className="auth-glow auth-glow-one"/><div className="auth-glow auth-glow-two"/>
    <section className="auth-side"><Brand/><div className="auth-hero"><img src="/images/kaeleon-brand-primary.png" alt="Logo principal de KAELEON" fetchPriority="high"/></div><div className="auth-copy"><span className="eyebrow">PLATAFORMA KAELEON</span><h1>Disciplina, estrategia y ejecución.</h1><p>Conecta tu cuenta y define tu capital para operar con KAELEON.</p></div><div className="auth-status"><i/> API segura · CoinW · Telegram</div></section>
    <section className="auth-panel"><div className="auth-card">
      <div className="auth-mobile-brand"><img src="/images/kaeleon-brand-primary.png" alt="Logo principal de KAELEON" fetchPriority="high"/></div>

      {(mode==='login'||mode==='register') && <>
        <div className="auth-tabs"><button className={mode==='login'?'active':''} onClick={()=>{setMode('login');setError('')}}>Iniciar sesión</button><button className={mode==='register'?'active':''} onClick={()=>{setMode('register');setError('')}}>Crear cuenta</button></div>
        <div className="auth-title"><h2>{mode==='login'?'Bienvenido de nuevo':'Crea tu cuenta'}</h2><p>{mode==='login'?'Accede a tu panel de ejecución.':'El teléfono debe coincidir con el que compartirás con Telegram.'}</p></div>
        <form onSubmit={submit} className="auth-form">
          {phoneField}
          <label>Contraseña<div className="password-field"><input type={showPassword?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} placeholder="Mínimo 8 caracteres" required minLength={8}/><button type="button" className="password-eye" onClick={()=>setShowPassword(v=>!v)} aria-label={showPassword?'Ocultar contraseña':'Mostrar contraseña'} aria-pressed={showPassword}><EyeIcon/></button></div></label>
          {mode==='register' && <label>Código de referido <span>(opcional)</span><input value={referral} onChange={e=>setReferral(e.target.value)} placeholder="KAELEON-..."/></label>}
          {error && <div className="form-error">{error}</div>}
          <button className="primary-btn auth-submit" disabled={busy}>{busy?'Procesando…':mode==='login'?'Entrar a KAELEON':'Continuar con Telegram'}</button>
          {mode==='login' && <button type="button" className="forgot-password-link" onClick={openForgot}>¿Olvidaste tu contraseña?</button>}
        </form>
      </>}

      {mode==='verify' && <div className="verify-card"><span className="verify-icon">✓</span><h2>Verifica tu teléfono</h2><p>Abre el bot de Telegram y comparte el mismo número con el que acabas de registrarte.</p>{telegramUrl && <a className="primary-btn telegram-btn" href={telegramUrl} target="_blank" rel="noreferrer">Abrir Telegram</a>}<button className="secondary-btn" disabled={busy} onClick={(e)=>submit(e as unknown as FormEvent)}>{busy?'Comprobando…':'Ya verifiqué mi número'}</button>{error && <div className="form-error">{error}</div>}<button className="text-btn" onClick={()=>setMode('register')}>Volver</button></div>}

      {mode==='forgot' && <div className="password-recovery-card">
        {rotatedRecoveryCode ? <>
          <span className="verify-icon">✓</span><h2>Contraseña actualizada</h2><p>Tu código de recuperación anterior ya no funciona. Guarda este código nuevo antes de volver al inicio de sesión.</p>
          <div className="recovery-code-box compact"><span>Nuevo código de recuperación</span><strong>{rotatedRecoveryCode}</strong><button type="button" className="secondary-btn" onClick={copyRotatedCode}>{copied?'✓ Copiado':'Copiar código'}</button></div>
          <label className="recovery-confirm"><input type="checkbox" checked={savedRotatedCode} onChange={e=>setSavedRotatedCode(e.target.checked)}/><span>Confirmo que guardé mi nuevo código.</span></label>
          <button type="button" className="primary-btn" disabled={!savedRotatedCode} onClick={returnToLogin}>Volver a iniciar sesión</button>
        </> : forgotMethod==='choose' ? <>
          <div className="auth-title"><span className="eyebrow">RECUPERAR ACCESO</span><h2>¿Cómo quieres verificar tu cuenta?</h2><p>Elige uno de los métodos de seguridad asociados a tu cuenta.</p></div>
          <div className="recovery-methods">
            <button type="button" onClick={()=>{setForgotMethod('telegram');setError('')}}><strong>Telegram</strong><span>Recibe un código de 6 dígitos en el Telegram vinculado.</span></button>
            <button type="button" onClick={()=>{setForgotMethod('recovery');setError('')}}><strong>Código de recuperación</strong><span>Usa el código KAE que guardaste al proteger tu cuenta.</span></button>
          </div>
          <button type="button" className="text-btn" onClick={returnToLogin}>Volver al inicio de sesión</button>
        </> : forgotMethod==='telegram' ? <>
          <div className="auth-title"><span className="eyebrow">RECUPERACIÓN · TELEGRAM</span><h2>Verifica con Telegram</h2><p>Introduce el teléfono de la cuenta. Por privacidad, KAELEON no confirma si un número está registrado.</p></div>
          {!telegramRecoveryChallenge ? <div className="auth-form">{phoneField}{error && <div className="form-success recovery-neutral">{error}</div>}<button type="button" className="primary-btn" disabled={busy||phone.length<7} onClick={requestTelegramRecovery}>{busy?'Enviando…':'Enviar código por Telegram'}</button></div> : <form className="auth-form" onSubmit={confirmTelegramRecovery}>
            <label>Código de Telegram<input value={otp} onChange={e=>setOtp(e.target.value.replace(/\D/g,'').slice(0,6))} inputMode="numeric" autoComplete="one-time-code" placeholder="000000" required pattern="\d{6}"/></label>
            <label>Nueva contraseña<div className="password-field"><input type={showNewPassword?'text':'password'} value={newPassword} onChange={e=>setNewPassword(e.target.value)} minLength={8} required placeholder="Mínimo 8 caracteres"/><button type="button" className="password-eye" onClick={()=>setShowNewPassword(v=>!v)}><EyeIcon/></button></div></label>
            {error && <div className={error.startsWith('Si tu cuenta')?'form-success recovery-neutral':'form-error'}>{error}</div>}
            <button className="primary-btn" disabled={busy||otp.length!==6||newPassword.length<8}>{busy?'Verificando…':'Crear nueva contraseña'}</button>
            <button type="button" className="text-btn" onClick={()=>{setTelegramRecoveryChallenge('');setOtp('');setError('')}}>Solicitar otro código</button>
          </form>}
          <button type="button" className="text-btn" onClick={()=>{setForgotMethod('choose');setError('')}}>Usar otro método</button>
        </> : <>
          <div className="auth-title"><span className="eyebrow">RECUPERACIÓN · CÓDIGO KAE</span><h2>Usa tu código de recuperación</h2><p>Introduce el teléfono de la cuenta y el código que guardaste. El código quedará invalidado después del cambio.</p></div>
          <form className="auth-form" onSubmit={resetWithRecoveryCode}>
            {phoneField}
            <label>Código de recuperación<input className="recovery-input" value={recoveryCode} onChange={e=>setRecoveryCode(e.target.value.toUpperCase().replace(/[^A-Z0-9-]/g,'').slice(0,28))} placeholder="KAE-XXXX-XXXX-XXXX-XXXX-XXXX" autoCapitalize="characters" required minLength={20}/></label>
            <label>Nueva contraseña<div className="password-field"><input type={showNewPassword?'text':'password'} value={newPassword} onChange={e=>setNewPassword(e.target.value)} minLength={8} required placeholder="Mínimo 8 caracteres"/><button type="button" className="password-eye" onClick={()=>setShowNewPassword(v=>!v)}><EyeIcon/></button></div></label>
            {error && <div className="form-error">{error}</div>}
            <button className="primary-btn" disabled={busy||newPassword.length<8}>{busy?'Verificando…':'Restablecer contraseña'}</button>
          </form>
          <button type="button" className="text-btn" onClick={()=>{setForgotMethod('choose');setError('')}}>Usar otro método</button>
        </>}
      </div>}

      <div className="auth-footer">KAELEON · Acceso cifrado · Sesiones Bearer</div>
    </div></section>
  </main>
}
