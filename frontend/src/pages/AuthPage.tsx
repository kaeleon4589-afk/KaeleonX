import { FormEvent, useMemo, useState } from 'react';
import Brand from '../components/Brand';
import { EyeIcon } from '../components/Icons';
import { api, humanizeError, session } from '../lib/api';
import { countries, flagEmoji } from '../lib/countries';
import type { User } from '../types';

export default function AuthPage({onAuthenticated}:{onAuthenticated:(user:User)=>void}) {
  const [mode,setMode]=useState<'login'|'register'|'verify'>('login');
  const [phone,setPhone]=useState(''); const [country,setCountry]=useState('+52'); const [password,setPassword]=useState(''); const [showPassword,setShowPassword]=useState(false); const [referral,setReferral]=useState('');
  const [challenge,setChallenge]=useState(''); const [telegramUrl,setTelegramUrl]=useState(''); const [error,setError]=useState(''); const [busy,setBusy]=useState(false);
  const countryValue=useMemo(()=>countries.find(c=>c.dial===country)?.iso||'MX',[country]);

  async function submit(e:FormEvent){e.preventDefault();setError('');setBusy(true);try{
    if(mode==='login'){
      const res=await api.login(phone,password,country); session.set(res.access_token); const me=await api.me(); onAuthenticated(me); return;
    }
    if(mode==='register'){
      const res=await api.register({phone,password,country_code:country,referral_code:referral}); setChallenge(res.challenge); setTelegramUrl(res.telegram_verification_url); setMode('verify'); return;
    }
    await api.verify(challenge); setMode('login'); setError('Verificación completada. Ya puedes iniciar sesión.');
  }catch(err){setError(humanizeError(err));}finally{setBusy(false)}}

  return <main className="auth-shell">
    <div className="auth-glow auth-glow-one"/><div className="auth-glow auth-glow-two"/>
    <section className="auth-side"><Brand/><div className="auth-copy"><span className="eyebrow">PLATAFORMA KAELEON</span><h1>Disciplina, estrategia y ejecución en un solo lugar.</h1><p>Conecta tu cuenta, define tu capital y deja que el motor gestione la ejecución respetando la lógica real del backend.</p></div><div className="auth-status"><i/> API segura · CoinW · Telegram</div></section>
    <section className="auth-panel"><div className="auth-card">
      <div className="auth-mobile-brand"><Brand/></div>
      {mode!=='verify' ? <>
        <div className="auth-tabs"><button className={mode==='login'?'active':''} onClick={()=>{setMode('login');setError('')}}>Iniciar sesión</button><button className={mode==='register'?'active':''} onClick={()=>{setMode('register');setError('')}}>Crear cuenta</button></div>
        <div className="auth-title"><h2>{mode==='login'?'Bienvenido de nuevo':'Crea tu cuenta'}</h2><p>{mode==='login'?'Accede a tu panel de ejecución.':'El teléfono debe coincidir con el que compartirás con Telegram.'}</p></div>
        <form onSubmit={submit} className="auth-form">
          <label>Teléfono<div className="phone-row"><select className="country country-select" value={countryValue} onChange={e=>{const c=countries.find(x=>x.iso===e.target.value); if(c)setCountry(c.dial)}} aria-label="País">{countries.map(c=><option key={`${c.iso}-${c.dial}`} value={c.iso}>{flagEmoji(c.iso)} {c.name} {c.dial}</option>)}</select><input value={phone} onChange={e=>setPhone(e.target.value.replace(/[^0-9\s-]/g,''))} placeholder="59494299" inputMode="tel" required minLength={7}/></div></label>
          <label>Contraseña<div className="password-field"><input type={showPassword?'text':'password'} value={password} onChange={e=>setPassword(e.target.value)} placeholder="Mínimo 8 caracteres" required minLength={8}/><button type="button" className="password-eye" onClick={()=>setShowPassword(v=>!v)} aria-label={showPassword?'Ocultar contraseña':'Mostrar contraseña'} aria-pressed={showPassword}><EyeIcon/></button></div></label>
          {mode==='register' && <label>Código de referido <span>(opcional)</span><input value={referral} onChange={e=>setReferral(e.target.value)} placeholder="KAELEON-..."/></label>}
          {error && <div className={error.startsWith('Verificación')?'form-success':'form-error'}>{error}</div>}
          <button className="primary-btn auth-submit" disabled={busy}>{busy?'Procesando…':mode==='login'?'Entrar a KAELEON':'Continuar con Telegram'}</button>
        </form>
      </> : <div className="verify-card"><span className="verify-icon">✓</span><h2>Verifica tu teléfono</h2><p>Abre el bot de Telegram y comparte el mismo número con el que acabas de registrarte.</p>{telegramUrl && <a className="primary-btn telegram-btn" href={telegramUrl} target="_blank" rel="noreferrer">Abrir Telegram</a>}<button className="secondary-btn" disabled={busy} onClick={(e)=>submit(e as unknown as FormEvent)}>{busy?'Comprobando…':'Ya verifiqué mi número'}</button>{error && <div className="form-error">{error}</div>}<button className="text-btn" onClick={()=>setMode('register')}>Volver</button></div>}
      <div className="auth-footer">KAELEON · Acceso cifrado · Sesiones Bearer</div>
    </div></section>
  </main>
}
