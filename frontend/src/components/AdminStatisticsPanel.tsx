import { useEffect, useRef, useState } from 'react';
import { api, humanizeError } from '../lib/api';
import type { AdminStatistics } from '../types';
import StatCard from './StatCard';

const date = (stamp: number) => new Date(stamp).toLocaleString('es');

export default function AdminStatisticsPanel() {
  const [mode, setMode] = useState<'demo' | 'live'>('demo');
  const [data, setData] = useState<AdminStatistics | null>(null);
  const [label, setLabel] = useState('');
  const [confirmed, setConfirmed] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [refresh, setRefresh] = useState(0);
  const pending = useRef<{mode: 'demo' | 'live'; label: string; confirm: true; request_id: string} | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true); setData(null); setError('');
    api.adminStatistics(mode).then(result => { if (active) setData(result); })
      .catch(e => { if (active) setError(humanizeError(e)); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [mode, refresh]);

  async function reset() {
    if (busy || !confirmed || !label.trim()) return;
    setBusy(true); setError(''); setMessage('');
    // Reuse the receipt on retry if the server accepted a request whose response was lost.
    const body = pending.current || {mode, label: label.trim(), confirm: true as const, request_id: crypto.randomUUID()};
    pending.current = body;
    try {
      await api.adminResetStatistics(body);
      pending.current = null;
      setMessage(`Nueva etapa ${body.mode.toUpperCase()} iniciada: ${body.label}.`);
      setConfirmed(false); setLabel(''); setRefresh(v => v + 1);
    } catch (e) { setError(humanizeError(e)); }
    finally { setBusy(false); }
  }

  const metrics = data?.metrics;
  return <section className="admin-panel statistics-panel">
    <h2>Estadísticas por setup</h2>
    <p>Medición global de todos los usuarios del modo seleccionado. Este reinicio solo afecta a este apartado; el rendimiento histórico personal se conserva.</p>
    <div className="statistics-controls">
      <label>Modo<select value={mode} disabled={busy} onChange={e => {
        setMode(e.target.value as 'demo' | 'live'); setConfirmed(false); setMessage(''); pending.current = null;
      }}><option value="demo">DEMO</option><option value="live">LIVE</option></select></label>
      <button className="secondary-btn" disabled={loading || busy} onClick={() => setRefresh(v => v + 1)}>Actualizar estadísticas</button>
    </div>
    {error && <div role="alert" className="form-error">{error}</div>}
    {message && <p role="status" className="manual-live-success">{message}</p>}
    {loading ? <p role="status">Cargando estadísticas…</p> : data && <>
      <p>Etapa: <strong>{data.period?.label || 'Histórico completo'}</strong> · Desde: {data.period ? date(data.period.started_at) : 'el inicio'}</p>
      <div className="admin-card-grid">
        <StatCard label="Operaciones cerradas" value={metrics!.trades}/>
        <StatCard label="PnL neto (USDT)" value={metrics!.pnl.toFixed(2)} tone={metrics!.pnl < 0 ? 'red' : 'green'}/>
        <StatCard label="Acierto" value={`${metrics!.win_rate.toFixed(1)}%`}/>
        <StatCard label="Ganadas / perdidas / neutras" value={`${metrics!.wins} / ${metrics!.losses} / ${metrics!.breakeven}`}/>
        <StatCard label="Profit factor" value={metrics!.profit_factor?.toFixed(2) ?? '—'} sub="Sin pérdidas cerradas: no calculable"/>
        <StatCard label="Abiertas en esta etapa" value={metrics!.open_positions}/>
      </div>
      {metrics!.trades === 0 && <p>No hay operaciones cerradas en esta etapa.</p>}
      <p>Operaciones anteriores excluidas: {metrics!.excluded_positions}. Las abiertas antes del reinicio no entrarán en esta etapa cuando cierren.</p>
    </>}
    <form className="statistics-reset" onSubmit={e => {e.preventDefault(); void reset();}}>
      <h3>Reiniciar estadísticas {mode.toUpperCase()}</h3>
      <p>Comienza una etapa nueva desde cero. Conserva saldos, historial, órdenes, posiciones abiertas y límites de riesgo. El otro modo permanece igual.</p>
      <label>Nombre del setup / etapa<input maxLength={100} required value={label} disabled={busy} placeholder="Ej.: Setup v2 — filtro de tendencia" onChange={e => {setLabel(e.target.value); pending.current = null; setConfirmed(false);}}/></label>
      <label className="statistics-confirm"><input type="checkbox" checked={confirmed} disabled={busy} onChange={e => setConfirmed(e.target.checked)}/>Confirmo iniciar una nueva medición global en {mode.toUpperCase()}.</label>
      <button className="danger-outline" disabled={busy || loading || !data || !confirmed || !label.trim()}>{busy ? 'Reiniciando…' : `Reiniciar ${mode.toUpperCase()}`}</button>
    </form>
    {!!data?.history.length && <details><summary>Últimos reinicios registrados</summary>{data.history.map(period => <p key={period.reset_id}><strong>{period.label}</strong> · {date(period.started_at)} · Antes del reinicio: {period.previous_metrics.trades} cierres, {period.previous_metrics.pnl.toFixed(2)} USDT netos.</p>)}</details>}
  </section>;
}
