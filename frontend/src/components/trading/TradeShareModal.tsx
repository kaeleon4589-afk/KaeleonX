import { useMemo, useState } from 'react';
import type { TradeShareOptions, TradeShareSnapshot } from '../../lib/tradeShare';
import { downloadTradeShareCard, shareTradeShareCard } from '../../lib/tradeShare';

const money = (n: unknown) =>
  new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Number(n) || 0);

const formatPercent = (n: unknown) => {
  const value = Number(n);
  if (!Number.isFinite(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(2)}%`;
};

const marketPrice = (value: unknown, exactPrecision?: number | null) => {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '—';
  const abs = Math.abs(n);
  const fallbackDigits = abs < 0.0001 ? 10 : abs < 0.01 ? 8 : abs < 1 ? 6 : abs < 1000 ? 4 : 2;
  const digits = Number.isInteger(exactPrecision) ? Math.max(0, Math.min(12, Number(exactPrecision))) : fallbackDigits;
  return new Intl.NumberFormat('en-US', {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(n);
};

const dateTime = (value: number) =>
  new Intl.DateTimeFormat('es-MX', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));

export default function TradeShareModal({
  snapshot,
  onClose,
  onNotice,
}: {
  snapshot: TradeShareSnapshot;
  onClose: () => void;
  onNotice: (message: string) => void;
}) {
  const [options, setOptions] = useState<TradeShareOptions>({ showRoe: true, showPnl: true });
  const [busy, setBusy] = useState<'share' | 'download' | ''>('');
  const accentClass = (snapshot.pnlValue ?? 0) >= 0 ? 'positive' : 'negative';
  const sideClass = snapshot.side === 'LONG' ? 'long' : 'short';
  const effectivePriceLabel = snapshot.status === 'LIVE' ? 'Precio actual' : 'Precio de salida';
  const effectivePrice = snapshot.status === 'LIVE' ? snapshot.currentPrice : snapshot.exitPrice;
  const snapshotTime = useMemo(() => snapshot.sharedAt || Date.now(), [snapshot.sharedAt]);

  async function handleShare() {
    setBusy('share');
    try {
      const result = await shareTradeShareCard(snapshot, { ...options });
      onNotice(result === 'shared' ? 'Tarjeta enviada al menú de compartir del teléfono.' : 'Tu navegador no soporta compartir archivos; se descargó la tarjeta.');
      onClose();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : 'No fue posible compartir la tarjeta.');
    } finally {
      setBusy('');
    }
  }

  async function handleDownload() {
    setBusy('download');
    try {
      await downloadTradeShareCard(snapshot, { ...options });
      onNotice('Tarjeta descargada correctamente.');
      onClose();
    } catch (error) {
      onNotice(error instanceof Error ? error.message : 'No fue posible descargar la tarjeta.');
    } finally {
      setBusy('');
    }
  }

  return (
    <div className="share-modal-backdrop" onClick={onClose}>
      <div className="share-modal" onClick={(event) => event.stopPropagation()}>
        <div className="share-modal-header">
          <div>
            <small>KAELEON · Compartir operación</small>
            <h3>{snapshot.symbol.replace(/[_-]/g, '/')}</h3>
          </div>
          <button className="share-close-btn" onClick={onClose} aria-label="Cerrar">
            ✕
          </button>
        </div>

        <div className="share-preview-card">
          <div className={`share-preview-overlay ${accentClass}`}>
            <div className="share-preview-topline">
              <span className={`share-pill mode ${snapshot.mode}`}>{snapshot.mode.toUpperCase()}</span>
              <span className={`share-pill status ${snapshot.status.toLowerCase()}`}>{snapshot.status}</span>
              <span className={`share-pill side ${sideClass}`}>{snapshot.side}</span>
              <span className="share-pill leverage">{Math.max(1, Number(snapshot.leverage) || 1)}x</span>
            </div>
            <div className="share-preview-copy">
              <strong>{snapshot.symbol.replace(/[_-]/g, '/')}</strong>
              <span>{snapshot.status === 'LIVE' ? 'Operación en vivo' : 'Operación cerrada'}</span>
            </div>
            <div className="share-preview-metrics">
              {options.showRoe && (
                <div>
                  <small>ROI / ROE</small>
                  <strong>{formatPercent(snapshot.roePct)}</strong>
                </div>
              )}
              {options.showPnl && (
                <div>
                  <small>PnL</small>
                  <strong>{`${(snapshot.pnlValue ?? 0) > 0 ? '+' : ''}${money(snapshot.pnlValue ?? 0)}`}</strong>
                </div>
              )}
            </div>
            <div className="share-preview-grid">
              <span>
                Precio de entrada
                <strong>{marketPrice(snapshot.entryPrice, snapshot.pricePrecision)}</strong>
              </span>
              <span>
                {effectivePriceLabel}
                <strong>{marketPrice(effectivePrice, snapshot.pricePrecision)}</strong>
              </span>
              <span>
                Modo
                <strong>{snapshot.mode.toUpperCase()}</strong>
              </span>
              <span>
                Compartido
                <strong>{dateTime(snapshotTime)}</strong>
              </span>
            </div>
          </div>
        </div>

        <div className="share-modal-options">
          <label>
            <input
              type="checkbox"
              checked={options.showRoe}
              onChange={(event) => setOptions((current) => ({ ...current, showRoe: event.target.checked }))}
            />
            Mostrar porcentaje ROI / ROE
          </label>
          <label>
            <input
              type="checkbox"
              checked={options.showPnl}
              onChange={(event) => setOptions((current) => ({ ...current, showPnl: event.target.checked }))}
            />
            Mostrar PnL en capital
          </label>
        </div>

        <div className="share-modal-actions">
          <button className="secondary-btn" onClick={handleDownload} disabled={busy !== ''}>
            {busy === 'download' ? 'Descargando…' : 'Descargar tarjeta'}
          </button>
          <button className="primary-btn" onClick={handleShare} disabled={busy !== ''}>
            {busy === 'share' ? 'Preparando…' : 'Compartir'}
          </button>
        </div>
      </div>
    </div>
  );
}
