import { useEffect, useMemo, useState } from 'react';
import Brand from '../components/Brand';
import { api, humanizeError } from '../lib/api';
import type { BillingPlan, Entitlement, PaymentOrder, User } from '../types';

const money=(n:unknown)=>`${Number(n||0).toFixed(2)} USDT`;
const date=(v:unknown)=>{if(!v)return '—';const d=new Date(String(v));return Number.isNaN(d.getTime())?'—':new Intl.DateTimeFormat('es-MX',{dateStyle:'medium',timeStyle:'short'}).format(d)};

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
      setOrders(ordersResult.value.items);
      const active=ordersResult.value.items.find(x=>['AWAITING_PAYMENT','VERIFYING','INVALID'].includes(String(x.status||'')));
      setOrder(active||null);
      if(active?.tx_hash)setTxHash(String(active.tx_hash));
    }else failures.push(`historial: ${humanizeError(ordersResult.reason)}`);

    if(failures.length)setError(failures.join(' · '));
  };
  useEffect(()=>{load()},[]);

  const activeOrder=order;
  const currentState=useMemo(()=>{
    if(entitlement?.live_allowed)return entitlement.live_expires_at?`LIVE activo hasta ${date(entitlement.live_expires_at)}`:'LIVE activo';
    return 'LIVE no activo';
  },[entitlement]);

  async function createOrder(code:string){
    setBusy(`plan-${code}`);setError('');setNotice('');setTxHash('');
    try{const o=await api.createBillingOrder(code);setOrder(o);setOrders(prev=>[o,...prev.filter(x=>x.payment_order_id!==o.payment_order_id)]);setNotice('Orden creada. Envía exactamente el importe indicado y luego pega el hash de la transacción.');}
    catch(e){setError(humanizeError(e))}finally{setBusy('')}
  }
  async function copy(value:string,label:string){try{await navigator.clipboard.writeText(value);setCopied(label);setTimeout(()=>setCopied(''),1500)}catch{setNotice(`${label}: ${value}`)}}
  async function verify(){
    if(!activeOrder?.payment_order_id||txHash.trim().length<20)return;
    setBusy('verify');setError('');setNotice('');
    try{
      await api.submitBillingTx(activeOrder.payment_order_id,txHash.trim());
      const r=await api.verifyBillingPayment(activeOrder.payment_order_id,txHash.trim());
      setNotice(`Pago confirmado. LIVE activo hasta ${date(r.live_expires_at)}.`);setOrder(null);setTxHash('');await load();
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
        {plans.map(plan=><article className="plan-card" key={plan.code}><span className="plan-badge">LIVE</span><h2>{plan.days} días</h2><strong className="plan-price">{money(plan.price_usdt)}</strong><p>Acceso completo al modo LIVE durante {plan.days} días.</p><button disabled={!!busy} onClick={()=>createOrder(plan.code)}>{busy===`plan-${plan.code}`?'Creando orden…':'Seleccionar plan'}</button></article>)}
      </section>
      {activeOrder&&<section className="payment-card"><div className="payment-head"><div><small>ORDEN DE PAGO</small><h2>{activeOrder.duration_days} días LIVE · {money(activeOrder.amount_usdt)}</h2></div><span>{String(activeOrder.status||'AWAITING_PAYMENT')}</span></div><div className="payment-grid"><div><small>Red</small><strong>{activeOrder.network==='BNB_SMART_CHAIN'?'BNB Smart Chain (BEP-20)':activeOrder.network}</strong></div><div><small>Importe exacto</small><strong>{money(activeOrder.amount_usdt)}</strong></div><div className="payment-wallet"><small>Wallet receptora</small><code>{activeOrder.destination_wallet}</code><button onClick={()=>copy(String(activeOrder.destination_wallet||''),'Wallet')}>{copied==='Wallet'?'Copiada ✓':'Copiar'}</button></div><div><small>La orden vence</small><strong>{date(activeOrder.expires_at)}</strong></div></div><div className="payment-warning"><b>Importante:</b> envía exactamente el importe indicado en USDT usando BNB Smart Chain (BEP-20). No envíes por otra red.</div><label className="tx-field">Hash de la transacción<input value={txHash} onChange={e=>setTxHash(e.target.value)} placeholder="0x..." autoCapitalize="off" autoCorrect="off"/><small>Pega el Tx Hash después de realizar el pago.</small></label><button className="verify-payment-btn" onClick={verify} disabled={!!busy||txHash.trim().length<20}>{busy==='verify'?'Verificando en blockchain…':'Verificar pago'}</button></section>}
      <section className="payment-history"><div className="section-heading"><h2>Historial de pagos</h2><button onClick={load}>Actualizar</button></div>{orders.length?<div className="history-list">{orders.map(o=><div key={o.payment_order_id}><span><b>{o.duration_days} días</b><small>{date(o.created_at)}</small></span><strong>{money(o.amount_usdt)}</strong><em className={`payment-state ${String(o.status||'').toLowerCase()}`}>{String(o.status||'—')}</em></div>)}</div>:<p className="empty-copy">Todavía no tienes órdenes de pago.</p>}</section>
    </main>
  </div>
}
