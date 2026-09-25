import { useCallback, useSyncExternalStore } from 'react';

export type LiveMarketQuoteSource = 'websocket' | 'rest' | 'initial';

export type LiveMarketQuote = {
  price: number;
  source: LiveMarketQuoteSource;
  updatedAt: number;
};

const quotes = new Map<string, LiveMarketQuote>();
const listeners = new Map<string, Set<() => void>>();

export function canonicalLiveSymbol(input: unknown): string {
  let value = String(input || '').trim().toUpperCase().replaceAll('-', '_').replaceAll('/', '_');
  value = value.split('_').filter(Boolean).join('_');
  if (!value) return '';
  if (value.endsWith('_USDC')) return value;
  if (value.endsWith('USDC')) return `${value.slice(0, -4)}_USDC`;
  if (value.endsWith('_USDT')) return `${value.slice(0, -5)}USDT`;
  if (value.endsWith('USDT')) return value;
  if (value.includes('_')) return value;
  return `${value}USDT`;
}

export function publishLiveMarketQuote(symbol: unknown, price: unknown, source: LiveMarketQuoteSource): void {
  const key = canonicalLiveSymbol(symbol);
  const numericPrice = Number(price);
  if (!key || !Number.isFinite(numericPrice) || numericPrice <= 0) return;

  const previous = quotes.get(key);
  if (previous && previous.price === numericPrice && previous.source === source) return;

  quotes.set(key, { price: numericPrice, source, updatedAt: Date.now() });
  listeners.get(key)?.forEach((listener) => listener());
}

export function getLiveMarketQuote(symbol: unknown): LiveMarketQuote | null {
  const key = canonicalLiveSymbol(symbol);
  return key ? quotes.get(key) ?? null : null;
}

export function useLiveMarketQuote(symbol: unknown): LiveMarketQuote | null {
  const key = canonicalLiveSymbol(symbol);
  const subscribe = useCallback((listener: () => void) => {
    if (!key) return () => undefined;
    const bucket = listeners.get(key) ?? new Set<() => void>();
    bucket.add(listener);
    listeners.set(key, bucket);
    return () => {
      bucket.delete(listener);
      if (!bucket.size) listeners.delete(key);
    };
  }, [key]);
  const getSnapshot = useCallback(() => (key ? quotes.get(key) ?? null : null), [key]);
  return useSyncExternalStore(subscribe, getSnapshot, () => null);
}
