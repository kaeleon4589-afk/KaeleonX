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
  demo_operating_capital: number;
  live_operating_capital: number;
  minimum_operating_capital: number;
  demo_available_equity: number;
  live_available_equity: number;
  available_equity: number;
  coinw_configured: boolean;
  coinw_verified: boolean;
  coinw_verified_at?: string | null;
  coinw_api_key?: string | null;
  live_allowed: boolean;
  live_state?: string;
};

export type Execution = {
  mode: string;
  status: string;
  capital: number;
  configured_capital: number;
  available_equity: number;
  markets_scanned: number;
  candidates: number;
  coinw_connected: boolean;
  leverage: string;
  market_selection: string;
  timeframe_selection: string;
  trading_enabled: boolean;
};

export type Performance = {
  mode: 'demo' | 'live';
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
  mode?: 'demo' | 'live';
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
  stop_price?: number;
  take_profit?: number;
  target_price?: number;
  tp1?: number;
  tp1_price?: number;
  tp2?: number;
  tp2_price?: number;
};

export type Operations = { mode?: 'demo'|'live'; open: Position[]; closed: Position[] };
export type Entitlement = {
  demo_allowed: boolean;
  live_allowed: boolean;
  live_state: string;
  live_expires_at?: string | null;
  reason?: string | null;
};
