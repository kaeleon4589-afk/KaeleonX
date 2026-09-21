import { useCallback, useEffect, useMemo, useState } from 'react';
import Brand from '../components/Brand';
import { api, humanizeError } from '../lib/api';
import type { ActivityEvent, User } from '../types';

const EVENT_LABELS: Record<string,string> = {
  REGIME_STATE:'Régimen actual', STRATEGY_STATE:'Estrategia actual',
  POSITION_OPENED:'Operación abierta', POSITION_CLOSED:'Operación cerrada',
};

function when(v:unknown){ if(!v)return '—'; const d=new Date(String(v)); return Number.isNaN(d.getTime())?'—':new Intl.DateTimeFormat('es-MX',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(d); }
function tone(e:string){ if(e==='POSITION_CLOSED')return 'success'; if(e==='POSITION_OPENED')return 'success'; if(e.includes('REGIME')||e.includes('STRATEGY'))return 'info'; return 'neutral'; }
function compact(event:ActivityEvent){
  const keys=['regime','candidate','confidence','strategy','direction','entry_price','stop_price','target_price','exit_price','realized_pnl','quantity','status'];
  return keys.filter(k=>event[k]!==undefined&&event[k]!==null&&event[k]!=='').map(k=>`${k}: ${typeof event[k]==='number'?Number(event[k]).toFixed(4):String(event[k])}`).join(' · ');
}

export default function ActivityPage({user,onBack}:{user:User;onBack:()=>void}){
  const [items,setItems]=useState<ActivityEvent[]>([]); const [loading,setLoading]=useState(true); const [error,setError]=useState(''); const [paused,setPaused]=useState(false);
  const load=useCallback(async()=>{try{const r=await api.activity(25);setItems(r.items);setError('')}catch(e){setError(humanizeError(e))}finally{setLoading(false)}},[]);
  useEffect(()=>{load(); if(paused)return; const id=window.setInterval(load,15000); return()=>window.clearInterval(id)},[load,paused]);
  const lastRegime=useMemo(()=>items.find(x=>x.event==='REGIME_STATE'),[items]);
  const lastStrategy=useMemo(()=>items.find(x=>x.event==='STRATEGY_STATE'),[items]);
  const recentTrades=useMemo(()=>items.filter(x=>x.event==='POSITION_OPENED'||x.event==='POSITION_CLOSED'),[items]);
  return <div className="activity-shell">
    <header className="activity-topbar"><Brand/><div><span>{user.phone||user.user_id}</span><button onClick={onBack}>← Dashboard</button></div></header>
    <main className="activity-main">
      <div className="activity-heading"><div><small>MONITOR LIGERO</small><h1>Estado del motor</h1><p>Solo régimen, estrategia y operaciones. Sin guardar logs operativos en MongoDB.</p></div><div className="activity-controls"><button onClick={()=>setPaused(v=>!v)}>{paused?'▶ Reanudar':'Ⅱ Pausar'}</button><button onClick={load}>↻ Actualizar</button></div></div>
      {error&&<div className="activity-error">{error}</div>}
      <section className="activity-summary">
        <div><span>Régimen actual</span><strong>{String(lastRegime?.regime||lastRegime?.candidate||'—')}</strong><small>{lastRegime?.symbol||'—'}</small></div>
        <div><span>Estrategia</span><strong>{String(lastStrategy?.strategy||'—')}</strong><small>{lastStrategy?.symbol||'—'}</small></div>
        <div><span>Modo</span><strong>{String(lastRegime?.mode||lastStrategy?.mode||'—').toUpperCase()}</strong><small>DEMO / LIVE separados</small></div>
        <div><span>Operaciones recientes</span><strong>{recentTrades.length}</strong><small>Datos ya persistidos como trading</small></div>
      </section>
      <section className="activity-panel">
        <div className="activity-filterbar"><strong>Estado y operaciones</strong><span>{paused?'Pausado':'● ACTUALIZACIÓN 15 s'}</span></div>
        {loading?<div className="activity-empty">Cargando estado…</div>:items.length===0?<div className="activity-empty">Todavía no hay estado operativo disponible.</div>:<div className="activity-list">{items.map((ev,i)=><article className={`activity-event ${tone(ev.event)}`} key={`${ev.position_id||ev.event}-${ev.updated_at||ev.opened_at||ev.closed_at||i}-${i}`}>
          <div className="activity-event-time"><span>{when(ev.updated_at||ev.closed_at||ev.opened_at||ev.created_at)}</span><b>{ev.mode?.toUpperCase()||''}</b></div>
          <div className="activity-event-main"><div><strong>{EVENT_LABELS[ev.event]||ev.event}</strong>{ev.symbol&&<em>{ev.symbol}</em>}</div><p>{compact(ev)||'Estado actual del motor.'}</p></div>
        </article>)}</div>}
      </section>
    </main>
  </div>
}
