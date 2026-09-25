export type TradeShareSnapshot = {
  symbol: string;
  side: 'LONG' | 'SHORT';
  leverage: number;
  mode: 'demo' | 'live';
  status: 'LIVE' | 'CLOSED';
  entryPrice: number;
  currentPrice?: number | null;
  exitPrice?: number | null;
  pnlValue?: number | null;
  roePct?: number | null;
  sharedAt?: number;
  pricePrecision?: number | null;
};

export type TradeShareOptions = {
  showRoe: boolean;
  showPnl: boolean;
};

const CARD_SIZE = 1080;
const BASE_IMAGE_SRC = '/images/kaeleon-trading-art.jpg';

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

const formatPrice = (value: unknown, exactPrecision?: number | null) => {
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

const formatDateTime = (value: number) =>
  new Intl.DateTimeFormat('es-MX', {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value));

function roundedRect(ctx: CanvasRenderingContext2D, x: number, y: number, w: number, h: number, r: number) {
  const radius = Math.min(r, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x + radius, y);
  ctx.arcTo(x + w, y, x + w, y + h, radius);
  ctx.arcTo(x + w, y + h, x, y + h, radius);
  ctx.arcTo(x, y + h, x, y, radius);
  ctx.arcTo(x, y, x + w, y, radius);
  ctx.closePath();
}

function drawBadge(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, fill: string, textColor = '#02160e') {
  ctx.save();
  ctx.font = '700 26px Inter, system-ui, sans-serif';
  const paddingX = 20;
  const height = 44;
  const width = ctx.measureText(text).width + paddingX * 2;
  ctx.fillStyle = fill;
  roundedRect(ctx, x, y, width, height, 16);
  ctx.fill();
  ctx.fillStyle = textColor;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, x + paddingX, y + height / 2);
  ctx.restore();
  return width;
}

function drawLabelValue(
  ctx: CanvasRenderingContext2D,
  label: string,
  value: string,
  x: number,
  y: number,
  width: number,
  valueColor = '#ecf6fb',
) {
  ctx.save();
  ctx.fillStyle = 'rgba(2, 16, 25, 0.72)';
  ctx.strokeStyle = 'rgba(130, 188, 217, 0.16)';
  ctx.lineWidth = 2;
  roundedRect(ctx, x, y, width, 98, 22);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = 'rgba(174, 197, 212, 0.82)';
  ctx.font = '600 22px Inter, system-ui, sans-serif';
  ctx.fillText(label, x + 22, y + 32);
  ctx.fillStyle = valueColor;
  ctx.font = '800 30px Inter, system-ui, sans-serif';
  ctx.fillText(value, x + 22, y + 71);
  ctx.restore();
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.crossOrigin = 'anonymous';
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error('No se pudo cargar la imagen base de KAELEON.'));
    img.src = src;
  });
}

export async function renderTradeShareCard(snapshot: TradeShareSnapshot, options: TradeShareOptions): Promise<Blob> {
  const canvas = document.createElement('canvas');
  canvas.width = CARD_SIZE;
  canvas.height = CARD_SIZE;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('No fue posible crear la tarjeta para compartir.');

  const accent = (snapshot.pnlValue ?? 0) >= 0 ? '#00e88b' : '#ff365f';
  const softAccent = (snapshot.pnlValue ?? 0) >= 0 ? 'rgba(0,232,139,0.22)' : 'rgba(255,54,95,0.22)';
  const bg = await loadImage(BASE_IMAGE_SRC);

  ctx.drawImage(bg, 0, 0, CARD_SIZE, CARD_SIZE);

  const overlay = ctx.createLinearGradient(0, 0, 0, CARD_SIZE);
  overlay.addColorStop(0, 'rgba(1, 8, 14, 0.22)');
  overlay.addColorStop(0.45, 'rgba(1, 7, 12, 0.12)');
  overlay.addColorStop(0.75, 'rgba(2, 9, 16, 0.82)');
  overlay.addColorStop(1, 'rgba(2, 9, 16, 0.93)');
  ctx.fillStyle = overlay;
  ctx.fillRect(0, 0, CARD_SIZE, CARD_SIZE);

  ctx.save();
  ctx.strokeStyle = softAccent;
  ctx.lineWidth = 5;
  roundedRect(ctx, 32, 32, CARD_SIZE - 64, CARD_SIZE - 64, 34);
  ctx.stroke();
  ctx.restore();

  let cursorX = 66;
  cursorX += drawBadge(ctx, snapshot.mode === 'live' ? 'LIVE' : 'DEMO', cursorX, 66, 'rgba(4, 20, 30, 0.86)', '#eef6fb') + 10;
  cursorX += drawBadge(ctx, snapshot.status, cursorX, 66, snapshot.status === 'LIVE' ? 'rgba(11,187,232,0.9)' : 'rgba(8, 34, 46, 0.9)', snapshot.status === 'LIVE' ? '#04131b' : '#eef6fb') + 10;
  drawBadge(ctx, snapshot.side, cursorX, 66, accent, '#02160e');
  drawBadge(ctx, `${Math.max(1, Number(snapshot.leverage) || 1)}x`, CARD_SIZE - 170, 66, 'rgba(242,184,62,0.92)', '#1b1202');

  ctx.fillStyle = '#ecf6fb';
  ctx.font = '900 66px Inter, system-ui, sans-serif';
  ctx.fillText(snapshot.symbol.replace(/[_-]/g, '/'), 66, 166);
  ctx.font = '600 26px Inter, system-ui, sans-serif';
  ctx.fillStyle = 'rgba(203, 222, 233, 0.88)';
  ctx.fillText(snapshot.status === 'LIVE' ? 'Operación compartida en vivo' : 'Resultado final compartido', 70, 205);

  if (options.showRoe) {
    ctx.fillStyle = 'rgba(2, 16, 25, 0.68)';
    ctx.strokeStyle = softAccent;
    ctx.lineWidth = 3;
    roundedRect(ctx, 60, 230, 380, 210, 28);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = 'rgba(189, 208, 220, 0.9)';
    ctx.font = '700 28px Inter, system-ui, sans-serif';
    ctx.fillText('ROI / ROE', 92, 282);
    ctx.fillStyle = accent;
    ctx.font = '900 84px Inter, system-ui, sans-serif';
    ctx.fillText(formatPercent(snapshot.roePct), 88, 370);
  }

  if (options.showPnl) {
    ctx.fillStyle = 'rgba(2, 16, 25, 0.68)';
    ctx.strokeStyle = softAccent;
    ctx.lineWidth = 3;
    roundedRect(ctx, 460, 230, 560, 210, 28);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = 'rgba(189, 208, 220, 0.9)';
    ctx.font = '700 28px Inter, system-ui, sans-serif';
    ctx.fillText('PnL', 492, 282);
    ctx.fillStyle = accent;
    ctx.font = '900 72px Inter, system-ui, sans-serif';
    const pnlLabel = money(snapshot.pnlValue ?? 0);
    ctx.fillText(`${(snapshot.pnlValue ?? 0) > 0 ? '+' : ''}${pnlLabel}`, 488, 366);
    ctx.font = '600 22px Inter, system-ui, sans-serif';
    ctx.fillStyle = 'rgba(189, 208, 220, 0.78)';
    ctx.fillText('Capital mostrado', 492, 402);
  }

  const bottomY = 640;
  ctx.fillStyle = 'rgba(2, 16, 25, 0.78)';
  ctx.strokeStyle = 'rgba(120, 178, 202, 0.18)';
  ctx.lineWidth = 2;
  roundedRect(ctx, 56, bottomY - 20, CARD_SIZE - 112, 320, 32);
  ctx.fill();
  ctx.stroke();

  const gap = 24;
  const colWidth = (CARD_SIZE - 112 - gap * 3) / 2;
  drawLabelValue(ctx, 'Precio de entrada', formatPrice(snapshot.entryPrice, snapshot.pricePrecision), 86, bottomY + 16, colWidth, '#ecf6fb');
  drawLabelValue(
    ctx,
    snapshot.status === 'LIVE' ? 'Precio actual' : 'Precio de salida',
    formatPrice(snapshot.status === 'LIVE' ? snapshot.currentPrice : snapshot.exitPrice, snapshot.pricePrecision),
    86 + colWidth + gap,
    bottomY + 16,
    colWidth,
    accent,
  );
  drawLabelValue(ctx, 'Modo de cuenta', snapshot.mode === 'live' ? 'LIVE' : 'DEMO', 86, bottomY + 132, colWidth, '#ecf6fb');
  drawLabelValue(ctx, 'Compartido', formatDateTime(snapshot.sharedAt || Date.now()), 86 + colWidth + gap, bottomY + 132, colWidth, '#ecf6fb');

  ctx.fillStyle = accent;
  ctx.font = '700 20px Inter, system-ui, sans-serif';
  ctx.fillText(snapshot.status === 'LIVE' ? 'Datos en vivo sincronizados con KAELEON' : 'Resultado final cerrado en KAELEON', 86, CARD_SIZE - 54);

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((value) => {
      if (value) resolve(value);
      else reject(new Error('No fue posible exportar la tarjeta de KAELEON.'));
    }, 'image/png');
  });

  return blob;
}

function buildFilename(snapshot: TradeShareSnapshot) {
  const stamp = new Date(snapshot.sharedAt || Date.now()).toISOString().replace(/[:.]/g, '-');
  return `kaeleon-${snapshot.symbol.replace(/[^a-z0-9]/gi, '-').toLowerCase()}-${snapshot.status.toLowerCase()}-${stamp}.png`;
}

export async function shareTradeShareCard(snapshot: TradeShareSnapshot, options: TradeShareOptions) {
  const blob = await renderTradeShareCard(snapshot, options);
  const file = new File([blob], buildFilename(snapshot), { type: 'image/png' });
  if (navigator.canShare?.({ files: [file] })) {
    await navigator.share({
      files: [file],
      title: `${snapshot.symbol} · ${snapshot.side}`,
      text: `Operación ${snapshot.status === 'LIVE' ? 'en vivo' : 'cerrada'} compartida desde KAELEON`,
    });
    return 'shared';
  }
  downloadBlob(blob, buildFilename(snapshot));
  return 'downloaded';
}

export async function downloadTradeShareCard(snapshot: TradeShareSnapshot, options: TradeShareOptions) {
  const blob = await renderTradeShareCard(snapshot, options);
  downloadBlob(blob, buildFilename(snapshot));
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1200);
}
