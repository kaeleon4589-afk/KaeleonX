import { useCallback, useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react';
import Brand from '../components/Brand';
import Sparkline from '../components/Sparkline';
import StatCard from '../components/StatCard';
import { BellIcon, BoltIcon, ChartIcon, CheckIcon, GearIcon, HomeIcon, PauseIcon, PlayIcon, RefreshIcon, ShieldIcon, SwapIcon, UserIcon, WalletIcon } from '../components/Icons';
import { api, humanizeError, session } from '../lib/api';
import type { Entitlement, Execution, Operations, Performance, Position, TradingConfig, User } from '../types';

const money=(n:unknown)=>new Intl.NumberFormat('en-US',{style:'currency',currency:'USD',minimumFractionDigits:2,maximumFractionDigits:2}).format(Number(n)||0);
const pct=(n:unknown)=>`${Number(n||0).toFixed(2)}%`;
const num=(v:unknown)=>{const n=Number(v);return Number.isFinite(n)?n:0};
const date=(v:unknown)=>{if(!v)return '—'; const n=typeof v==='number'&&v<1e12?v*1000:v; const d=new Date(n as string|number); return Number.isNaN(d.getTime())?'—':new Intl.DateTimeFormat('es-MX',{day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit'}).format(d)};
const side=(p:Position)=>String(p.side||p.direction||'—').toUpperCase();
const symbol=(p:Position)=>String(p.symbol||'—').replace(/[_-]/g,'/');
const pnl=(p:Position)=>num(p.realized_pnl)-num(p.entry_fee)-num(p.exit_fee)+num(p.funding_pnl);

export default function DashboardPage({user,onSignedOut}:{user:User;onSignedOut:()=>void}){
  const [config,setConfig]=useState<TradingConfig|null>(null); const [exec,setExec]=useState<Execution|null>(null); const [perf,setPerf]=useState<Performance|null>(null); const [ops,setOps]=useState<Operations>({open:[],closed:[]}); const [ent,setEnt]=useState<Entitlement|null>(null);
  const [loading,setLoading]=useState(true); const [error,setError]=useState(''); const [notice,setNotice]=useState(''); const [busy,setBusy]=useState(''); const [drawer,setDrawer]=useState(false);
  const [capital,setCapital]=useState(''); const [apiKey,setApiKey]=useState(''); const [apiSecret,setApiSecret]=useState('');

  const load=useCallback(async(silent=false)=>{
    if(!silent)setLoading(true);
    setError('');
    const requests = [
      ['config', api.config()],
      ['execution', api.execution()],
      ['performance', api.performance()],
      ['operations', api.operations()],
      ['entitlement', api.entitlement()],
    ] as const;
    const results = await Promise.allSettled(requests.map(([,promise])=>promise));
    const failures:string[]=[];
    results.forEach((result,index)=>{
      const name=requests[index][0];
      if(result.status==='rejected'){failures.push(`${name}: ${humanizeError(result.reason)}`);return;}
      if(name==='config'){const c=result.value as TradingConfig;setConfig(c);setCapital(String(c.operating_capital));}
      else if(name==='execution')setExec(result.value as Execution);
      else if(name==='performance')setPerf(result.value as Performance);
      else if(name==='operations')setOps(result.value as Operations);
      else if(name==='entitlement')setEnt(result.value as Entitlement);
    });
    if(failures.length)setError(failures.join(' · '));
    if(!session.get())onSignedOut();
    setLoading(false);
  },[onSignedOut]);
  useEffect(()=>{load(); const id=window.setInterval(()=>load(true),15000); return()=>clearInterval(id)},[load]);

  const equity=useMemo(()=>{let cur=config?.operating_capital||0;const values=[cur];[...ops.closed].reverse().forEach(p=>{cur+=pnl(p);values.push(cur)});return values},[ops.closed,config?.operating_capital]);
  const totalPositionCount=ops.open.length; const mode=config?.execution_mode||'demo'; const enabled=Boolean(config?.trading_enabled);

  async function save(partial:Partial<{execution_mode:'demo'|'live';trading_enabled:boolean;operating_capital:number;coinw_api_key:string;coinw_api_secret:string}>, action='save'){
    if(!config)return; setBusy(action);setError('');setNotice('');try{await api.saveConfig({execution_mode:partial.execution_mode??config.execution_mode,trading_enabled:partial.trading_enabled??config.trading_enabled,operating_capital:partial.operating_capital??config.operating_capital,...(partial.coinw_api_key?{coinw_api_key:partial.coinw_api_key,coinw_api_secret:partial.coinw_api_secret}: {})});setNotice('Configuración actualizada correctamente.');setApiKey('');setApiSecret('');await load(true);}catch(err){setError(humanizeError(err));}finally{setBusy('')}}
  async function testCoinW(){setBusy('coinw-test');setError('');try{const r=await api.testCoinW();setNotice(`CoinW conectado. Equity disponible: ${money(r.available_equity)}`);await load(true)}catch(err){setError(humanizeError(err))}finally{setBusy('')}}
  async function disconnect(){setBusy('disconnect');setError('');try{await api.deleteCredentials();setNotice('Credenciales CoinW eliminadas.');await load(true)}catch(err){setError(humanizeError(err))}finally{setBusy('')}}
  async function activateTrial(){setBusy('trial');setError('');try{await api.activateTrial();setNotice('Prueba LIVE activada.');await load(true)}catch(err){setError(humanizeError(err))}finally{setBusy('')}}
  async function logout(){try{await api.logout()}catch{}session.clear();onSignedOut()}

  if(loading&&!config)return <div className="app-loader"><div className="loader-ring"/><strong>KAELEON</strong><span>Cargando tu panel…</span></div>;

  return <div className="dashboard-shell">
    <aside className={`sidebar ${drawer?'open':''}`}><div className="sidebar-brand"><Brand/></div><nav><a href="#inicio"><HomeIcon/>Dashboard</a><a className="active" href="#ejecucion"><BoltIcon/>Ejecución</a><a href="#operaciones"><SwapIcon/>Operaciones</a><a href="#rendimiento"><ChartIcon/>Rendimiento</a><a href="#configuracion"><GearIcon/>Configuración</a></nav><div className="side-promo"><div className="lizard-orb">K</div><h3>KAELEON</h3><strong>Tu aliado en el trading</strong><p>Tecnología, disciplina y gestión de riesgo conectadas a tu cuenta.</p><span><i/>Trading {enabled?'Activo':'Pausado'}</span></div></aside>
    {drawer&&<button className="drawer-backdrop" onClick={()=>setDrawer(false)} aria-label="Cerrar menú"/>}
    <div className="main-column">
      <header className="topbar"><button className="mobile-menu" onClick={()=>setDrawer(true)}>☰</button><div className="market-strip"><MarketChip icon="₿" label="BTC/USDT" status="KAELEON AUTO"/><MarketChip icon="Ξ" label="ETH/USDT" status="KAELEON AUTO"/><MarketChip icon="S" label="SOL/USDT" status="KAELEON AUTO"/><MarketChip icon="◆" label="Mercados" status={`${exec?.markets_scanned||0} escaneados`}/></div><div className="top-actions"><button><BellIcon/><i/></button><div className="avatar"><UserIcon/></div><div className="welcome"><span>Bienvenido</span><strong>{user.phone||'Usuario KAELEON'}</strong></div><button className="logout-mini" onClick={logout}>Salir</button></div></header>
      <main className="content" id="ejecucion">
        <section className="page-heading"><div className="heading-icon"><BoltIcon/></div><div><h1>Ejecución</h1><p>Configura tu cuenta, activa el trading y deja que KAELEON gestione la ejecución.</p></div><button className="refresh-btn" onClick={()=>load()}><RefreshIcon/>Actualizar</button></section>
        {error&&<div className="banner banner-error">{error}</div>}{notice&&<div className="banner banner-success">{notice}</div>}
        <section className="config-grid" id="configuracion">
          <article className="panel coinw-panel"><PanelTitle icon={<WalletIcon/>} title="Cuenta CoinW" badge={config?.coinw_configured?'Conectada':'Sin conectar'} badgeTone={config?.coinw_configured?'green':'gray'}/><div className="coinw-logo"><span>CW</span><strong>CoinW</strong></div><label>API Key<input value={apiKey} onChange={e=>setApiKey(e.target.value)} placeholder={config?.coinw_api_key||'Pega tu API Key'}/></label><label>API Secret<input type="password" value={apiSecret} onChange={e=>setApiSecret(e.target.value)} placeholder={config?.coinw_configured?'••••••••••••••••':'Pega tu API Secret'}/></label><button className="verify-btn" disabled={!config?.coinw_configured||busy==='coinw-test'} onClick={testCoinW}><CheckIcon/>{busy==='coinw-test'?'Comprobando…':'Verificar conexión'}</button><div className="coinw-actions">{apiKey&&apiSecret&&<button className="secondary-btn" disabled={!!busy} onClick={()=>save({coinw_api_key:apiKey,coinw_api_secret:apiSecret},'credentials')}>Guardar credenciales</button>}{config?.coinw_configured&&<button className="danger-outline" disabled={!!busy||mode==='live'} onClick={disconnect}>Desconectar</button>}</div></article>
          <article className="panel capital-panel"><PanelTitle icon={<ShieldIcon/>} title="Capital operativo"/><div className="capital-summary"><div><span>Capital configurado</span><strong>{money(config?.operating_capital)}</strong></div><div className="capital-ring"><span>{perf?.available_equity?Math.min(100,Math.round((config?.operating_capital||0)/Math.max(perf.available_equity,1)*100)):0}%</span><small>equity</small></div><div><span>Disponible</span><strong className="green">{money(perf?.available_equity)}</strong></div></div><label>Capital para KAELEON<div className="capital-input"><span>$</span><input type="number" min={config?.minimum_operating_capital||3} step="0.01" value={capital} onChange={e=>setCapital(e.target.value)}/><button onClick={()=>save({operating_capital:Number(capital)},'capital')} disabled={!!busy||!capital}>Guardar</button></div></label><div className="info-box"><strong>Gestión de riesgo interna</strong><p>KAELEON usa el capital configurado y limita el riesgo según el equity disponible. El apalancamiento lo gestiona internamente el motor.</p></div><div className="backend-rules"><span>Mercados <b>Automático</b></span><span>Timeframes <b>Internos</b></span><span>Leverage <b>Interno</b></span></div></article>
          <article className="panel trading-panel"><PanelTitle icon={<BoltIcon/>} title="Modo de trading"/><div className="mode-picker"><button className={mode==='demo'?'selected':''} onClick={()=>save({execution_mode:'demo',trading_enabled:false},'mode')}><ShieldIcon/><strong>Paper Trading</strong><span>Práctica · Sin riesgo</span></button><button className={`${mode==='live'?'selected live':''} ${!config?.live_allowed?'locked':''}`} onClick={()=>config?.live_allowed&&save({execution_mode:'live',trading_enabled:false},'mode')}><span className="lock">{config?.live_allowed?'●':'🔒'}</span><strong>Live Trading</strong><span>{config?.live_allowed?'Disponible':'Requiere acceso LIVE'}</span></button></div>{!config?.live_allowed&&<button className="trial-btn" disabled={!!busy} onClick={activateTrial}>{busy==='trial'?'Activando…':'Activar prueba LIVE'}</button>}<button className="start-btn" disabled={!!busy||enabled} onClick={()=>save({trading_enabled:true},'start')}><PlayIcon/>Activar Trading</button><button className="pause-btn" disabled={!!busy||!enabled} onClick={()=>save({trading_enabled:false},'pause')}><PauseIcon/>Pausar Trading</button><div className="warning-box">El motor deja de abrir nuevas entradas al pausar. Las posiciones LIVE abiertas siguen bajo reconciliación y protección del backend.</div></article>
        </section>
        <section className={`engine-bar ${enabled?'active':''}`}><div className="engine-title"><span className="engine-icon">◉</span><div><small>Estado del motor</small><strong>{exec?.status||'PAUSADO'}</strong></div></div><EngineMetric label="Modo" value={(exec?.mode||mode).toUpperCase()}/><EngineMetric label="CoinW" value={exec?.coinw_connected?'CONECTADO':config?.coinw_configured?'CONFIGURADO':'NO CONFIGURADO'}/><EngineMetric label="Datos" value={enabled?'EN VIVO':'EN ESPERA'}/><EngineMetric label="Capital" value={money(exec?.configured_capital)}/><EngineMetric label="Posiciones" value={String(totalPositionCount)}/><div className="engine-spark"><Sparkline values={equity}/></div></section>
        <section className="analytics-grid" id="rendimiento">
          <article className="panel chart-panel"><div className="chart-header"><div><span className="asset-badge">K</span><strong>Curva de capital</strong><em>{money(perf?.current_capital)}</em><b className={(perf?.pnl||0)>=0?'green':'red'}>{(perf?.pnl||0)>=0?'+':''}{pct(perf?.pnl_pct)}</b></div><div className="chart-note">Datos reales de operaciones cerradas</div></div><div className="big-chart"><div className="chart-y"><span>{money(Math.max(...equity,1))}</span><span>{money((Math.max(...equity,1)+Math.min(...equity,0))/2)}</span><span>{money(Math.min(...equity,0))}</span></div><Sparkline values={equity}/><div className="chart-empty-overlay">{ops.closed.length===0&&<span>La curva aparecerá cuando existan operaciones cerradas.</span>}</div></div><div className="chart-legend"><span><i/>Capital</span><span>Fuente: /user/performance + /user/operations</span></div></article>
          <article className="panel performance-panel"><div className="panel-heading-row"><h3>Rendimiento General</h3><span>Total</span></div><div className={`hero-pnl ${(perf?.pnl||0)>=0?'green':'red'}`}>{(perf?.pnl||0)>=0?'+':''}{pct(perf?.pnl_pct)}<small>PnL acumulado <b>{money(perf?.pnl)}</b></small></div><Sparkline values={equity}/><div className="capital-boxes"><StatCard label="Capital inicial" value={money(perf?.capital)}/><StatCard label="Capital actual" value={money(perf?.current_capital)}/></div><div className="metric-grid"><StatCard label="Operaciones" value={perf?.trades||0}/><StatCard label="Win rate" value={pct(perf?.win_rate)} tone="green"/><StatCard label="Profit factor" value={(perf?.profit_factor||0).toFixed(2)}/><StatCard label="Drawdown máx." value={`-${pct(perf?.drawdown)}`} tone="red"/></div></article>
        </section>
        <section className="bottom-grid" id="operaciones"><article className="panel active-ops"><div className="panel-heading-row"><h3>Operaciones Activas</h3><span className="green">{ops.open.length} {ops.open.length===1?'operación':'operaciones'}</span></div>{ops.open.length?ops.open.slice(0,3).map(p=><OpenPosition key={String(p.position_id||Math.random())} p={p}/>):<Empty text="No hay posiciones abiertas en este momento."/>}</article><article className="panel recent-ops"><div className="panel-heading-row"><h3>Últimas Operaciones</h3><span>{ops.closed.length} cerradas</span></div>{ops.closed.length?<div className="ops-table"><div className="ops-row ops-head"><span>Par</span><span>Tipo</span><span>Resultado</span><span>PnL</span><span>Fecha</span></div>{ops.closed.slice(0,6).map(p=>{const v=pnl(p);return <div className="ops-row" key={String(p.position_id||Math.random())}><strong>{symbol(p)}</strong><b className={side(p)==='LONG'?'green':'red'}>{side(p)}</b><span className={v>=0?'green':'red'}>{v>=0?'● WIN':'● LOSS'}</span><strong className={v>=0?'green':'red'}>{money(v)}</strong><span>{date(p.closed_at||p.created_at)}</span></div>})}</div>:<Empty text="Las operaciones cerradas aparecerán aquí."/>}</article><article className="panel distribution"><div className="panel-heading-row"><h3>Resumen del motor</h3><span>{ent?.live_state||'demo'}</span></div><div className="donut" style={{'--pct':`${Math.max(0,Math.min(100,perf?.win_rate||0))*3.6}deg`} as CSSProperties}><div><small>Win rate</small><strong>{pct(perf?.win_rate)}</strong></div></div><div className="summary-list"><span><i className="green-dot"/>Modo<b>{mode.toUpperCase()}</b></span><span><i/>Mercados<b>Auto</b></span><span><i/>Timeframes<b>Internos</b></span><span><i/>Acceso LIVE<b>{config?.live_allowed?'Sí':'No'}</b></span></div></article></section>
      </main><footer>DISCIPLINA <b>·</b> ESTRATEGIA <b>·</b> RESULTADOS <Brand compact/></footer>
    </div>
  </div>
}

function MarketChip({icon,label,status}:{icon:string;label:string;status:string}){return <div className="market-chip"><span>{icon}</span><div><strong>{label}</strong><small>{status}</small></div></div>}
function PanelTitle({icon,title,badge,badgeTone}:{icon:ReactNode;title:string;badge?:string;badgeTone?:string}){return <div className="panel-title"><span>{icon}</span><h3>{title}</h3>{badge&&<b className={`badge ${badgeTone}`}>● {badge}</b>}</div>}
function EngineMetric({label,value}:{label:string;value:string}){return <div className="engine-metric"><span>{label}</span><strong>{value}</strong></div>}
function Empty({text}:{text:string}){return <div className="empty-state"><span>◇</span><p>{text}</p></div>}
function OpenPosition({p}:{p:Position}){return <div className="open-position"><div className="position-top"><div><span className="asset-badge">◈</span><strong>{symbol(p)}</strong><b className={side(p)==='LONG'?'long':'short'}>{side(p)}</b></div><span>•••</span></div><div className="position-grid"><span>Entrada<strong>{money(p.entry_price)}</strong></span><span>Precio actual<strong>{money(p.current_price||p.entry_price)}</strong></span><span>PnL actual<strong className={num(p.unrealized_pnl)>=0?'green':'red'}>{money(p.unrealized_pnl)}</strong></span></div><div className="position-progress"><i/></div><div className="position-bottom"><span>SL<strong>{money(p.stop_loss)}</strong></span><span>TP1<strong>{money(p.tp1||p.take_profit)}</strong></span><span>TP2<strong>{money(p.tp2)}</strong></span></div></div>}
