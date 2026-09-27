import { useCallback, useEffect, useMemo, useState } from 'react';
import { api, humanizeError } from '../../lib/api';
import type { ManualOrder, ManualTradingState, Position } from '../../types';
import LiveMarketChart, { type LiveMarketSelection } from './LiveMarketChart';

const money = (value: unknown) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
}).format(Number(value) || 0);
const n = (value: unknown) => { const v = Number(value); return Number.isFinite(v) ? v : 0; };
const displaySymbol = (value: unknown) => String(value || '—').replace('_USDC', '/USDC').replace('USDT', '/USDT').replaceAll('_', '/');
const fmtPrice = (value: unknown, precision = 6) => {
  const num = Number(value);
  if (!Number.isFinite(num) || num <= 0) return '—';
  const digits = Math.max(0, Math.min(12, precision));
  return new Intl.NumberFormat('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits }).format(num);
};
const positionSide = (p: Position) => String(p.direction || p.side || 'LONG').toUpperCase() === 'SHORT' ? 'SHORT' : 'LONG';

function makeClientOrderId() {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID().replaceAll('-', '').slice(0, 16)
    : Math.random().toString(36).slice(2, 14);
  return `MANUAL-${Date.now()}-${random}`.slice(0, 50);
}

type Props = {
  mode: 'demo' | 'live';
  positions: Position[];
  closedPositions: Position[];
  dynamicProtectionEnabled: boolean;
  onRefresh: () => Promise<unknown> | void;
  onNotice: (message: string) => void;
  onError: (message: string) => void;
};

type ConfirmAction =
  | { kind: 'OPEN'; side: 'LONG' | 'SHORT' }
  | { kind: 'CLOSE'; position: Position }
  | { kind: 'CANCEL'; order: ManualOrder }
  | { kind: 'PROTECTION'; position: Position; stopLoss?: number; takeProfit?: number }
  | null;

export default function TradingTerminal({
  mode, positions, closedPositions, dynamicProtectionEnabled, onRefresh, onNotice, onError,
}: Props) {
  const [state, setState] = useState<ManualTradingState | null>(null);
  const [market, setMarket] = useState<LiveMarketSelection | null>(null);
  const [orderType, setOrderType] = useState<'MARKET' | 'LIMIT'>('MARKET');
  const [margin, setMargin] = useState('');
  const [leverage, setLeverage] = useState(10);
  const [limitPrice, setLimitPrice] = useState('');
  const [stopLoss, setStopLoss] = useState('');
  const [takeProfit, setTakeProfit] = useState('');
  const [tpslEnabled, setTpslEnabled] = useState(false);
  const [busy, setBusy] = useState('');
  const [confirmAction, setConfirmAction] = useState<ConfirmAction>(null);
  const [terminalTab, setTerminalTab] = useState<'POSITIONS' | 'ORDERS' | 'HISTORY'>('POSITIONS');
  const [editingProtection, setEditingProtection] = useState<string | null>(null);
  const [focusedPositionId, setFocusedPositionId] = useState<string | null>(null);
  const [editSl, setEditSl] = useState('');
  const [editTp, setEditTp] = useState('');

  const load = useCallback(async (quiet = false) => {
    try {
      const next = await api.manualTradingState();
      setState(next);
    } catch (error) {
      if (!quiet) onError(humanizeError(error));
    }
  }, [onError]);

  useEffect(() => {
    void load();
    const id = window.setInterval(() => void load(true), 5000);
    return () => window.clearInterval(id);
  }, [load, mode]);

  useEffect(() => {
    if (!state) return;
    setLeverage((current) => Math.min(Math.max(1, current), state.max_leverage));
  }, [state]);

  const allOpenPositions = useMemo(() => {
    const map = new Map<string, Position>();
    positions.forEach((position) => map.set(String(position.position_id || `${position.symbol}:${position.opened_at}`), position));
    state?.positions.forEach((position) => map.set(String(position.position_id || `${position.symbol}:${position.opened_at}`), position));
    return [...map.values()];
  }, [positions, state?.positions]);

  const allClosedPositions = useMemo(() => {
    const map = new Map<string, Position>();
    closedPositions.forEach((position) => map.set(String(position.position_id || `${position.symbol}:${position.closed_at}`), position));
    state?.position_history.forEach((position) => map.set(String(position.position_id || `${position.symbol}:${position.closed_at}`), position));
    return [...map.values()];
  }, [closedPositions, state?.position_history]);

  const chartPositions = useMemo(() => {
    if (!focusedPositionId) return allOpenPositions;
    const focused = allOpenPositions.find((position) => String(position.position_id || '') === focusedPositionId);
    if (!focused) return allOpenPositions;
    return [focused, ...allOpenPositions.filter((position) => position !== focused)];
  }, [allOpenPositions, focusedPositionId]);

  const marketMaxLeverage = market?.maxLeverage && Number.isFinite(market.maxLeverage)
    ? Math.max(1, Math.floor(market.maxLeverage))
    : state?.max_leverage || 50;
  const maxLeverage = Math.min(state?.max_leverage || 50, marketMaxLeverage);
  const marginValue = n(margin);
  const notional = marginValue * leverage;
  const referencePrice = orderType === 'LIMIT' ? n(limitPrice) : n(market?.price);
  const marketReferenceFor = (side: 'LONG' | 'SHORT') => orderType === 'LIMIT'
    ? n(limitPrice)
    : n(side === 'LONG' ? (market?.ask ?? market?.price) : (market?.bid ?? market?.price));
  const slValue = n(stopLoss);
  const tpValue = n(takeProfit);

  const orderReady = Boolean(
    state && state.mode === mode && market?.symbol && marginValue >= (state?.min_margin || 1) && leverage >= 1 && leverage <= maxLeverage
    && referencePrice > 0
  );

  const protectionValidFor = (side: 'LONG' | 'SHORT') => {
    const entry = marketReferenceFor(side);
    if (entry <= 0) return false;
    if (!tpslEnabled) return true;
    if (slValue > 0) {
      if (side === 'LONG' ? slValue >= entry : slValue <= entry) return false;
    }
    if (tpValue > 0) {
      if (side === 'LONG' ? tpValue <= entry : tpValue >= entry) return false;
    }
    return true;
  };

  const roiAtPrice = (side: 'LONG' | 'SHORT', price: number) => {
    const entry = marketReferenceFor(side);
    if (entry <= 0 || price <= 0) return null;
    const move = side === 'LONG' ? (price - entry) / entry : (entry - price) / entry;
    return move * leverage * 100;
  };

  const protectionSummary = (side: 'LONG' | 'SHORT') => {
    const tpRoi = tpslEnabled && tpValue > 0 ? roiAtPrice(side, tpValue) : null;
    const slRoi = tpslEnabled && slValue > 0 ? roiAtPrice(side, slValue) : null;
    const parts = [];
    if (tpRoi != null) parts.push(`TP ${tpRoi >= 0 ? '+' : ''}${tpRoi.toFixed(2)}%`);
    if (slRoi != null) parts.push(`SL ${slRoi >= 0 ? '+' : ''}${slRoi.toFixed(2)}%`);
    return parts.join(' · ');
  };

  async function refreshAll() {
    await Promise.allSettled([Promise.resolve(onRefresh()), load(true)]);
  }

  async function submit(side: 'LONG' | 'SHORT', confirmed: boolean) {
    if (!market || !state) return;
    if (!orderReady) {
      onError('Completa el margen y, si usas LIMIT, el precio de entrada.');
      return;
    }
    if (!protectionValidFor(side)) {
      onError(side === 'LONG'
        ? 'Para LONG, el SL debe quedar debajo de la entrada y el TP por encima.'
        : 'Para SHORT, el TP debe quedar debajo de la entrada y el SL por encima.');
      return;
    }
    if (mode === 'live' && !confirmed) {
      setConfirmAction({ kind: 'OPEN', side });
      return;
    }
    setBusy(`open-${side}`);
    onError('');
    try {
      const result = await api.manualPlaceOrder({
        client_order_id: makeClientOrderId(),
        mode,
        symbol: market.symbol,
        side,
        order_type: orderType,
        margin: marginValue,
        leverage,
        ...(orderType === 'LIMIT' ? { limit_price: n(limitPrice) } : {}),
        ...(tpslEnabled && slValue > 0 ? { stop_loss: slValue } : {}),
        ...(tpslEnabled && tpValue > 0 ? { take_profit: tpValue } : {}),
        confirm_live: mode === 'live',
      });
      const filled = Boolean(result.filled);
      const protectionConfirmed = result.protected !== false;
      if (mode === 'live' && filled && !protectionConfirmed) {
        onError('La posición LIVE abrió, pero CoinW no confirmó TP/SL. Revisa la posición y vuelve a fijar la protección inmediatamente.');
        onNotice(`Orden LIVE ${side} ejecutada. TP/SL pendiente de confirmación.`);
      } else {
        onNotice(mode === 'live'
          ? filled ? `Orden LIVE ${side} ejecutada y confirmada en CoinW.` : `Orden LIVE ${side} aceptada por CoinW. Esperando confirmación/fill.`
          : orderType === 'LIMIT' ? `Orden LIMIT DEMO ${side} creada.` : `Operación DEMO ${side} abierta.`);
      }
      setConfirmAction(null);
      await refreshAll();
    } catch (error) {
      onError(humanizeError(error));
    } finally {
      setBusy('');
    }
  }

  async function closePosition(position: Position, confirmed: boolean) {
    const positionId = String(position.position_id || '');
    if (!positionId) return;
    if (mode === 'live' && !confirmed) {
      setConfirmAction({ kind: 'CLOSE', position });
      return;
    }
    setBusy(`close-${positionId}`);
    try {
      const result = await api.manualClosePosition(positionId, mode === 'live');
      onNotice(Boolean(result.pending) ? 'Cierre enviado a CoinW. Esperando confirmación.' : 'Posición DEMO cerrada.');
      setConfirmAction(null);
      await refreshAll();
    } catch (error) {
      onError(humanizeError(error));
    } finally {
      setBusy('');
    }
  }

  async function cancelOrder(order: ManualOrder, confirmed: boolean) {
    if (mode === 'live' && !confirmed) {
      setConfirmAction({ kind: 'CANCEL', order });
      return;
    }
    setBusy(`cancel-${order.manual_order_id}`);
    try {
      await api.manualCancelOrder(order.manual_order_id, mode === 'live');
      onNotice('Orden cancelada.');
      setConfirmAction(null);
      await refreshAll();
    } catch (error) {
      onError(humanizeError(error));
    } finally {
      setBusy('');
    }
  }

  async function saveProtection(position: Position, confirmed = false, stopOverride?: number, targetOverride?: number) {
    const id = String(position.position_id || '');
    const slRaw = stopOverride ?? n(editSl);
    const tpRaw = targetOverride ?? n(editTp);
    const sl = slRaw > 0 ? slRaw : undefined;
    const tp = tpRaw > 0 ? tpRaw : undefined;
    if (!id || (!sl && !tp)) {
      onError('Introduce al menos un Stop Loss o un Take Profit.');
      return;
    }
    if (mode === 'live' && !confirmed) {
      setConfirmAction({ kind: 'PROTECTION', position, stopLoss: sl, takeProfit: tp });
      return;
    }
    setBusy(`protect-${id}`);
    try {
      await api.manualUpdateProtection(id, sl, tp, mode === 'live');
      onNotice(mode === 'live' ? 'TP/SL actualizado en CoinW.' : 'TP/SL DEMO actualizado.');
      setEditingProtection(null);
      setConfirmAction(null);
      await refreshAll();
    } catch (error) {
      onError(humanizeError(error));
    } finally {
      setBusy('');
    }
  }

  function beginProtectionEdit(position: Position) {
    setEditingProtection(String(position.position_id || ''));
    setEditSl(String(position.stop_price ?? position.stop_loss ?? ''));
    setEditTp(String(position.target_price ?? position.take_profit ?? position.tp2_price ?? ''));
  }

  const account = state?.account;
  const availableBalance = n(account?.available);
  const marginPercent = availableBalance > 0 ? Math.max(0, Math.min(100, marginValue / availableBalance * 100)) : 0;
  const setMarginPercent = (percent: number) => {
    const value = availableBalance * Math.max(0, Math.min(100, percent)) / 100;
    setMargin(value > 0 ? value.toFixed(2) : '');
  };
  const liveDanger = mode === 'live';

  return <div className="trading-terminal">
    <div className="terminal-account-bar">
      <div className={`terminal-mode ${mode}`}><i />{mode.toUpperCase()} <small>{mode === 'live' ? 'CoinW REAL' : 'Simulación'}</small></div>
      <span><small>Balance</small><strong>{money(account?.balance)}</strong></span>
      <span><small>Disponible</small><strong>{money(account?.available)}</strong></span>
      <span><small>Margen usado</small><strong>{money(account?.used_margin)}</strong></span>
      <span><small>PnL no realizado</small><strong className={n(account?.unrealized_pnl) >= 0 ? 'green' : 'red'}>{money(account?.unrealized_pnl)}</strong></span>
      <span><small>Equity</small><strong>{money(account?.equity)}</strong></span>
    </div>

    {liveDanger && <div className="terminal-live-warning">LIVE · Las órdenes de este panel se envían a tu cuenta real de CoinW después de confirmarlas.</div>}
    {state && state.mode !== mode && <div className="terminal-sync-warning">Sincronizando la terminal con el modo {mode.toUpperCase()}…</div>}

    <div className="terminal-workspace">
      <div className="terminal-chart-column">
        <LiveMarketChart
          positions={chartPositions}
          closedPositions={allClosedPositions}
          dynamicProtectionEnabled={dynamicProtectionEnabled}
          compactTradingMode
          onMarketChange={setMarket}
        />
      </div>

      <aside className="manual-order-panel coinw-order-panel" aria-label="Operativa manual">
        <div className="manual-panel-title coinw-panel-head">
          <div><small>Futuros · Operativa manual</small><strong>{market?.display || 'Selecciona un mercado'} <em>Perp.</em></strong></div>
          <b className={mode}>{mode.toUpperCase()}</b>
        </div>

        <div className="coinw-mode-row">
          <button type="button" className="margin-mode-pill">Aislado (comb.)</button>
          <button type="button" className="leverage-pill">{leverage}x</button>
        </div>

        <div className="coinw-open-close-tabs">
          <button type="button" className="active">Abrir</button>
          <button type="button" onClick={() => setTerminalTab('POSITIONS')}>Cerrar</button>
        </div>

        <div className="manual-order-tabs coinw-order-type">
          <button type="button" className={orderType === 'MARKET' ? 'active' : ''} onClick={() => setOrderType('MARKET')}>Market</button>
          <button type="button" className={orderType === 'LIMIT' ? 'active' : ''} onClick={() => { setOrderType('LIMIT'); if (!limitPrice && market?.price) setLimitPrice(String(market.price)); }}>Limit</button>
        </div>

        <div className="coinw-live-price">
          <small>Precio actual</small>
          <strong>{fmtPrice(market?.price, market?.pricePrecision)}</strong>
          <span>Bid {fmtPrice(market?.bid, market?.pricePrecision)} · Ask {fmtPrice(market?.ask, market?.pricePrecision)}</span>
        </div>

        {orderType === 'LIMIT' && <div className="coinw-price-input-row">
          <label className="manual-field"><span>Precio <em>USDT</em></span><input inputMode="decimal" value={limitPrice} onChange={e => setLimitPrice(e.target.value)} placeholder="Precio límite" /></label>
          <button type="button" className="bbo-button" onClick={() => market?.price && setLimitPrice(String(market.price))}>BBO</button>
        </div>}

        <label className="manual-field coinw-amount-field"><span>Monto / Margen <em>USDT</em></span><input inputMode="decimal" value={margin} onChange={e => setMargin(e.target.value)} placeholder={`Mín. ${state?.min_margin || 1}`} /></label>

        <div className="coinw-margin-slider">
          <input type="range" min="0" max="100" step="1" value={marginPercent} onChange={e => setMarginPercent(Number(e.target.value))} aria-label="Porcentaje del saldo disponible" />
          <div>{[0, 25, 50, 75, 100].map(v => <button type="button" key={v} onClick={() => setMarginPercent(v)}>{v}%</button>)}</div>
        </div>

        <div className="coinw-available-row"><span>Disponible</span><strong>{money(account?.available)}</strong></div>

        <div className="manual-leverage coinw-leverage-control">
          <div><span>Leverage</span><strong>{leverage}x</strong></div>
          <div className="leverage-presets">{[1, 5, 10, 20, 30, 50].filter(v => v <= maxLeverage).map(v => <button type="button" key={v} className={leverage === v ? 'active' : ''} onClick={() => setLeverage(v)}>{v}x</button>)}</div>
        </div>

        <label className="coinw-tpsl-toggle">
          <input type="checkbox" checked={tpslEnabled} onChange={e => setTpslEnabled(e.target.checked)} />
          <span>TP/SL <small>Opcional · también puedes configurarlo después de abrir</small></span>
        </label>

        {tpslEnabled && <div className="manual-tpsl-grid coinw-tpsl-grid">
          <label className="manual-field"><span>Stop Loss</span><input inputMode="decimal" value={stopLoss} onChange={e => setStopLoss(e.target.value)} placeholder="SL opcional" /></label>
          <label className="manual-field"><span>Take Profit</span><input inputMode="decimal" value={takeProfit} onChange={e => setTakeProfit(e.target.value)} placeholder="TP opcional" /></label>
        </div>}

        <div className="manual-order-summary coinw-order-summary">
          <span>Notional <b>{money(notional)}</b></span>
          <span>Costo / Margen <b>{money(marginValue)}</b></span>
          <span>Modo margen <b>ISOLATED</b></span>
          <span>Precio ref. <b>{fmtPrice(referencePrice, market?.pricePrecision)}</b></span>
        </div>

        <div className="manual-side-actions coinw-side-actions">
          <button type="button" className="manual-long" disabled={!orderReady || Boolean(busy) || !protectionValidFor('LONG')} onClick={() => void submit('LONG', false)}>
            <strong>{busy === 'open-LONG' ? 'Enviando…' : 'Abrir long'}</strong>
            {protectionSummary('LONG') && <small>{protectionSummary('LONG')}</small>}
          </button>
          <button type="button" className="manual-short" disabled={!orderReady || Boolean(busy) || !protectionValidFor('SHORT')} onClick={() => void submit('SHORT', false)}>
            <strong>{busy === 'open-SHORT' ? 'Enviando…' : 'Abrir short'}</strong>
            {protectionSummary('SHORT') && <small>{protectionSummary('SHORT')}</small>}
          </button>
        </div>
        <small className="manual-panel-note">TP y SL no son obligatorios. Si los introduces, KAELEON muestra el ROI estimado con el leverage actual antes de abrir.</small>
      </aside>
    </div>

    <div className="terminal-book">
      <div className="terminal-tabs">
        <button type="button" className={terminalTab === 'POSITIONS' ? 'active' : ''} onClick={() => setTerminalTab('POSITIONS')}>Posiciones <b>{allOpenPositions.length}</b></button>
        <button type="button" className={terminalTab === 'ORDERS' ? 'active' : ''} onClick={() => setTerminalTab('ORDERS')}>Órdenes abiertas <b>{state?.open_orders.length || 0}</b></button>
        <button type="button" className={terminalTab === 'HISTORY' ? 'active' : ''} onClick={() => setTerminalTab('HISTORY')}>Historial <b>{(state?.position_history.length || 0) + (state?.order_history.length || 0)}</b></button>
      </div>

      {terminalTab === 'POSITIONS' && <div className="terminal-position-list">
        {allOpenPositions.length ? allOpenPositions.map(position => {
          const id = String(position.position_id || `${position.symbol}:${position.opened_at}`);
          const side = positionSide(position);
          const source = String(position.source || 'BOT').toUpperCase();
          const entry = n(position.entry_price);
          const qty = n(position.quantity);
          const marginUsed = n(position.position_margin) || (entry * qty / Math.max(1, n(position.leverage) || 1));
          const upnl = n(position.unrealized_pnl);
          const roi = marginUsed > 0 ? upnl / marginUsed * 100 : 0;
          const canManage = source === 'MANUAL';
          const editing = editingProtection === id;
          return <div className="terminal-position-row" key={id}>
            <div className="terminal-position-id"><strong>{displaySymbol(position.symbol)}</strong><span className={side === 'LONG' ? 'green' : 'red'}>{side} · {position.leverage || 1}x</span><em className={position.protected === false ? 'protection-unconfirmed' : ''}>{source}{position.protected === false ? ' · TP/SL NO CONFIRMADO' : ''}</em></div>
            <span><small>Entry</small><b>{fmtPrice(entry)}</b></span>
            <span><small>Margen</small><b>{money(marginUsed)}</b></span>
            <span><small>PnL</small><b className={upnl >= 0 ? 'green' : 'red'}>{money(upnl)}</b></span>
            <span><small>ROI</small><b className={roi >= 0 ? 'green' : 'red'}>{roi >= 0 ? '+' : ''}{roi.toFixed(2)}%</b></span>
            <span><small>Liq.</small><b>{position.liquidation_price ? fmtPrice(position.liquidation_price) : '—'}</b></span>
            <span><small>TP / SL</small><b>{fmtPrice(position.target_price ?? position.take_profit)} / {fmtPrice(position.stop_price ?? position.stop_loss)}</b></span>
            <div className="terminal-position-actions">
              <button type="button" className={focusedPositionId === id ? 'active' : ''} onClick={() => setFocusedPositionId(id)}>Ver gráfico</button>
              {canManage && <button type="button" onClick={() => beginProtectionEdit(position)}>TP/SL</button>}
              {canManage && <button type="button" className="danger" disabled={busy === `close-${id}`} onClick={() => void closePosition(position, false)}>{position.settlement_pending ? 'Cerrando…' : 'Cerrar'}</button>}
            </div>
            {editing && <div className="terminal-protection-editor"><label>SL<input value={editSl} onChange={e => setEditSl(e.target.value)} /></label><label>TP<input value={editTp} onChange={e => setEditTp(e.target.value)} /></label><button type="button" onClick={() => void saveProtection(position)}>Guardar</button><button type="button" onClick={() => setEditingProtection(null)}>Cancelar</button></div>}
          </div>;
        }) : <div className="terminal-empty">No hay posiciones abiertas.</div>}
      </div>}

      {terminalTab === 'ORDERS' && <div className="terminal-order-list">
        {state?.open_orders.length ? state.open_orders.map(order => <div className="terminal-order-row" key={order.manual_order_id}>
          <div><strong>{displaySymbol(order.symbol)}</strong><span className={order.side === 'LONG' ? 'green' : 'red'}>{order.side} · {order.leverage}x</span></div>
          <span><small>Tipo</small><b>{order.order_type}</b></span>
          <span><small>Precio</small><b>{order.limit_price ? fmtPrice(order.limit_price) : 'Market'}</b></span>
          <span><small>Margen</small><b>{money(order.margin)}</b></span>
          <span><small>TP / SL</small><b>{fmtPrice(order.target_price)} / {fmtPrice(order.stop_price)}</b></span>
          <span><small>Estado</small><b>{order.exchange_status || order.status}</b></span>
          <button type="button" className="terminal-cancel-order" disabled={busy === `cancel-${order.manual_order_id}` || (mode === 'live' && !order.order_id)} onClick={() => void cancelOrder(order, false)}>{mode === 'live' && !order.order_id ? 'Reconciliando…' : 'Cancelar'}</button>
        </div>) : <div className="terminal-empty">No hay órdenes manuales pendientes.</div>}
      </div>}

      {terminalTab === 'HISTORY' && <div className="terminal-history">
        <div className="terminal-history-heading"><strong>Operaciones manuales cerradas</strong><span>{state?.position_history.length || 0}</span></div>
        <div className="terminal-position-list">
          {state?.position_history.length ? state.position_history.map(position => {
            const id = String(position.position_id || `${position.symbol}:${position.closed_at}`);
            const side = positionSide(position);
            const entry = n(position.entry_price);
            const exit = n(position.exit_price);
            const qty = n(position.quantity);
            const marginUsed = n(position.position_margin) || (entry * qty / Math.max(1, n(position.leverage) || 1));
            const netPnl = position.net_pnl != null
              ? n(position.net_pnl)
              : n(position.realized_pnl) - n(position.entry_fee) - n(position.exit_fee) + n(position.funding_pnl);
            const roi = marginUsed > 0 ? netPnl / marginUsed * 100 : 0;
            return <div className="terminal-position-row" key={id}>
              <div className="terminal-position-id"><strong>{displaySymbol(position.symbol)}</strong><span className={side === 'LONG' ? 'green' : 'red'}>{side} · {position.leverage || 1}x</span><em>MANUAL</em></div>
              <span><small>Entrada</small><b>{fmtPrice(entry)}</b></span>
              <span><small>Salida</small><b>{fmtPrice(exit)}</b></span>
              <span><small>Margen</small><b>{money(marginUsed)}</b></span>
              <span><small>PnL neto</small><b className={netPnl >= 0 ? 'green' : 'red'}>{money(netPnl)}</b></span>
              <span><small>ROI</small><b className={roi >= 0 ? 'green' : 'red'}>{roi >= 0 ? '+' : ''}{roi.toFixed(2)}%</b></span>
              <span><small>Resultado</small><b className={netPnl >= 0 ? 'green' : 'red'}>{netPnl > 0 ? 'WIN' : netPnl < 0 ? 'LOSS' : 'BE'}</b></span>
              <span><small>Cierre</small><b>{String(position.exit_reason || 'CLOSED')}</b></span>
            </div>;
          }) : <div className="terminal-empty">Todavía no hay operaciones manuales cerradas.</div>}
        </div>

        <div className="terminal-history-heading"><strong>Auditoría de órdenes manuales</strong><span>{state?.order_history.length || 0}</span></div>
        <div className="terminal-order-list">
          {state?.order_history.length ? state.order_history.map(order => <div className="terminal-order-row" key={order.manual_order_id}>
            <div><strong>{displaySymbol(order.symbol)}</strong><span className={order.side === 'LONG' ? 'green' : 'red'}>{order.side}</span></div>
            <span><small>Tipo</small><b>{order.order_type}</b></span>
            <span><small>Margen</small><b>{money(order.margin)}</b></span>
            <span><small>Leverage</small><b>{order.leverage}x</b></span>
            <span><small>Fill</small><b>{order.fill_price ? fmtPrice(order.fill_price) : '—'}</b></span>
            <span><small>Estado</small><b>{order.exchange_status || order.status}</b></span>
          </div>) : <div className="terminal-empty">No hay órdenes manuales finalizadas todavía.</div>}
        </div>
      </div>}
    </div>

    {confirmAction && mode === 'live' && <div className="live-order-modal-backdrop" role="presentation">
      <div className="live-order-modal" role="dialog" aria-modal="true" aria-label="Confirmar operación LIVE">
        <b className="live-order-modal-badge">LIVE · DINERO REAL</b>
        {confirmAction.kind === 'OPEN' && <>
          <h3>Confirmar orden {confirmAction.side}</h3>
          <p>Esta orden se enviará a CoinW usando la API Key configurada.</p>
          <div className="live-confirm-grid"><span>Par<b>{market?.display}</b></span><span>Tipo<b>{orderType}</b></span><span>Margen<b>{money(marginValue)}</b></span><span>Leverage<b>{leverage}x</b></span><span>Notional<b>{money(notional)}</b></span><span>Entrada estimada<b>{fmtPrice(marketReferenceFor(confirmAction.side), market?.pricePrecision)}</b></span><span>TP<b>{fmtPrice(tpValue, market?.pricePrecision)}</b></span><span>SL<b>{fmtPrice(slValue, market?.pricePrecision)}</b></span></div>
          <div className="live-modal-actions"><button type="button" onClick={() => setConfirmAction(null)}>Cancelar</button><button type="button" className={confirmAction.side === 'LONG' ? 'confirm-long' : 'confirm-short'} onClick={() => void submit(confirmAction.side, true)}>Confirmar LIVE</button></div>
        </>}
        {confirmAction.kind === 'CLOSE' && <>
          <h3>Cerrar posición real</h3><p>CoinW recibirá una solicitud de cierre a mercado para el 100% de esta posición.</p>
          <div className="live-confirm-grid"><span>Par<b>{displaySymbol(confirmAction.position.symbol)}</b></span><span>Dirección<b>{positionSide(confirmAction.position)}</b></span><span>Origen<b>{String(confirmAction.position.source || 'MANUAL')}</b></span><span>Position ID<b>{String(confirmAction.position.position_id || '')}</b></span></div>
          <div className="live-modal-actions"><button type="button" onClick={() => setConfirmAction(null)}>Cancelar</button><button type="button" className="confirm-short" onClick={() => void closePosition(confirmAction.position, true)}>Confirmar cierre</button></div>
        </>}
        {confirmAction.kind === 'CANCEL' && <>
          <h3>Cancelar orden real</h3><p>Se solicitará a CoinW cancelar la orden todavía no ejecutada.</p>
          <div className="live-confirm-grid"><span>Par<b>{displaySymbol(confirmAction.order.symbol)}</b></span><span>Orden<b>{confirmAction.order.order_type}</b></span><span>Dirección<b>{confirmAction.order.side}</b></span><span>Margen<b>{money(confirmAction.order.margin)}</b></span></div>
          <div className="live-modal-actions"><button type="button" onClick={() => setConfirmAction(null)}>Volver</button><button type="button" className="confirm-short" onClick={() => void cancelOrder(confirmAction.order, true)}>Cancelar orden LIVE</button></div>
        </>}
        {confirmAction.kind === 'PROTECTION' && <>
          <h3>Modificar TP/SL real</h3><p>Las nuevas protecciones se enviarán a CoinW y no se mostrarán como confirmadas hasta que la API acepte el cambio.</p>
          <div className="live-confirm-grid"><span>Par<b>{displaySymbol(confirmAction.position.symbol)}</b></span><span>Dirección<b>{positionSide(confirmAction.position)}</b></span><span>Stop Loss<b>{fmtPrice(confirmAction.stopLoss)}</b></span><span>Take Profit<b>{fmtPrice(confirmAction.takeProfit)}</b></span></div>
          <div className="live-modal-actions"><button type="button" onClick={() => setConfirmAction(null)}>Volver</button><button type="button" className="confirm-long" onClick={() => void saveProtection(confirmAction.position, true, confirmAction.stopLoss, confirmAction.takeProfit)}>Confirmar cambio LIVE</button></div>
        </>}
      </div>
    </div>}
  </div>;
}
