import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { dispose, init, registerOverlay } from 'klinecharts';
import { api, humanizeError } from '../../lib/api';
import type { MarketCandle, MarketInstrument, Position } from '../../types';

const COINW_FUTURES_WS = 'wss://ws.futurescw.com/perpum';
const TRADE_LEVEL_GROUP = 'kaeleon-trade-levels';
const TRADE_LEVEL_OVERLAY = 'kaeleonTradeLevel';
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

type TradeLevel = {
  key: string;
  label: string;
  value: number;
  tone: 'entry' | 'tp' | 'sl';
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

function openedTimestamp(position: Position | undefined): number {
  const value = position?.opened_at ?? position?.created_at;
  const raw = typeof value === 'number' ? value : value ? new Date(value).getTime() : Date.now();
  if (!Number.isFinite(raw)) return Date.now();
  return raw < 100_000_000_000 ? raw * 1000 : raw;
}

function getTradeLevels(position: Position | undefined): TradeLevel[] {
  if (!position) return [];
  const entry = toNumber(position.entry_price);
  const stop = toNumber(position.stop_price ?? position.stop_loss);
  const tp1 = toNumber(position.tp1_price ?? position.tp1);
  const tp2 = toNumber(position.tp2_price ?? position.tp2 ?? position.target_price ?? position.take_profit);
  const levels: TradeLevel[] = [];
  if (entry && entry > 0) levels.push({ key: 'entry', label: 'ENTRY', value: entry, tone: 'entry', color: '#3cc9ff' });
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

export default function LiveMarketChart({ positions }: { positions: Position[] }) {
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
  const [chartMode, setChartMode] = useState<ChartMode>('candle');
  const [mainIndicator, setMainIndicator] = useState<MainIndicator>('NONE');
  const [subIndicator, setSubIndicator] = useState<SubIndicator>('VOL');
  const [fullscreen, setFullscreen] = useState(false);

  const rootRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<HTMLDivElement | null>(null);
  const chartApiRef = useRef<KChart | null>(null);
  const cleanupMapRef = useRef(new Map<string, () => void>());
  const mainIndicatorIdRef = useRef<string | null>(null);
  const subIndicatorIdRef = useRef<string | null>(null);
  const manualSelectionRef = useRef(false);
  const lastPrimaryPositionRef = useRef('');

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
          const bars = (response.items || []).map(toKLineData).sort((a, b) => a.timestamp - b.timestamp);
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
            socket.send(JSON.stringify({
              event: 'sub',
              params: { biz: 'futures', interval, pairCode: currentPairCode, type: 'candles_swap_utc' },
            }));
            socket.send(JSON.stringify({
              event: 'sub',
              params: { biz: 'futures', pairCode: currentPairCode, type: 'mark_price' },
            }));
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
                setStreamState('live');
              }
            } else if (payload.type === 'mark_price') {
              const data = unwrapData(payload.data);
              if (data && typeof data === 'object') {
                const value = Number((data as Record<string, unknown>).p);
                if (Number.isFinite(value) && value > 0) setMarkPrice(value);
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
    mainIndicatorIdRef.current = mainIndicator === 'NONE'
      ? null
      : chart.createIndicator({ name: mainIndicator, paneId: 'candle_pane' });
  }, [mainIndicator]);

  useEffect(() => {
    const chart = chartApiRef.current;
    if (!chart) return;
    if (subIndicatorIdRef.current) chart.removeIndicator({ id: subIndicatorIdRef.current });
    subIndicatorIdRef.current = chart.createIndicator({ name: subIndicator, paneId: 'kaeleon_sub_pane' });
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
          <span className={`market-stream-state ${streamState}`}><i />{statusLabel}</span>
        </div>
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
          <button type="button" className={chartMode === 'candle' ? 'active' : ''} onClick={() => setChartMode(chartMode === 'candle' ? 'area' : 'candle')} title="Cambiar entre velas y área">{chartMode === 'candle' ? '▥ Velas' : '⌁ Área'}</button>
          <button type="button" onClick={() => chartApiRef.current?.scrollToRealTime(250)} title="Volver al precio actual">LIVE</button>
          <button type="button" onClick={() => void toggleFullscreen()} title="Pantalla completa">{fullscreen ? '✕' : '⛶'}</button>
        </div>
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

      <div className="market-chart-footer">
        <span>Velas y Mark Price: CoinW</span>
        <span>{activePosition ? 'Niveles ENTRY / TP / SL sincronizados con la operación activa' : 'Selecciona el par de una operación activa para ver ENTRY / TP / SL'}</span>
      </div>
    </section>
  );
}
