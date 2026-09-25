import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { dispose, init, registerOverlay } from 'klinecharts';
import { api, humanizeError } from '../../lib/api';
import type { MarketCandle, MarketInstrument, MarketOrderBook, MarketTicker, MarketTrade, Position } from '../../types';
import MarketMicrostructure from './MarketMicrostructure';

const COINW_FUTURES_WS = 'wss://ws.futurescw.com/perpum';
const TRADE_LEVEL_GROUP = 'kaeleon-trade-levels';
const TRADE_LEVEL_OVERLAY = 'kaeleonTradeLevel';
const RISK_ZONE_GROUP = 'kaeleon-risk-zones';
const RISK_ZONE_OVERLAY = 'kaeleonRiskZone';
const HISTORY_GROUP = 'kaeleon-trade-history';
const HISTORY_OVERLAY = 'kaeleonTradeMarker';
const DRAWING_GROUP = 'kaeleon-user-drawings';
const ALERT_STORAGE_PREFIX = 'kaeleon_price_alerts_';
const WS_STALE_AFTER_MS = 25_000;
const WS_RECONNECT_MAX_MS = 12_000;
const FALLBACK_POLL_MS = 5_000;

type KChart = NonNullable<ReturnType<typeof init>>;
type KLineData = { timestamp: number; open: number; high: number; low: number; close: number; volume?: number; turnover?: number };
type Period = { type: 'second' | 'minute' | 'hour' | 'day' | 'week' | 'month' | 'year'; span: number };
type SymbolInfo = { ticker: string; pricePrecision?: number; volumePrecision?: number };

const TIMEFRAMES = [
  { key: '1m', label: '1m', period: { type: 'minute', span: 1 } as Period, ws: '1' },
  { key: '3m', label: '3m', period: { type: 'minute', span: 3 } as Period, ws: '3' },
  { key: '5m', label: '5m', period: { type: 'minute', span: 5 } as Period, ws: '5' },
  { key: '15m', label: '15m', period: { type: 'minute', span: 15 } as Period, ws: '15' },
  { key: '30m', label: '30m', period: { type: 'minute', span: 30 } as Period, ws: '30' },
  { key: '1h', label: '1H', period: { type: 'hour', span: 1 } as Period, ws: '1H' },
  { key: '4h', label: '4H', period: { type: 'hour', span: 4 } as Period, ws: '4H' },
  { key: '1d', label: '1D', period: { type: 'day', span: 1 } as Period, ws: '1D' },
] as const;

type TimeframeKey = (typeof TIMEFRAMES)[number]['key'];
type StreamState = 'connecting' | 'live' | 'reconnecting' | 'polling' | 'offline';
type MainIndicator = 'NONE' | 'MA' | 'EMA' | 'BOLL';
type SubIndicator = 'VOL' | 'MACD' | 'RSI';
type ChartMode = 'candle' | 'area';
type DrawingTool = 'straightLine' | 'segment' | 'horizontalStraightLine' | 'priceLine' | 'priceChannelLine' | 'parallelStraightLine' | 'fibonacciLine' | 'simpleAnnotation' | 'brush';
type PriceAlert = { id: string; price: number; direction: 'above' | 'below'; triggered: boolean };

const EMPTY_ORDER_BOOK: MarketOrderBook = { asks: [], bids: [], timestamp: null };
const INDICATOR_DEFAULTS: Record<string, number[]> = {
  MA: [5, 10, 30, 60], EMA: [6, 12, 20], BOLL: [20, 2],
  VOL: [5, 10, 20], MACD: [12, 26, 9], RSI: [6, 12, 24],
};

type TradeLevel = {
  key: string;
  label: string;
  value: number;
  tone: 'entry' | 'tp' | 'sl' | 'be';
  color: string;
};

// Draw the operation levels inside the candle pane itself. The horizontal
// line is intentionally non-interactive: users can inspect the chart without
// accidentally moving the real ENTRY/TP/SL values recorded by the engine.
const overlayRegistry = globalThis as typeof globalThis & { __kaeleonTradeLevelOverlay?: boolean };
if (!overlayRegistry.__kaeleonTradeLevelOverlay) {
  registerOverlay({
    name: TRADE_LEVEL_OVERLAY,
    totalStep: 2,
    lock: true,
    fixedZLevel: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, bounding, overlay }: any) => {
      if (!coordinates?.length) return [];
      const y = Number(coordinates[0]?.y);
      if (!Number.isFinite(y)) return [];
      const data = (overlay?.extendData || {}) as { label?: string; priceText?: string; color?: string; tone?: string };
      const color = data.color || '#32c7ff';
      const text = [data.label, data.priceText].filter(Boolean).join('  ');
      const nearTop = y < 28;
      return [
        {
          key: 'level-line',
          type: 'line',
          attrs: { coordinates: [{ x: 0, y }, { x: bounding.width, y }] },
          styles: { color, size: data.tone === 'entry' ? 2 : 1, style: 'dashed', dashedValue: [6, 4] },
          ignoreEvent: true,
        },
        {
          key: 'level-label',
          type: 'text',
          attrs: { x: 8, y: nearTop ? y + 5 : y - 5, text, align: 'left', baseline: nearTop ? 'top' : 'bottom' },
          styles: {
            style: 'fill',
            color,
            size: 11,
            weight: 700,
            paddingLeft: 5,
            paddingRight: 5,
            paddingTop: 3,
            paddingBottom: 3,
            borderSize: 1,
            borderColor: color,
            borderRadius: 4,
            backgroundColor: 'rgba(3,16,25,.90)',
          },
          ignoreEvent: true,
        },
      ];
    },
  });
  overlayRegistry.__kaeleonTradeLevelOverlay = true;
}


const advancedOverlayRegistry = globalThis as typeof globalThis & {
  __kaeleonRiskZoneOverlay?: boolean;
  __kaeleonTradeMarkerOverlay?: boolean;
};

if (!advancedOverlayRegistry.__kaeleonRiskZoneOverlay) {
  registerOverlay({
    name: RISK_ZONE_OVERLAY,
    totalStep: 3,
    lock: true,
    fixedZLevel: false,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, bounding, overlay }: any) => {
      if (!coordinates || coordinates.length < 2) return [];
      const y1 = Number(coordinates[0]?.y);
      const y2 = Number(coordinates[1]?.y);
      if (!Number.isFinite(y1) || !Number.isFinite(y2)) return [];
      const top = Math.min(y1, y2);
      const height = Math.max(1, Math.abs(y1 - y2));
      const data = (overlay?.extendData || {}) as { color?: string; label?: string };
      const color = data.color || 'rgba(0,230,151,.08)';
      return [
        {
          key: 'risk-zone',
          type: 'rect',
          attrs: { x: 0, y: top, width: bounding.width, height },
          styles: { style: 'fill', color },
          ignoreEvent: true,
        },
      ];
    },
  });
  advancedOverlayRegistry.__kaeleonRiskZoneOverlay = true;
}

if (!advancedOverlayRegistry.__kaeleonTradeMarkerOverlay) {
  registerOverlay({
    name: HISTORY_OVERLAY,
    totalStep: 2,
    lock: true,
    fixedZLevel: true,
    needDefaultPointFigure: false,
    needDefaultXAxisFigure: false,
    needDefaultYAxisFigure: false,
    createPointFigures: ({ coordinates, overlay }: any) => {
      if (!coordinates?.length) return [];
      const x = Number(coordinates[0]?.x);
      const y = Number(coordinates[0]?.y);
      if (!Number.isFinite(x) || !Number.isFinite(y)) return [];
      const data = (overlay?.extendData || {}) as { label?: string; detail?: string; color?: string; placement?: 'top' | 'bottom' };
      const color = data.color || '#32c7ff';
      const isTop = data.placement !== 'bottom';
      const labelY = isTop ? y - 10 : y + 10;
      return [
        {
          key: 'history-dot',
          type: 'circle',
          attrs: { x, y, r: 4 },
          styles: { style: 'fill', color, borderColor: '#031019', borderSize: 1 },
          ignoreEvent: true,
        },
        {
          key: 'history-label',
          type: 'text',
          attrs: { x, y: labelY, text: data.label || '', align: 'center', baseline: isTop ? 'bottom' : 'top' },
          styles: {
            style: 'fill', color, size: 9, weight: 800,
            paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2,
            backgroundColor: 'rgba(3,16,25,.9)', borderColor: color, borderSize: 1, borderRadius: 3,
          },
          ignoreEvent: true,
        },
      ];
    },
  });
  advancedOverlayRegistry.__kaeleonTradeMarkerOverlay = true;
}

function canonicalSymbol(input: unknown): string {
  let value = String(input || '').trim().toUpperCase().replaceAll('-', '_').replaceAll('/', '_');
  value = value.split('_').filter(Boolean).join('_');
  if (!value) return 'BTCUSDT';
  if (value.endsWith('_USDC')) return value;
  if (value.endsWith('USDC')) return `${value.slice(0, -4)}_USDC`;
  if (value.endsWith('_USDT')) return `${value.slice(0, -5)}USDT`;
  if (value.endsWith('USDT')) return value;
  if (value.includes('_')) return value;
  return `${value}USDT`;
}

function pairCode(symbol: string): string {
  const normalized = canonicalSymbol(symbol);
  return normalized.endsWith('USDT') ? normalized.slice(0, -4) : normalized;
}

function displaySymbol(symbol: string): string {
  const normalized = canonicalSymbol(symbol);
  if (normalized.endsWith('USDT')) return `${normalized.slice(0, -4)}/USDT`;
  if (normalized.endsWith('_USDC')) return `${normalized.slice(0, -5)}/USDC`;
  return normalized.replaceAll('_', '/');
}

function positionSymbol(position: Position): string {
  return canonicalSymbol(position.symbol || 'BTC');
}

function toNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function toKLineData(candle: MarketCandle): KLineData {
  return {
    timestamp: Number(candle.timestamp),
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
    volume: Number(candle.volume || 0),
  };
}

function timeframeFromPeriod(period: Period): TimeframeKey {
  const match = TIMEFRAMES.find((item) => item.period.type === period.type && item.period.span === period.span);
  return match?.key || '5m';
}

function wsInterval(period: Period): string {
  const match = TIMEFRAMES.find((item) => item.period.type === period.type && item.period.span === period.span);
  return match?.ws || '5';
}

function subscriptionKey(symbol: SymbolInfo, period: Period): string {
  return `${canonicalSymbol(symbol.ticker)}:${period.type}:${period.span}`;
}

function formatPrice(value: number | null, precision = 6): string {
  if (value == null || !Number.isFinite(value)) return '—';
  const safePrecision = Math.max(0, Math.min(12, precision));
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: safePrecision,
    maximumFractionDigits: safePrecision,
  }).format(value);
}


function indicatorParams(text: string, fallback: number[]): number[] {
  const parsed = text.split(',').map((part) => Number(part.trim())).filter((value) => Number.isFinite(value) && value > 0).slice(0, 8);
  return parsed.length ? parsed : fallback;
}

function unwrapSocketPayload(raw: unknown): Record<string, unknown> | null {
  if (typeof raw !== 'string') return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed as Record<string, unknown> : null;
  } catch {
    return null;
  }
}

function unwrapData(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try { return JSON.parse(value); } catch { return value; }
}

function socketCandle(data: unknown): KLineData | null {
  const value = unwrapData(data);
  if (!Array.isArray(value) || value.length < 6) return null;
  const timestampRaw = Number(value[0]);
  const timestamp = timestampRaw < 100_000_000_000 ? timestampRaw * 1000 : timestampRaw;
  const open = Number(value[1]);
  const high = Number(value[2]);
  const low = Number(value[3]);
  const close = Number(value[4]);
  const volume = Number(value[5]);
  if (![timestamp, open, high, low, close, volume].every(Number.isFinite)) return null;
  if (open <= 0 || high <= 0 || low <= 0 || close <= 0 || high < Math.max(open, close) || low > Math.min(open, close)) return null;
  return { timestamp, open, high, low, close, volume: Math.max(0, volume) };
}


function socketDepth(data: unknown): MarketOrderBook | null {
  const value = unwrapData(data);
  if (!value || typeof value !== 'object') return null;
  const source = value as Record<string, unknown>;
  const parseSide = (rows: unknown, reverse: boolean) => {
    if (!Array.isArray(rows)) return [];
    const parsed = rows.flatMap((item) => {
      try {
        const record = item && typeof item === 'object' && !Array.isArray(item) ? item as Record<string, unknown> : null;
        const price = Number(record ? record.p ?? record.price : (item as unknown[])[0]);
        const quantity = Number(record ? record.m ?? record.quantity : (item as unknown[])[1]);
        if (!Number.isFinite(price) || !Number.isFinite(quantity) || price <= 0 || quantity < 0) return [];
        return [{ price, quantity }];
      } catch { return []; }
    });
    parsed.sort((a, b) => reverse ? b.price - a.price : a.price - b.price);
    return parsed.slice(0, 100);
  };
  return {
    asks: parseSide(source.ask ?? source.asks, false),
    bids: parseSide(source.bids ?? source.bid, true),
    timestamp: Number(source.ts ?? source.timestamp ?? Date.now()),
  };
}

function socketTrades(data: unknown): MarketTrade[] {
  const value = unwrapData(data);
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== 'object') return [];
    const row = item as Record<string, unknown>;
    const timestamp = Number(row.createdDate ?? row.timestamp ?? row.ts ?? Date.now());
    const price = Number(row.price);
    const quantity = Number(row.quantity ?? 0);
    const directionRaw = String(row.direction || '').toLowerCase();
    const direction: MarketTrade['direction'] = directionRaw === 'long' ? 'long' : directionRaw === 'short' ? 'short' : 'unknown';
    if (![timestamp, price, quantity].every(Number.isFinite) || price <= 0 || quantity < 0) return [];
    return [{
      id: String(row.id ?? `${timestamp}:${price}:${quantity}`),
      timestamp,
      price,
      quantity,
      piece: Number(row.piece ?? 0) || 0,
      direction,
    }];
  });
}

function socketTicker(data: unknown): MarketTicker | null {
  const value = unwrapData(data);
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const optional = (...keys: string[]) => {
    for (const key of keys) {
      const parsed = Number(row[key]);
      if (Number.isFinite(parsed)) return parsed;
    }
    return null;
  };
  return {
    last: optional('last', 'last_price'), high: optional('high'), low: optional('low'), open: optional('open'),
    change_rate: optional('changeRate', 'rise_fall_rate'), volume: optional('vol', 'total_volume'),
    volume_quote: optional('volUsdt', 'volValue'), max_leverage: optional('maxLeverage', 'max_leverage'),
    contract_size: optional('contractSize', 'contract_size'), index_price: optional('fair_price', 'indexPrice'),
  };
}

function toMarketCandle(bar: KLineData): MarketCandle {
  return { timestamp: bar.timestamp, open: bar.open, high: bar.high, low: bar.low, close: bar.close, volume: Number(bar.volume || 0) };
}

function mergeCandle(list: MarketCandle[], next: MarketCandle): MarketCandle[] {
  const existing = list.findIndex((item) => item.timestamp === next.timestamp);
  if (existing >= 0) {
    const copy = list.slice();
    copy[existing] = next;
    return copy.slice(-600);
  }
  return [...list, next].sort((a, b) => a.timestamp - b.timestamp).slice(-600);
}

function positionTimestamp(value: unknown): number | null {
  const raw = typeof value === 'number' ? value : value ? new Date(String(value)).getTime() : NaN;
  if (!Number.isFinite(raw)) return null;
  return raw < 100_000_000_000 ? raw * 1000 : raw;
}

function positionPnl(position: Position): number | null {
  return toNumber(position.net_pnl ?? position.realized_pnl ?? position.unrealized_pnl);
}

function openedTimestamp(position: Position | undefined): number {
  const value = position?.opened_at ?? position?.created_at;
  const raw = typeof value === 'number' ? value : value ? new Date(value).getTime() : Date.now();
  if (!Number.isFinite(raw)) return Date.now();
  return raw < 100_000_000_000 ? raw * 1000 : raw;
}

function estimatedBreakEven(position: Position | undefined): { value: number; estimated: boolean } | null {
  if (!position) return null;
  const explicit = toNumber(position.break_even_price ?? position.breakeven_price ?? position.breakEvenPrice);
  if (explicit && explicit > 0) return { value: explicit, estimated: false };

  const entry = toNumber(position.entry_price);
  const quantity = toNumber(position.quantity);
  const entryFee = toNumber(position.entry_fee);
  if (!entry || entry <= 0 || !quantity || quantity === 0 || entryFee == null || entryFee === 0) return null;
  const notional = Math.abs(quantity * entry);
  if (!notional) return null;
  // Estimate the exit fee using the effective entry fee rate. It is marked as
  // estimated because maker/taker fees can differ at exit.
  const rate = Math.abs(entryFee) / notional;
  if (!Number.isFinite(rate) || rate <= 0 || rate >= 0.02) return null;
  const side = String(position.side ?? position.direction ?? 'LONG').toUpperCase();
  const value = side === 'SHORT'
    ? entry * (1 - rate) / (1 + rate)
    : entry * (1 + rate) / (1 - rate);
  return Number.isFinite(value) && value > 0 ? { value, estimated: true } : null;
}

function getTradeLevels(position: Position | undefined): TradeLevel[] {
  if (!position) return [];
  const entry = toNumber(position.entry_price);
  const stop = toNumber(position.stop_price ?? position.stop_loss);
  const tp1 = toNumber(position.tp1_price ?? position.tp1);
  const tp2 = toNumber(position.tp2_price ?? position.tp2 ?? position.target_price ?? position.take_profit);
  const levels: TradeLevel[] = [];
  if (entry && entry > 0) levels.push({ key: 'entry', label: 'ENTRY', value: entry, tone: 'entry', color: '#3cc9ff' });
  const breakEven = estimatedBreakEven(position);
  if (breakEven && (!entry || Math.abs(breakEven.value - entry) > Number.EPSILON)) {
    levels.push({ key: 'be', label: breakEven.estimated ? 'BE EST.' : 'BE', value: breakEven.value, tone: 'be', color: '#f4c95d' });
  }
  if (stop && stop > 0) levels.push({ key: 'sl', label: 'SL', value: stop, tone: 'sl', color: '#ff486e' });
  if (tp1 && tp1 > 0) levels.push({ key: 'tp1', label: 'TP1', value: tp1, tone: 'tp', color: '#00e697' });
  if (tp2 && tp2 > 0 && (!tp1 || Math.abs(tp2 - tp1) > Number.EPSILON)) {
    levels.push({ key: 'tp2', label: tp1 ? 'TP2' : 'TP', value: tp2, tone: 'tp', color: '#00e697' });
  }
  return levels;
}

const BASE_STYLES = {
  grid: {
    show: true,
    horizontal: { show: true, size: 1, color: '#102d3a', style: 'dashed', dashedValue: [3, 4] },
    vertical: { show: true, size: 1, color: '#0d2733', style: 'dashed', dashedValue: [3, 4] },
  },
  candle: {
    type: 'candle_solid',
    bar: {
      compareRule: 'current_open',
      upColor: '#00df91',
      downColor: '#ff466d',
      noChangeColor: '#7793a1',
      upBorderColor: '#00df91',
      downBorderColor: '#ff466d',
      noChangeBorderColor: '#7793a1',
      upWickColor: '#00df91',
      downWickColor: '#ff466d',
      noChangeWickColor: '#7793a1',
    },
    area: {
      lineSize: 2,
      lineColor: '#32c7ff',
      smooth: true,
      value: 'close',
      backgroundColor: [
        { offset: 0, color: 'rgba(50,199,255,.24)' },
        { offset: 1, color: 'rgba(50,199,255,.01)' },
      ],
    },
    priceMark: {
      last: {
        show: true,
        line: { show: true, style: 'dashed', dashedValue: [4, 3], size: 1 },
        text: { show: true, color: '#00150f', size: 11, paddingLeft: 4, paddingRight: 4, paddingTop: 2, paddingBottom: 2 },
      },
      high: { show: true, color: '#829eac' },
      low: { show: true, color: '#829eac' },
    },
    tooltip: { showRule: 'follow_cross', showType: 'standard' },
  },
  indicator: {
    tooltip: { showRule: 'follow_cross', showType: 'standard' },
  },
  xAxis: {
    axisLine: { show: true, color: '#173847', size: 1 },
    tickLine: { show: true, color: '#173847', size: 1, length: 3 },
    tickText: { show: true, color: '#6f8997', size: 10 },
  },
  yAxis: {
    axisLine: { show: true, color: '#173847', size: 1 },
    tickLine: { show: true, color: '#173847', size: 1, length: 3 },
    tickText: { show: true, color: '#91a8b5', size: 10 },
  },
  separator: { size: 1, color: '#173847', fill: true, activeBackgroundColor: 'rgba(50,199,255,.06)' },
  crosshair: {
    show: true,
    horizontal: {
      show: true,
      line: { show: true, style: 'dashed', dashedValue: [4, 3], size: 1, color: '#628391' },
      text: { show: true, color: '#d9edf5', backgroundColor: '#12313f', borderColor: '#23536a', borderSize: 1, size: 10 },
    },
    vertical: {
      show: true,
      line: { show: true, style: 'dashed', dashedValue: [4, 3], size: 1, color: '#628391' },
      text: { show: true, color: '#d9edf5', backgroundColor: '#12313f', borderColor: '#23536a', borderSize: 1, size: 10 },
    },
  },
};

export default function LiveMarketChart({ positions, closedPositions = [] }: { positions: Position[]; closedPositions?: Position[] }) {
  const firstPositionSymbol = positions.length ? positionSymbol(positions[0]) : 'BTCUSDT';
  const [selected, setSelected] = useState<MarketInstrument>({
    symbol: firstPositionSymbol,
    display: displaySymbol(firstPositionSymbol),
    base: pairCode(firstPositionSymbol),
    quote: firstPositionSymbol.endsWith('_USDC') ? 'USDC' : 'USDT',
    pair_code: pairCode(firstPositionSymbol),
    price_precision: 6,
    status: 'online',
  });
  const [timeframe, setTimeframe] = useState<TimeframeKey>('5m');
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<MarketInstrument[]>([]);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searching, setSearching] = useState(false);
  const [streamState, setStreamState] = useState<StreamState>('connecting');
  const [chartError, setChartError] = useState('');
  const [lastPrice, setLastPrice] = useState<number | null>(() => {
    const matching = positions.find((p) => positionSymbol(p) === firstPositionSymbol);
    return toNumber(matching?.current_price);
  });
  const [markPrice, setMarkPrice] = useState<number | null>(null);
  const [indexPrice, setIndexPrice] = useState<number | null>(null);
  const [fundingRate, setFundingRate] = useState<number | null>(null);
  const [fundingTimestamp, setFundingTimestamp] = useState<number | null>(null);
  const [fundingTimestampKind, setFundingTimestampKind] = useState<'next' | 'last' | null>(null);
  const [orderBook, setOrderBook] = useState<MarketOrderBook>(EMPTY_ORDER_BOOK);
  const [trades, setTrades] = useState<MarketTrade[]>([]);
  const [ticker, setTicker] = useState<MarketTicker>({});
  const [candleHistory, setCandleHistory] = useState<MarketCandle[]>([]);
  const [chartMode, setChartMode] = useState<ChartMode>('candle');
  const [mainIndicator, setMainIndicator] = useState<MainIndicator>('NONE');
  const [subIndicator, setSubIndicator] = useState<SubIndicator>('VOL');
  const [fullscreen, setFullscreen] = useState(false);
  const [indicatorSettingsOpen, setIndicatorSettingsOpen] = useState(false);
  const [mainParamsText, setMainParamsText] = useState('');
  const [subParamsText, setSubParamsText] = useState(INDICATOR_DEFAULTS.VOL.join(', '));
  const [alertInput, setAlertInput] = useState('');
  const [alerts, setAlerts] = useState<PriceAlert[]>([]);
  const [alertNotice, setAlertNotice] = useState('');

  const rootRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);
  const chartApiRef = useRef<KChart | null>(null);
  const cleanupMapRef = useRef(new Map<string, () => void>());
  const mainIndicatorIdRef = useRef<string | null>(null);
  const subIndicatorIdRef = useRef<string | null>(null);
  const manualSelectionRef = useRef(false);
  const lastPrimaryPositionRef = useRef('');
  const lastDepthPaintRef = useRef(0);

  const activePosition = useMemo(
    () => positions.find((position) => positionSymbol(position) === canonicalSymbol(selected.symbol)),
    [positions, selected.symbol],
  );
  const tradeLevels = useMemo(() => getTradeLevels(activePosition), [activePosition]);

  const selectInstrument = useCallback((instrument: MarketInstrument) => {
    manualSelectionRef.current = true;
    setSelected(instrument);
    setQuery('');
    setSearchOpen(false);
    setLastPrice(null);
    setMarkPrice(null);
    setIndexPrice(null);
    setFundingRate(null);
    setFundingTimestamp(null);
    setFundingTimestampKind(null);
    setOrderBook(EMPTY_ORDER_BOOK);
    setTrades([]);
    setTicker({});
    setCandleHistory([]);
    setChartError('');
  }, []);

  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      setSearching(true);
      try {
        const response = await api.marketInstruments(query.trim(), query.trim() ? 80 : 60);
        if (!cancelled) setResults(response.items || []);
      } catch (error) {
        if (!cancelled) {
          setResults([]);
          setChartError(humanizeError(error));
        }
      } finally {
        if (!cancelled) setSearching(false);
      }
    }, query.trim() ? 220 : 0);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [query]);


  useEffect(() => {
    let cancelled = false;
    api.marketSnapshot(selected.symbol).then((snapshot) => {
      if (cancelled) return;
      setOrderBook(snapshot.order_book || EMPTY_ORDER_BOOK);
      setTrades(snapshot.trades || []);
      setTicker(snapshot.ticker || {});
      if (snapshot.ticker?.last != null) setLastPrice(Number(snapshot.ticker.last));
      if (snapshot.ticker?.index_price != null) setIndexPrice(Number(snapshot.ticker.index_price));
      if (snapshot.funding?.rate != null) setFundingRate(Number(snapshot.funding.rate));
      if (snapshot.funding?.timestamp != null) {
        setFundingTimestamp(Number(snapshot.funding.timestamp));
        setFundingTimestampKind(snapshot.funding.kind === 'last_settlement' ? 'last' : null);
      }
    }).catch(() => {
      // The websocket can still populate all live fields. Snapshot failures are
      // deliberately non-fatal so the candle chart does not disappear.
    });
    return () => { cancelled = true; };
  }, [selected.symbol]);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(`${ALERT_STORAGE_PREFIX}${canonicalSymbol(selected.symbol)}`);
      const parsed = raw ? JSON.parse(raw) : [];
      setAlerts(Array.isArray(parsed) ? parsed.filter((item): item is PriceAlert => item && Number.isFinite(Number(item.price))) : []);
    } catch { setAlerts([]); }
    setAlertInput('');
    setAlertNotice('');
  }, [selected.symbol]);

  useEffect(() => {
    if (lastPrice == null || !Number.isFinite(lastPrice)) return;
    let changed = false;
    const next = alerts.map((alert) => {
      if (alert.triggered) return alert;
      const hit = alert.direction === 'above' ? lastPrice >= alert.price : lastPrice <= alert.price;
      if (!hit) return alert;
      changed = true;
      const message = `${displaySymbol(selected.symbol)} alcanzó ${formatPrice(alert.price, selected.price_precision)}`;
      setAlertNotice(`Alerta: ${message}`);
      try {
        if ('Notification' in window && Notification.permission === 'granted') {
          new Notification('KAELEON · Alerta de precio', { body: message });
        }
      } catch { /* browser notifications are optional */ }
      return { ...alert, triggered: true };
    });
    if (changed) setAlerts(next);
  }, [alerts, lastPrice, selected.price_precision, selected.symbol]);

  useEffect(() => {
    try { localStorage.setItem(`${ALERT_STORAGE_PREFIX}${canonicalSymbol(selected.symbol)}`, JSON.stringify(alerts)); } catch { /* storage unavailable */ }
  }, [alerts, selected.symbol]);

  const addPriceAlert = useCallback(() => {
    const price = Number(alertInput);
    if (!Number.isFinite(price) || price <= 0) {
      setAlertNotice('Introduce un precio válido para crear la alerta.');
      return;
    }
    const reference = lastPrice ?? toNumber(activePosition?.current_price) ?? price;
    const direction: PriceAlert['direction'] = price >= reference ? 'above' : 'below';
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
    setAlerts((current) => [...current.filter((item) => Math.abs(item.price - price) > Number.EPSILON), { id, price, direction, triggered: false }].slice(-8));
    try {
      if ('Notification' in window && Notification.permission === 'default') void Notification.requestPermission();
    } catch { /* keep the in-app alert even if notifications are unsupported */ }
    setAlertInput('');
    setAlertNotice(`Alerta creada para ${direction === 'above' ? 'subida' : 'bajada'} a ${formatPrice(price, selected.price_precision)}.`);
  }, [activePosition?.current_price, alertInput, lastPrice, selected.price_precision]);

  const followActiveOperation = useCallback(async () => {
    if (!positions.length) return;
    manualSelectionRef.current = false;
    const expected = canonicalSymbol(positionSymbol(positions[0]));
    const position = positions[0];
    const fallback: MarketInstrument = {
      symbol: expected,
      display: displaySymbol(expected),
      base: pairCode(expected),
      quote: expected.endsWith('_USDC') ? 'USDC' : 'USDT',
      pair_code: pairCode(expected),
      price_precision: selected.price_precision || 6,
      status: 'online',
    };
    setSelected(fallback);
    setLastPrice(toNumber(position.current_price));
    setMarkPrice(null);
    setIndexPrice(null);
    setFundingRate(null);
    setFundingTimestamp(null);
    setFundingTimestampKind(null);
    setOrderBook(EMPTY_ORDER_BOOK);
    setTrades([]);
    setTicker({});
    setCandleHistory([]);
    setChartError('');
    try {
      const response = await api.marketInstruments(expected, 25);
      const exact = response.items.find((item) => canonicalSymbol(item.symbol) === expected);
      if (exact && !manualSelectionRef.current) setSelected(exact);
    } catch {
      // The chart can still load with the canonical symbol; data loading will surface errors.
    }
  }, [positions, selected.price_precision]);

  const primaryPositionKey = positions.length
    ? `${String(positions[0].position_id || positions[0].opened_at || positions[0].created_at || '')}:${firstPositionSymbol}`
    : '';

  useEffect(() => {
    if (!positions.length) {
      lastPrimaryPositionRef.current = '';
      return;
    }

    const isNewPrimaryOperation = Boolean(primaryPositionKey) && primaryPositionKey !== lastPrimaryPositionRef.current;
    if (isNewPrimaryOperation) {
      lastPrimaryPositionRef.current = primaryPositionKey;
      manualSelectionRef.current = false;
      void followActiveOperation();
      return;
    }

    if (manualSelectionRef.current) return;
    const expected = canonicalSymbol(firstPositionSymbol);
    if (canonicalSymbol(selected.symbol) !== expected) {
      void followActiveOperation();
      return;
    }

    // Enrich the fallback symbol with CoinW precision/icon metadata once the
    // exchange catalogue is available. Do not take control back from a user
    // who intentionally searched another market.
    if (selected.icon_url || selected.price_precision !== 6) return;
    let cancelled = false;
    api.marketInstruments(expected, 25).then((response) => {
      if (cancelled || manualSelectionRef.current) return;
      const exact = response.items.find((item) => canonicalSymbol(item.symbol) === expected);
      if (exact) setSelected(exact);
    }).catch(() => undefined);
    return () => { cancelled = true; };
  }, [firstPositionSymbol, followActiveOperation, positions.length, primaryPositionKey, selected.icon_url, selected.price_precision, selected.symbol]);

  useEffect(() => {
    const onFullscreen = () => setFullscreen(document.fullscreenElement === rootRef.current);
    document.addEventListener('fullscreenchange', onFullscreen);
    return () => document.removeEventListener('fullscreenchange', onFullscreen);
  }, []);

  const toggleFullscreen = useCallback(async () => {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await rootRef.current?.requestFullscreen();
    } catch {
      setChartError('El navegador no permitió abrir el gráfico en pantalla completa.');
    }
  }, []);

  useEffect(() => {
    const container = chartRef.current;
    if (!container) return;

    const chart = init(container, {
      layout: {
        barSpaceLimit: { min: 2, max: 38 },
        pane: { minHeight: 64, dragEnabled: true },
        yAxis: { position: 'right', inside: false, scrollZoomEnabled: true },
      },
      styles: BASE_STYLES as any,
    });
    if (!chart) {
      setChartError('No se pudo inicializar el gráfico.');
      return;
    }
    chartApiRef.current = chart;

    chart.setDataLoader({
      async getBars({ type, symbol, period, callback }) {
        if (type !== 'init') {
          callback([], false);
          return;
        }
        setChartError('');
        try {
          const response = await api.marketCandles(symbol.ticker, timeframeFromPeriod(period), 500);
          const sourceCandles = (response.items || []).slice().sort((a, b) => Number(a.timestamp) - Number(b.timestamp));
          setCandleHistory(sourceCandles);
          const bars = sourceCandles.map(toKLineData);
          const latest = bars.at(-1);
          if (latest) setLastPrice(latest.close);
          callback(bars, false);
          if (!bars.length) setChartError('CoinW no devolvió velas para este mercado y temporalidad.');
        } catch (error) {
          setChartError(humanizeError(error));
          callback([], false);
        }
      },
      subscribeBar({ symbol, period, callback }) {
        const key = subscriptionKey(symbol, period);
        cleanupMapRef.current.get(key)?.();

        let stopped = false;
        let socket: WebSocket | null = null;
        let reconnectTimer: number | null = null;
        let pollTimer: number | null = null;
        let staleTimer: number | null = null;
        let attempt = 0;
        let lastSocketMessageAt = Date.now();
        const normalizedSymbol = canonicalSymbol(symbol.ticker);
        const currentTimeframe = timeframeFromPeriod(period);
        const interval = wsInterval(period);
        const currentPairCode = pairCode(normalizedSymbol);

        const pushLatestViaRest = async () => {
          if (stopped) return;
          try {
            const response = await api.marketCandles(normalizedSymbol, currentTimeframe, 80);
            const latest = response.items?.at(-1);
            if (latest) {
              const bar = toKLineData(latest);
              callback(bar);
              setLastPrice(bar.close);
              setCandleHistory((current) => mergeCandle(current, toMarketCandle(bar)));
              setChartError('');
            }
          } catch (error) {
            if (!stopped) {
              setStreamState('offline');
              setChartError(humanizeError(error));
            }
          }
        };

        const stopPolling = () => {
          if (pollTimer != null) window.clearInterval(pollTimer);
          pollTimer = null;
        };

        const startPolling = () => {
          if (stopped || pollTimer != null) return;
          setStreamState('polling');
          void pushLatestViaRest();
          pollTimer = window.setInterval(() => void pushLatestViaRest(), FALLBACK_POLL_MS);
        };

        const scheduleReconnect = () => {
          if (stopped || reconnectTimer != null) return;
          startPolling();
          setStreamState(attempt ? 'reconnecting' : 'connecting');
          const delay = Math.min(WS_RECONNECT_MAX_MS, 1_000 * 2 ** Math.min(attempt, 4));
          attempt += 1;
          reconnectTimer = window.setTimeout(() => {
            reconnectTimer = null;
            connect();
          }, delay);
        };

        const connect = () => {
          if (stopped) return;
          setStreamState(attempt ? 'reconnecting' : 'connecting');
          try {
            socket = new WebSocket(COINW_FUTURES_WS);
          } catch {
            scheduleReconnect();
            return;
          }

          socket.onopen = () => {
            if (stopped || !socket) return;
            attempt = 0;
            lastSocketMessageAt = Date.now();
            stopPolling();
            setStreamState('live');
            setChartError('');
            const channels = [
              { type: 'candles_swap_utc', interval },
              { type: 'mark_price' },
              { type: 'index_price' },
              { type: 'funding_rate' },
              { type: 'depth' },
              { type: 'fills' },
              { type: 'ticker_swap' },
            ];
            for (const channel of channels) {
              socket.send(JSON.stringify({ event: 'sub', params: { biz: 'futures', pairCode: currentPairCode, ...channel } }));
            }
          };

          socket.onmessage = (event) => {
            if (stopped) return;
            const payload = unwrapSocketPayload(event.data);
            if (!payload) return;
            lastSocketMessageAt = Date.now();
            if (payload.type === 'candles_swap_utc') {
              const bar = socketCandle(payload.data);
              if (bar) {
                callback(bar);
                setLastPrice(bar.close);
                setCandleHistory((current) => mergeCandle(current, toMarketCandle(bar)));
                setStreamState('live');
              }
            } else if (payload.type === 'mark_price' || payload.type === 'index_price') {
              const data = unwrapData(payload.data);
              if (data && typeof data === 'object') {
                const value = Number((data as Record<string, unknown>).p);
                if (Number.isFinite(value) && value > 0) {
                  if (payload.type === 'mark_price') setMarkPrice(value);
                  else setIndexPrice(value);
                }
              }
            } else if (payload.type === 'funding_rate') {
              const data = unwrapData(payload.data);
              if (data && typeof data === 'object') {
                const row = data as Record<string, unknown>;
                const rate = Number(row.r);
                const nextFunding = Number(row.nt);
                const streamTimestamp = Number(row.ts);
                if (Number.isFinite(rate)) setFundingRate(rate);
                if (Number.isFinite(nextFunding) && nextFunding > 0) {
                  setFundingTimestamp(nextFunding);
                  setFundingTimestampKind('next');
                } else if (Number.isFinite(streamTimestamp) && streamTimestamp > 0) {
                  setFundingTimestamp(streamTimestamp);
                  setFundingTimestampKind('last');
                }
              }
            } else if (payload.type === 'depth') {
              const now = Date.now();
              if (now - lastDepthPaintRef.current >= 180) {
                const book = socketDepth(payload.data);
                if (book) {
                  lastDepthPaintRef.current = now;
                  setOrderBook(book);
                }
              }
            } else if (payload.type === 'fills') {
              const incoming = socketTrades(payload.data);
              if (incoming.length) {
                setTrades((current) => {
                  const unique = new Map<string, MarketTrade>();
                  [...incoming, ...current].forEach((trade) => unique.set(String(trade.id), trade));
                  return [...unique.values()].sort((a, b) => b.timestamp - a.timestamp).slice(0, 60);
                });
              }
            } else if (payload.type === 'ticker_swap') {
              const nextTicker = socketTicker(payload.data);
              if (nextTicker) {
                setTicker((current) => ({ ...current, ...nextTicker }));
                if (nextTicker.last != null && Number.isFinite(nextTicker.last)) setLastPrice(nextTicker.last);
                if (nextTicker.index_price != null && Number.isFinite(nextTicker.index_price)) setIndexPrice(nextTicker.index_price);
              }
            }
          };

          socket.onerror = () => {
            if (!stopped) setStreamState('reconnecting');
          };
          socket.onclose = () => {
            socket = null;
            scheduleReconnect();
          };
        };

        staleTimer = window.setInterval(() => {
          if (stopped || !socket || socket.readyState !== WebSocket.OPEN) return;
          if (Date.now() - lastSocketMessageAt > WS_STALE_AFTER_MS) socket.close();
        }, 8_000);

        connect();
        const cleanup = () => {
          stopped = true;
          stopPolling();
          if (reconnectTimer != null) window.clearTimeout(reconnectTimer);
          if (staleTimer != null) window.clearInterval(staleTimer);
          reconnectTimer = null;
          staleTimer = null;
          if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
          socket = null;
          cleanupMapRef.current.delete(key);
        };
        cleanupMapRef.current.set(key, cleanup);
      },
      unsubscribeBar({ symbol, period }) {
        const key = subscriptionKey(symbol, period);
        cleanupMapRef.current.get(key)?.();
      },
    });

    chart.setSymbol({ ticker: canonicalSymbol(selected.symbol), pricePrecision: selected.price_precision || 6, volumePrecision: 4 });
    chart.setPeriod(TIMEFRAMES.find((item) => item.key === timeframe)?.period || TIMEFRAMES[2].period);

    return () => {
      cleanupMapRef.current.forEach((cleanup) => cleanup());
      cleanupMapRef.current.clear();
      chartApiRef.current = null;
      dispose(container);
    };
  }, []);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    setMarkPrice(null);
    setChartError('');
    chart.setSymbol({ ticker: canonicalSymbol(selected.symbol), pricePrecision: selected.price_precision || 6, volumePrecision: 4 });
  }, [selected.symbol, selected.price_precision]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    const period = TIMEFRAMES.find((item) => item.key === timeframe)?.period || TIMEFRAMES[2].period;
    chart.setPeriod(period);
  }, [timeframe]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    chart.setStyles({ candle: { type: chartMode === 'area' ? 'area' : 'candle_solid' } });
  }, [chartMode]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    if (mainIndicatorIdRef.current) chart.removeIndicator({ id: mainIndicatorIdRef.current });
    if (mainIndicator === 'NONE') {
      mainIndicatorIdRef.current = null;
      setMainParamsText('');
      return;
    }
    const params = INDICATOR_DEFAULTS[mainIndicator] || [];
    setMainParamsText(params.join(', '));
    mainIndicatorIdRef.current = chart.createIndicator({ name: mainIndicator, paneId: 'candle_pane', calcParams: params });
  }, [mainIndicator]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    if (subIndicatorIdRef.current) chart.removeIndicator({ id: subIndicatorIdRef.current });
    const params = INDICATOR_DEFAULTS[subIndicator] || [];
    setSubParamsText(params.join(', '));
    subIndicatorIdRef.current = chart.createIndicator({ name: subIndicator, paneId: 'kaeleon_sub_pane', calcParams: params });
  }, [subIndicator]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    chart.removeOverlay({ groupId: TRADE_LEVEL_GROUP });
    const anchor = openedTimestamp(activePosition);
    for (const level of tradeLevels) {
      chart.createOverlay({
        name: TRADE_LEVEL_OVERLAY,
        groupId: TRADE_LEVEL_GROUP,
        lock: true,
        fixedZLevel: true,
        points: [{ timestamp: anchor, value: level.value }],
        extendData: {
          label: level.label,
          tone: level.tone,
          color: level.color,
          priceText: formatPrice(level.value, selected.price_precision),
        },
      });
    }
  }, [activePosition, selected.price_precision, tradeLevels]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    chart.removeOverlay({ groupId: RISK_ZONE_GROUP });
    if (!activePosition) return;
    const entry = toNumber(activePosition.entry_price);
    const stop = toNumber(activePosition.stop_price ?? activePosition.stop_loss);
    const target = toNumber(activePosition.tp2_price ?? activePosition.tp2 ?? activePosition.target_price ?? activePosition.take_profit ?? activePosition.tp1_price ?? activePosition.tp1);
    const anchor = openedTimestamp(activePosition);
    if (entry && target && entry > 0 && target > 0) {
      chart.createOverlay({
        name: RISK_ZONE_OVERLAY, groupId: RISK_ZONE_GROUP, lock: true, zLevel: -10,
        points: [{ timestamp: anchor, value: entry }, { timestamp: anchor, value: target }],
        extendData: { label: 'PROFIT', color: 'rgba(0,230,151,.055)' },
      });
    }
    if (entry && stop && entry > 0 && stop > 0) {
      chart.createOverlay({
        name: RISK_ZONE_OVERLAY, groupId: RISK_ZONE_GROUP, lock: true, zLevel: -10,
        points: [{ timestamp: anchor, value: entry }, { timestamp: anchor, value: stop }],
        extendData: { label: 'RISK', color: 'rgba(255,72,110,.050)' },
      });
    }
  }, [activePosition]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    chart.removeOverlay({ groupId: HISTORY_GROUP });
    const selectedSymbol = canonicalSymbol(selected.symbol);
    const matching = closedPositions.filter((position) => positionSymbol(position) === selectedSymbol).slice(0, 24);
    for (const position of matching) {
      const entry = toNumber(position.entry_price);
      const exit = toNumber(position.exit_price ?? position.current_price);
      const opened = positionTimestamp(position.opened_at ?? position.created_at);
      const closed = positionTimestamp(position.closed_at);
      const side = String(position.side ?? position.direction ?? 'LONG').toUpperCase();
      if (entry && opened) {
        chart.createOverlay({
          name: HISTORY_OVERLAY, groupId: HISTORY_GROUP, lock: true,
          points: [{ timestamp: opened, value: entry }],
          extendData: { label: side === 'SHORT' ? '▼ SHORT' : '▲ LONG', color: side === 'SHORT' ? '#ff647f' : '#30d8ff', placement: side === 'SHORT' ? 'top' : 'bottom' },
        });
      }
      if (exit && closed) {
        const pnl = positionPnl(position);
        chart.createOverlay({
          name: HISTORY_OVERLAY, groupId: HISTORY_GROUP, lock: true,
          points: [{ timestamp: closed, value: exit }],
          extendData: { label: `${(pnl ?? 0) >= 0 ? '✓' : '×'} ${pnl == null ? 'CLOSE' : `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(2)}`}`, color: (pnl ?? 0) >= 0 ? '#00e697' : '#ff486e', placement: (pnl ?? 0) >= 0 ? 'top' : 'bottom' },
        });
      }
    }
  }, [closedPositions, selected.symbol]);

  const applyMainParams = useCallback(() => {
    const chart = chartApiRef.current;
    if (!chart || mainIndicator === 'NONE' || !mainIndicatorIdRef.current) return;
    const params = indicatorParams(mainParamsText, INDICATOR_DEFAULTS[mainIndicator] || []);
    setMainParamsText(params.join(', '));
    chart.overrideIndicator({ id: mainIndicatorIdRef.current, calcParams: params });
  }, [mainIndicator, mainParamsText]);

  const applySubParams = useCallback(() => {
    const chart = chartApiRef.current;
    if (!chart || !subIndicatorIdRef.current) return;
    const params = indicatorParams(subParamsText, INDICATOR_DEFAULTS[subIndicator] || []);
    setSubParamsText(params.join(', '));
    chart.overrideIndicator({ id: subIndicatorIdRef.current, calcParams: params });
  }, [subIndicator, subParamsText]);

  const startDrawing = useCallback((tool: DrawingTool) => {
    chartApiRef.current?.createOverlay({ name: tool, groupId: DRAWING_GROUP });
  }, []);

  const clearDrawings = useCallback(() => {
    chartApiRef.current?.removeOverlay({ groupId: DRAWING_GROUP });
  }, []);

  const livePrice = lastPrice ?? toNumber(activePosition?.current_price);
  const statusLabel = streamState === 'live'
    ? 'LIVE'
    : streamState === 'polling'
      ? 'REST FALLBACK'
      : streamState === 'offline'
        ? 'OFFLINE'
        : streamState === 'reconnecting'
          ? 'RECONECTANDO'
          : 'CONECTANDO';
  const rawChange = ticker.change_rate == null ? null : Number(ticker.change_rate);
  const changePct = rawChange == null || !Number.isFinite(rawChange) ? null : (Math.abs(rawChange) <= 1 ? rawChange * 100 : rawChange);
  const bestAsk = orderBook.asks[0]?.price ?? null;
  const bestBid = orderBook.bids[0]?.price ?? null;
  const spread = bestAsk != null && bestBid != null ? Math.max(0, bestAsk - bestBid) : null;

  return (
    <section className={`live-market-chart ${fullscreen ? 'is-fullscreen' : ''}`} ref={rootRef} aria-label="Gráfico de mercado CoinW">
      <div className="market-chart-header">
        <div className="market-chart-symbol">
          <span className="market-chart-token">{selected.base.slice(0, 2)}</span>
          <div>
            <strong>{selected.display}</strong>
            <small>CoinW Perpetual · {selected.quote}</small>
          </div>
          {activePosition && <b className={String(activePosition.side || activePosition.direction || '').toUpperCase() === 'SHORT' ? 'short' : 'long'}>{String(activePosition.side || activePosition.direction || 'LONG').toUpperCase()}</b>}
        </div>
        <div className="market-chart-prices">
          <span><small>Last</small><strong>{formatPrice(livePrice, selected.price_precision)}</strong></span>
          <span><small>Mark</small><strong>{formatPrice(markPrice, selected.price_precision)}</strong></span>
          <span><small>Index</small><strong>{formatPrice(indexPrice, selected.price_precision)}</strong></span>
          <span className={`market-stream-state ${streamState}`}><i />{statusLabel}</span>
        </div>
      </div>

      <div className="market-quick-stats">
        <span><small>Funding</small><strong className={(fundingRate ?? 0) >= 0 ? 'positive' : 'negative'}>{fundingRate == null ? '—' : `${fundingRate >= 0 ? '+' : ''}${(fundingRate * 100).toFixed(4)}%`}</strong></span>
        <span><small>24h</small><strong className={(changePct ?? 0) >= 0 ? 'positive' : 'negative'}>{changePct == null ? '—' : `${changePct >= 0 ? '+' : ''}${changePct.toFixed(2)}%`}</strong></span>
        <span><small>High</small><strong>{formatPrice(ticker.high ?? null, selected.price_precision)}</strong></span>
        <span><small>Low</small><strong>{formatPrice(ticker.low ?? null, selected.price_precision)}</strong></span>
        <span><small>Spread</small><strong>{formatPrice(spread, selected.price_precision)}</strong></span>
      </div>

      {positions.length > 0 && !activePosition && (
        <div className="market-operation-jump-row">
          <span>Estás explorando otro mercado.</span>
          <button type="button" onClick={() => void followActiveOperation()}>Ver operación {displaySymbol(firstPositionSymbol)}</button>
        </div>
      )}

      <div className="market-chart-search-wrap">
        <div className="market-chart-search">
          <span aria-hidden="true">⌕</span>
          <input
            value={query}
            placeholder="Buscar cualquier par de futuros en CoinW…"
            aria-label="Buscar par en CoinW"
            onFocus={() => setSearchOpen(true)}
            onChange={(event) => { setQuery(event.target.value); setSearchOpen(true); }}
            onKeyDown={(event) => {
              if (event.key === 'Escape') setSearchOpen(false);
              if (event.key === 'Enter' && results[0]) selectInstrument(results[0]);
            }}
          />
          {searching && <span className="market-searching">Buscando…</span>}
        </div>
        {searchOpen && (
          <div className="market-search-results" role="listbox">
            {results.length ? results.map((instrument) => (
              <button key={instrument.symbol} type="button" onClick={() => selectInstrument(instrument)} role="option" aria-selected={instrument.symbol === selected.symbol}>
                <span className="market-result-token">{instrument.base.slice(0, 2)}</span>
                <span><strong>{instrument.display}</strong><small>{instrument.base} · Perpetual</small></span>
                <em>{instrument.quote}</em>
              </button>
            )) : <div className="market-search-empty">{searching ? 'Consultando mercados…' : 'No se encontraron pares.'}</div>}
          </div>
        )}
      </div>

      <div className="market-chart-toolbar">
        <div className="timeframe-scroll" aria-label="Temporalidad">
          {TIMEFRAMES.map((item) => <button key={item.key} type="button" className={timeframe === item.key ? 'active' : ''} aria-pressed={timeframe === item.key} onClick={() => setTimeframe(item.key)}>{item.label}</button>)}
        </div>
        <div className="chart-tools-right">
          <select value={mainIndicator} onChange={(event) => setMainIndicator(event.target.value as MainIndicator)} aria-label="Indicador principal">
            <option value="NONE">Indicador</option><option value="MA">MA</option><option value="EMA">EMA</option><option value="BOLL">BOLL</option>
          </select>
          <select value={subIndicator} onChange={(event) => setSubIndicator(event.target.value as SubIndicator)} aria-label="Indicador inferior">
            <option value="VOL">VOL</option><option value="MACD">MACD</option><option value="RSI">RSI</option>
          </select>
          <select value="" onChange={(event) => { const value = event.target.value as DrawingTool; if (value) startDrawing(value); }} aria-label="Herramientas de dibujo">
            <option value="">Dibujar</option><option value="straightLine">Tendencia</option><option value="segment">Segmento</option><option value="horizontalStraightLine">Horizontal</option><option value="priceLine">Línea de precio</option><option value="priceChannelLine">Canal de precio</option><option value="parallelStraightLine">Paralelas</option><option value="fibonacciLine">Fibonacci</option><option value="simpleAnnotation">Anotación</option><option value="brush">Pincel</option>
          </select>
          <button type="button" onClick={clearDrawings} title="Borrar dibujos">⌫</button>
          <button type="button" className={indicatorSettingsOpen ? 'active' : ''} onClick={() => setIndicatorSettingsOpen((value) => !value)} title="Configurar indicadores">⚙</button>
          <button type="button" className={chartMode === 'candle' ? 'active' : ''} onClick={() => setChartMode(chartMode === 'candle' ? 'area' : 'candle')} title="Cambiar entre velas y área">{chartMode === 'candle' ? '▥ Velas' : '⌁ Área'}</button>
          <button type="button" onClick={() => chartApiRef.current?.scrollToRealTime(250)} title="Volver al precio actual">LIVE</button>
          <button type="button" onClick={() => void toggleFullscreen()} title="Pantalla completa">{fullscreen ? '✕' : '⛶'}</button>
        </div>
      </div>

      {indicatorSettingsOpen && <div className="indicator-settings">
        <div><span>{mainIndicator === 'NONE' ? 'Indicador principal' : mainIndicator}</span><input value={mainParamsText} disabled={mainIndicator === 'NONE'} onChange={(event) => setMainParamsText(event.target.value)} placeholder="ej. 9, 21, 50"/><button type="button" disabled={mainIndicator === 'NONE'} onClick={applyMainParams}>Aplicar</button></div>
        <div><span>{subIndicator}</span><input value={subParamsText} onChange={(event) => setSubParamsText(event.target.value)} placeholder="parámetros"/><button type="button" onClick={applySubParams}>Aplicar</button></div>
        <small>Parámetros separados por comas. Los cambios solo modifican la visualización del gráfico.</small>
      </div>}

      <div className="market-alert-row">
        <div className="market-alert-create"><span>🔔</span><input inputMode="decimal" value={alertInput} onChange={(event) => setAlertInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') addPriceAlert(); }} placeholder="Alerta de precio"/><button type="button" onClick={addPriceAlert}>Crear</button></div>
        <div className="market-alert-chips">{alerts.map((alert) => <button type="button" key={alert.id} className={alert.triggered ? 'triggered' : ''} onClick={() => setAlerts((current) => current.filter((item) => item.id !== alert.id))} title="Eliminar alerta">{alert.direction === 'above' ? '↑' : '↓'} {formatPrice(alert.price, selected.price_precision)} {alert.triggered ? '✓' : '×'}</button>)}</div>
        {alertNotice && <small>{alertNotice}</small>}
      </div>

      {tradeLevels.length > 0 && (
        <div className="trade-level-strip" aria-label="Niveles de la operación activa">
          {tradeLevels.map((level) => <span key={level.key} className={level.tone}><i />{level.label}<strong>{formatPrice(level.value, selected.price_precision)}</strong></span>)}
        </div>
      )}

      <div className="market-chart-canvas-wrap">
        <div className="market-chart-canvas" ref={chartRef} />
        {chartError && <div className="market-chart-error"><strong>Datos de mercado</strong><span>{chartError}</span></div>}
      </div>

      <MarketMicrostructure
        orderBook={orderBook}
        trades={trades}
        ticker={ticker}
        lastPrice={livePrice}
        markPrice={markPrice}
        indexPrice={indexPrice}
        fundingRate={fundingRate}
        fundingTimestamp={fundingTimestamp}
        fundingTimestampKind={fundingTimestampKind}
        precision={selected.price_precision}
        candles={candleHistory}
        activePosition={activePosition}
      />

      <div className="market-chart-footer">
        <span>Velas, Last, Mark, Index, Funding, Order Book y Trades: CoinW</span>
        <span>{activePosition ? 'Niveles ENTRY / TP / SL sincronizados con la operación activa' : 'Selecciona el par de una operación activa para ver ENTRY / TP / SL'}</span>
      </div>
    </section>
  );
}
