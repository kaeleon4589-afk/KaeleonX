import { useEffect, useMemo, useState } from 'react';
import type { MarketCandle, MarketOrderBook, MarketTicker, MarketTrade, Position } from '../../types';

type Props = {
  orderBook: MarketOrderBook;
  trades: MarketTrade[];
  ticker: MarketTicker;
  lastPrice: number | null;
  markPrice: number | null;
  indexPrice: number | null;
  fundingRate: number | null;
  fundingTimestamp: number | null;
  fundingTimestampKind: 'next' | 'last' | null;
  precision: number;
  candles: MarketCandle[];
  activePosition?: Position;
};

type Tab = 'book' | 'trades' | 'market' | 'depth';

const num = (value: unknown): number | null => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

function formatPrice(value: number | null | undefined, precision: number): string {
  if (value == null || !Number.isFinite(value)) return '—';
  const safe = Math.max(0, Math.min(12, precision));
  return new Intl.NumberFormat('en-US', { minimumFractionDigits: safe, maximumFractionDigits: safe }).format(value);
}

function compact(value: number | null | undefined, suffix = ''): string {
  if (value == null || !Number.isFinite(value)) return '—';
  return `${new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 }).format(value)}${suffix}`;
}

function pctRate(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return '—';
  const percentage = Math.abs(value) <= 1 ? value * 100 : value;
  return `${percentage >= 0 ? '+' : ''}${percentage.toFixed(4)}%`;
}

function clock(timestamp: number | null | undefined): string {
  if (!timestamp || !Number.isFinite(timestamp)) return '—';
  const ms = timestamp < 100_000_000_000 ? timestamp * 1000 : timestamp;
  return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function normalizedTime(timestamp: number | null | undefined): number | null {
  if (!timestamp || !Number.isFinite(timestamp)) return null;
  return timestamp < 100_000_000_000 ? timestamp * 1000 : timestamp;
}

function countdown(timestamp: number | null | undefined, now: number): string {
  const target = normalizedTime(timestamp);
  if (target == null) return '—';
  const remaining = Math.max(0, target - now);
  const totalSeconds = Math.floor(remaining / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
}

function OrderBook({ book, precision, lastPrice }: { book: MarketOrderBook; precision: number; lastPrice: number | null }) {
  const asks = book.asks.slice(0, 12).reverse();
  const bids = book.bids.slice(0, 12);
  const bidVolume = bids.reduce((sum, row) => sum + row.quantity, 0);
  const askVolume = asks.reduce((sum, row) => sum + row.quantity, 0);
  const maxQty = Math.max(1e-12, ...asks.map((r) => r.quantity), ...bids.map((r) => r.quantity));
  const bestAsk = book.asks[0]?.price ?? null;
  const bestBid = book.bids[0]?.price ?? null;
  const spread = bestAsk != null && bestBid != null ? Math.max(0, bestAsk - bestBid) : null;
  const total = bidVolume + askVolume;
  const buyPressure = total > 0 ? bidVolume / total * 100 : 50;

  return <div className="market-book">
    <div className="book-head"><span>Precio</span><span>Cantidad</span><span>Total</span></div>
    <div className="book-side asks">
      {asks.map((row) => <div className="book-row" key={`ask-${row.price}`}>
        <i style={{ width: `${Math.min(100, row.quantity / maxQty * 100)}%` }} />
        <strong>{formatPrice(row.price, precision)}</strong><span>{compact(row.quantity)}</span><span>{compact(row.price * row.quantity)}</span>
      </div>)}
    </div>
    <div className="book-mid">
      <strong>{formatPrice(lastPrice, precision)}</strong>
      <span>Spread {formatPrice(spread, precision)}</span>
    </div>
    <div className="book-side bids">
      {bids.map((row) => <div className="book-row" key={`bid-${row.price}`}>
        <i style={{ width: `${Math.min(100, row.quantity / maxQty * 100)}%` }} />
        <strong>{formatPrice(row.price, precision)}</strong><span>{compact(row.quantity)}</span><span>{compact(row.price * row.quantity)}</span>
      </div>)}
    </div>
    <div className="book-pressure">
      <div><strong>{buyPressure.toFixed(1)}%</strong><span>Bid liquidity</span></div>
      <div className="pressure-bar"><i style={{ width: `${buyPressure}%` }} /><b style={{ width: `${100 - buyPressure}%` }} /></div>
      <div><strong>{(100 - buyPressure).toFixed(1)}%</strong><span>Ask liquidity</span></div>
    </div>
    <small className="market-derived-note">Presión derivada de los niveles visibles del libro; no representa cantidad de personas.</small>
  </div>;
}

function RecentTrades({ trades, precision }: { trades: MarketTrade[]; precision: number }) {
  return <div className="market-trades">
    <div className="trades-head"><span>Precio</span><span>Cantidad</span><span>Hora</span></div>
    {trades.slice(0, 30).map((trade) => <div className={`trade-row ${trade.direction}`} key={`${trade.id}-${trade.timestamp}`}>
      <strong>{formatPrice(trade.price, precision)}</strong>
      <span>{compact(trade.quantity)}</span>
      <span>{clock(trade.timestamp)} <b>{trade.direction === 'long' ? 'LONG' : trade.direction === 'short' ? 'SHORT' : '—'}</b></span>
    </div>)}
    {!trades.length && <div className="micro-empty">Esperando operaciones ejecutadas de CoinW…</div>}
  </div>;
}

function DepthChart({ book, precision }: { book: MarketOrderBook; precision: number }) {
  const data = useMemo(() => {
    const bids = book.bids.slice(0, 40);
    const asks = book.asks.slice(0, 40);
    let running = 0;
    const bidCum = bids.map((row) => ({ ...row, cumulative: (running += row.quantity) })).reverse();
    running = 0;
    const askCum = asks.map((row) => ({ ...row, cumulative: (running += row.quantity) }));
    const max = Math.max(1e-12, ...bidCum.map((r) => r.cumulative), ...askCum.map((r) => r.cumulative));
    const bidPoints = bidCum.map((row, index) => `${bidCum.length > 1 ? index / (bidCum.length - 1) * 49 : 49},${42 - row.cumulative / max * 38}`).join(' ');
    const askPoints = askCum.map((row, index) => `${51 + (askCum.length > 1 ? index / (askCum.length - 1) * 49 : 0)},${42 - row.cumulative / max * 38}`).join(' ');
    return { bidPoints, askPoints, bidCum, askCum, max };
  }, [book]);
  const bestBid = book.bids[0]?.price ?? null;
  const bestAsk = book.asks[0]?.price ?? null;

  return <div className="depth-chart-panel">
    <div className="depth-caption"><span>Bids {formatPrice(bestBid, precision)}</span><strong>Profundidad acumulada</strong><span>Asks {formatPrice(bestAsk, precision)}</span></div>
    <svg viewBox="0 0 100 46" preserveAspectRatio="none" className="depth-svg" role="img" aria-label="Gráfico de profundidad del libro">
      <line x1="50" x2="50" y1="2" y2="44" className="depth-center" />
      {data.bidPoints && <><polyline points={data.bidPoints} className="depth-line bid" /><polygon points={`0,44 ${data.bidPoints} 49,44`} className="depth-fill bid" /></>}
      {data.askPoints && <><polyline points={data.askPoints} className="depth-line ask" /><polygon points={`51,44 ${data.askPoints} 100,44`} className="depth-fill ask" /></>}
    </svg>
    <div className="depth-legend"><span><i className="bid"/>Liquidez de compra</span><span><i className="ask"/>Liquidez de venta</span></div>
  </div>;
}

function VolumeProfile({ candles, precision }: { candles: MarketCandle[]; precision: number }) {
  const bins = useMemo(() => {
    const rows = candles.slice(-120);
    if (!rows.length) return [] as Array<{ price: number; volume: number }>;
    const low = Math.min(...rows.map((c) => c.low));
    const high = Math.max(...rows.map((c) => c.high));
    if (!Number.isFinite(low) || !Number.isFinite(high) || high <= low) return [];
    const count = 12;
    const step = (high - low) / count;
    const values = Array.from({ length: count }, (_, i) => ({ price: low + step * (i + 0.5), volume: 0 }));
    for (const candle of rows) {
      const typical = (candle.high + candle.low + candle.close) / 3;
      const index = Math.max(0, Math.min(count - 1, Math.floor((typical - low) / step)));
      values[index].volume += Math.max(0, candle.volume || 0);
    }
    return values.reverse();
  }, [candles]);
  const max = Math.max(1, ...bins.map((row) => row.volume));
  return <div className="volume-profile">
    <div className="volume-profile-title"><strong>Volumen por zona</strong><span>últimas {Math.min(120, candles.length)} velas</span></div>
    {bins.map((row) => <div className="volume-profile-row" key={row.price}>
      <span>{formatPrice(row.price, precision)}</span><i><b style={{ width: `${row.volume / max * 100}%` }} /></i><em>{compact(row.volume)}</em>
    </div>)}
    {!bins.length && <div className="micro-empty">Esperando velas para calcular el perfil.</div>}
    <small>Perfil aproximado: asigna el volumen de cada vela a su precio típico; no es tick-by-tick.</small>
  </div>;
}

function PositionData({ position, lastPrice }: { position?: Position; lastPrice: number | null }) {
  if (!position) return <div className="micro-empty">Este mercado no corresponde a una operación activa.</div>;
  const entry = num(position.entry_price);
  const current = lastPrice ?? num(position.current_price);
  const qty = num(position.quantity);
  const leverage = num(position.leverage);
  const margin = num(position.margin ?? position.estimated_margin ?? position.position_margin);
  const pnl = num(position.unrealized_pnl ?? position.net_pnl ?? position.realized_pnl);
  const exposure = qty != null && current != null ? Math.abs(qty * current) : margin != null && leverage != null ? margin * leverage : null;
  const roe = margin && pnl != null ? pnl / margin * 100 : null;
  return <div className="position-data-grid">
    <span>Entrada<strong>{entry?.toFixed(6) ?? '—'}</strong></span>
    <span>Actual<strong>{current?.toFixed(6) ?? '—'}</strong></span>
    <span>Leverage<strong>{leverage ? `${leverage}x` : '—'}</strong></span>
    <span>Margen<strong>{margin != null ? `$${margin.toFixed(2)}` : '—'}</strong></span>
    <span>Exposición<strong>{exposure != null ? `$${exposure.toFixed(2)}` : '—'}</strong></span>
    <span>PnL<strong className={(pnl ?? 0) >= 0 ? 'positive' : 'negative'}>{pnl != null ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}` : '—'}</strong></span>
    <span>ROE<strong className={(roe ?? 0) >= 0 ? 'positive' : 'negative'}>{roe != null ? `${roe >= 0 ? '+' : ''}${roe.toFixed(2)}%` : '—'}</strong></span>
  </div>;
}

export default function MarketMicrostructure(props: Props) {
  const [tab, setTab] = useState<Tab>('book');
  const { orderBook, trades, ticker, precision, lastPrice, markPrice, indexPrice, fundingRate, fundingTimestamp, fundingTimestampKind, candles, activePosition } = props;
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (fundingTimestampKind !== 'next' || fundingTimestamp == null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [fundingTimestamp, fundingTimestampKind]);
  const bestAsk = orderBook.asks[0]?.price ?? null;
  const bestBid = orderBook.bids[0]?.price ?? null;
  const spread = bestAsk != null && bestBid != null ? bestAsk - bestBid : null;
  const change = ticker.change_rate == null ? null : (Math.abs(ticker.change_rate) <= 1 ? ticker.change_rate * 100 : ticker.change_rate);

  return <section className="market-microstructure" aria-label="Microestructura de mercado CoinW">
    <div className="micro-tabs" role="tablist">
      <button type="button" className={tab === 'book' ? 'active' : ''} onClick={() => setTab('book')}>Order Book</button>
      <button type="button" className={tab === 'trades' ? 'active' : ''} onClick={() => setTab('trades')}>Trades</button>
      <button type="button" className={tab === 'market' ? 'active' : ''} onClick={() => setTab('market')}>Market Data</button>
      <button type="button" className={tab === 'depth' ? 'active' : ''} onClick={() => setTab('depth')}>Depth</button>
    </div>

    {tab === 'book' && <OrderBook book={orderBook} precision={precision} lastPrice={lastPrice} />}
    {tab === 'trades' && <RecentTrades trades={trades} precision={precision} />}
    {tab === 'depth' && <DepthChart book={orderBook} precision={precision} />}
    {tab === 'market' && <div className="market-data-layout">
      <div>
        <div className="market-data-grid">
          <span>Last<strong>{formatPrice(lastPrice ?? ticker.last ?? null, precision)}</strong></span>
          <span>Mark<strong>{formatPrice(markPrice, precision)}</strong></span>
          <span>Index<strong>{formatPrice(indexPrice ?? ticker.index_price ?? null, precision)}</strong></span>
          <span>24h<strong className={(change ?? 0) >= 0 ? 'positive' : 'negative'}>{change == null ? '—' : `${change >= 0 ? '+' : ''}${change.toFixed(2)}%`}</strong></span>
          <span>High 24h<strong>{formatPrice(ticker.high ?? null, precision)}</strong></span>
          <span>Low 24h<strong>{formatPrice(ticker.low ?? null, precision)}</strong></span>
          <span>Vol 24h<strong>{compact(ticker.volume)}</strong></span>
          <span>Vol quote<strong>{compact(ticker.volume_quote, ' USDT')}</strong></span>
          <span>Funding<strong className={(fundingRate ?? 0) >= 0 ? 'positive' : 'negative'}>{pctRate(fundingRate)}</strong></span>
          <span>{fundingTimestampKind === 'next' ? 'Next funding' : 'Funding ref.'}<strong>{fundingTimestampKind === 'next' ? countdown(fundingTimestamp, now) : clock(fundingTimestamp)}</strong></span>
          <span>Spread<strong>{formatPrice(spread, precision)}</strong></span>
          <span>Max leverage<strong>{ticker.max_leverage ? `${ticker.max_leverage}x` : '—'}</strong></span>
        </div>
        <div className="position-data-card"><strong>Operación activa</strong><PositionData position={activePosition} lastPrice={lastPrice} /></div>
      </div>
      <VolumeProfile candles={candles} precision={precision} />
    </div>}
  </section>;
}
