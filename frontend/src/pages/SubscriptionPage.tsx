import { useEffect, useMemo, useState } from 'react';
import Brand from '../components/Brand';
import { api, humanizeError } from '../lib/api';
import type { BillingPlan, Entitlement, PaymentOrder, User } from '../types';

const money=(n:unknown)=>`${Number(n||0).toFixed(2)} USDT`;
const date=(v:unknown)=>{if(!v)return '—';const d=new Date(String(v));return Number.isNaN(d.getTime())?'—':new Intl.DateTimeFormat('es-MX',{dateStyle:'medium',timeStyle:'short'}).format(d)};
const TX_HASH_RE=/^0x[0-9a-fA-F]{64}$/;
const ACTIVE_STATUSES=new Set(['AWAITING_PAYMENT','VERIFYING','INVALID']);

function paymentReason(reason?:string|null){
  if(!reason)return '';
  const map:Record<string,string>={
    tx_not_confirmed:'La transacción todavía no está confirmada. Puedes volver a verificar sin crear otra orden.',
    payment_verifier_not_configured:'El verificador BSC del servidor no está configurado. La orden se conserva para reintentar.',
    wrong_network:'El nodo configurado no corresponde a BNB Smart Chain.',
    amount_or_destination_mismatch:'El hash existe, pero el importe, token o wallet receptora no coinciden con esta orden.',
    bsc_rpc_temporarily_unavailable:'El nodo BNB Smart Chain no respondió. La orden sigue activa para reintentar.',
    bsc_verification_error:'No se pudo completar la lectura blockchain. La orden sigue activa para reintentar.',
    tx_reverted:'La transacción fue revertida en blockchain.',
    tx_before_order:'Ese hash corresponde a una transferencia anterior a esta orden y no puede reutilizarse.',
    invalid_tx_hash:'El Tx Hash guardado no tiene el formato completo esperado.',
  };
  return map[reason]||reason.replaceAll('_',' ');
}

export default function SubscriptionPage({user,onBack}:{user:User;onBack:()=>void}){
  const [plans,setPlans]=useState<BillingPlan[]>([]);
  const [entitlement,setEntitlement]=useState<Entitlement|null>(null);
  const [orders,setOrders]=useState<PaymentOrder[]>([]);
  const [order,setOrder]=useState<PaymentOrder|null>(null);
  const [txHash,setTxHash]=useState('');
  const [busy,setBusy]=useState('');
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [copied,setCopied]=useState('');

  const load=async()=>{
    setError('');
    const results=await Promise.allSettled([api.billingPlans(),api.entitlement(),api.billingOrders()]);
    const failures:string[]=[];

    const plansResult=results[0];
    if(plansResult.status==='fulfilled')setPlans(plansResult.value.plans);
    else failures.push(`planes: ${humanizeError(plansResult.reason)}`);

    const entitlementResult=results[1];
    if(entitlementResult.status==='fulfilled')setEntitlement(entitlementResult.value);
    else failures.push(`estado LIVE: ${humanizeError(entitlementResult.reason)}`);

    const ordersResult=results[2];
    if(ordersResult.status==='fulfilled'){
      const items=ordersResult.value.items;
      setOrders(items);
      const active=items.find(x=>ACTIVE_STATUSES.has(String(x.status||'')))||null;
      setOrder(active);
      setTxHash(active?.tx_hash?String(active.tx_hash):'');
    }else failures.push(`historial: ${humanizeError(ordersResult.reason)}`);

    if(failures.length)setError(failures.join(' · '));
  };
  useEffect(()=>{load()},[]);

  const activeOrder=order;
  const hashIsValid=TX_HASH_RE.test(txHash.trim());
  const currentState=useMemo(()=>{
    if(entitlement?.live_allowed)return entitlement.live_expires_at?`LIVE activo hasta ${date(entitlement.live_expires_at)}`:'LIVE activo';
    return 'LIVE no activo';
  },[entitlement]);

  async function createOrder(code:string){
    if(activeOrder){
      setNotice('Ya tienes una orden de pago activa. Usa esa misma orden; KAELEON no generará otra mientras siga vigente.');
      document.querySelector('.payment-card')?.scrollIntoView({behavior:'smooth',block:'start'});
      return;
    }
    setBusy(`plan-${code}`);setError('');setNotice('');setTxHash('');
    try{
      const o=await api.createBillingOrder(code);
      setOrder(o);
      setOrders(prev=>[o,...prev.filter(x=>x.payment_order_id!==o.payment_order_id)]);
      setTxHash(o.tx_hash?String(o.tx_hash):'');
      setNotice(o.reused_existing?'Ya existía una orden activa; se recuperó la misma orden sin crear un duplicado.':'Orden creada. Envía exactamente el importe indicado y luego pega el hash completo de la transacción.');
    }catch(e){setError(humanizeError(e))}finally{setBusy('')}
  }

  async function copy(value:string,label:string){
    try{await navigator.clipboard.writeText(value);setCopied(label);setTimeout(()=>setCopied(''),1500)}
    catch{setNotice(`${label}: ${value}`)}
  }

  async function verify(){
    if(!activeOrder?.payment_order_id)return;
    const hash=txHash.trim();
    if(!TX_HASH_RE.test(hash)){
      setError('El Tx Hash debe ser completo: 0x seguido de 64 caracteres hexadecimales (66 caracteres en total).');
      return;
    }
    setBusy('verify');setError('');setNotice('');
    try{
      const submitted=await api.submitBillingTx(activeOrder.payment_order_id,hash);
      setOrder(submitted);
      const r=await api.verifyBillingPayment(activeOrder.payment_order_id,hash);
      if(r.confirmed){
        setNotice(`Pago confirmado. LIVE activo hasta ${date(r.live_expires_at)}.`);
        setOrder(null);setTxHash('');await load();
      }else{
        setNotice(paymentReason(r.reason)||'La transacción está pendiente de confirmación. Vuelve a verificar en unos segundos.');
        await load();
      }
    }catch(e){
      setError(humanizeError(e));
      await load();
    }finally{setBusy('')}
  }

  return <div className="subscription-shell">
    <header className="subscription-topbar"><Brand/><div><span>{user.phone}</span><button onClick={onBack}>Volver al dashboard</button></div></header>
    <main className="subscription-main">
      <section className="subscription-hero"><small>KAELEON LIVE</small><h1>Planes y suscripción</h1><p>Activa o extiende el acceso LIVE pagando USDT por BNB Smart Chain. DEMO continúa disponible independientemente de tu suscripción LIVE.</p><div className={`subscription-status ${entitlement?.live_allowed?'active':''}`}><span>Estado</span><strong>{currentState}</strong></div></section>
      {error&&<div className="form-error">{error}</div>}{notice&&<div className="form-success">{notice}</div>}
      <section className="plans-grid">
        {plans.map(plan=><article className="plan-card" key={plan.code}><span className="plan-badge">LIVE</span><h2>{plan.days} días</h2><strong className="plan-price">{money(plan.price_usdt)}</strong><p>Acceso completo al modo LIVE durante {plan.days} días.</p><button disabled={!!busy||!!activeOrder} onClick={()=>createOrder(plan.code)}>{activeOrder?'Orden activa':busy===`plan-${plan.code}`?'Creando orden…':'Seleccionar plan'}</button></article>)}
      </section>
      {activeOrder&&<section className="payment-card"><div className="payment-head"><div><small>ORDEN DE PAGO</small><h2>{activeOrder.duration_days} días LIVE · {money(activeOrder.amount_usdt)}</h2></div><span>{String(activeOrder.status||'AWAITING_PAYMENT')}</span></div><div className="payment-grid"><div><small>Red</small><strong>{activeOrder.network==='BNB_SMART_CHAIN'?'BNB Smart Chain (BEP-20)':activeOrder.network}</strong></div><div><small>Importe exacto</small><strong>{money(activeOrder.amount_usdt)}</strong></div><div className="payment-wallet"><small>Wallet receptora</small><code>{activeOrder.destination_wallet}</code><button onClick={()=>copy(String(activeOrder.destination_wallet||''),'Wallet')}>{copied==='Wallet'?'Copiada ✓':'Copiar'}</button></div><div><small>La orden vence</small><strong>{date(activeOrder.expires_at)}</strong></div></div><div className="payment-warning"><b>Importante:</b> envía exactamente el importe indicado en USDT usando BNB Smart Chain (BEP-20). No envíes por otra red.</div>{activeOrder.verification_reason&&<div className={activeOrder.status==='INVALID'?'form-error':'form-success'}>{paymentReason(activeOrder.verification_reason)}</div>}<label className="tx-field">Hash de la transacción<input value={txHash} onChange={e=>setTxHash(e.target.value.trim())} placeholder="0x + 64 caracteres hexadecimales" autoCapitalize="off" autoCorrect="off" spellCheck={false}/><small>{hashIsValid?'Hash completo ✓':`Hash completo requerido · ${txHash.trim().length}/66 caracteres`}</small></label><button className="verify-payment-btn" onClick={verify} disabled={!!busy||!hashIsValid}>{busy==='verify'?'Verificando en blockchain…':'Verificar pago'}</button></section>}
      <section className="payment-history"><div className="section-heading"><h2>Historial de pagos</h2><button onClick={load}>Actualizar</button></div>{orders.length?<div className="history-list">{orders.map(o=><div key={o.payment_order_id}><span><b>{o.duration_days} días</b><small>{date(o.created_at)}{o.verification_reason?` · ${paymentReason(o.verification_reason)}`:''}</small></span><strong>{money(o.amount_usdt)}</strong><em className={`payment-state ${String(o.status||'').toLowerCase()}`}>{String(o.status||'—')}</em></div>)}</div>:<p className="empty-copy">Todavía no tienes órdenes de pago.</p>}</section>
    </main>
  </div>
}
