export type User = {
  user_id: string;
  phone?: string;
  status?: string;
  role?: string;
  plan?: string;
  telegram_verified?: boolean;
  tutorial_completed?: boolean;
};

export type TradingConfig = {
  execution_mode: 'demo' | 'live';
  trading_enabled: boolean;
  operating_capital: number;
  minimum_operating_capital: number;
  coinw_configured: boolean;
  coinw_api_key?: string | null;
  live_allowed: boolean;
  live_state?: string;
};

export type Execution = {
  mode: string;
  status: string;
  capital: number;
  configured_capital: number;
  markets_scanned: number;
  candidates: number;
  coinw_connected: boolean;
  leverage: string;
  market_selection: string;
  timeframe_selection: string;
  trading_enabled: boolean;
};

export type Performance = {
  capital: number;
  configured_capital: number;
  pnl: number;
  pnl_pct: number;
  drawdown: number;
  win_rate: number;
  profit_factor: number;
  trades: number;
  current_capital: number;
  available_equity: number;
};

export type Position = Record<string, unknown> & {
  position_id?: string;
  symbol?: string;
  side?: string;
  direction?: string;
  status?: string;
  entry_price?: number;
  exit_price?: number;
  current_price?: number;
  quantity?: number;
  realized_pnl?: number;
  unrealized_pnl?: number;
  entry_fee?: number;
  exit_fee?: number;
  funding_pnl?: number;
  opened_at?: string | number;
  closed_at?: string | number;
  created_at?: string | number;
  stop_loss?: number;
  take_profit?: number;
  tp1?: number;
  tp2?: number;
};

export type Operations = { open: Position[]; closed: Position[] };
export type Entitlement = {
  demo_allowed: boolean;
  live_allowed: boolean;
  live_state: string;
  live_expires_at?: string | null;
  reason?: string | null;
};
