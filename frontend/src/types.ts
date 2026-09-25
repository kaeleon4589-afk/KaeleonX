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
  demo_auto_compound?: boolean;
  leverage?: number;
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
  leverage: number;
  last_price?: number;
  last_market_at?: number;
  last_rejection?: string | null;
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
  net_pnl?: number;
  settlement_pending?: boolean;
  leverage?: number;
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

export type ReferralItem = {
  user_id?: string;
  phone_masked?: string | null;
  status?: string;
  created_at?: string;
  rewarded: boolean;
  reward_days: number;
  rewarded_at?: string | null;
};
export type ReferralSummary = {
  referral_code?: string | null;
  referred_count: number;
  rewarded_count: number;
  reward_days_total: number;
  reward_rules: Record<string, number>;
  items: ReferralItem[];
};
export type AdminDashboard = {
  users: { total: number; active: number; blocked: number; suspended: number };
  trading: { open_positions: number; orders: number; decisions: number };
  payments: { total: number; pending: number; confirmed: number };
  timestamp: string;
};
export type AdminUser = User & {
  created_at?: string;
  live_state?: string;
  subscription_expires_at?: string | null;
  live_access_until?: string | null;
  referral_code?: string;
  referral_reward_days_total?: number;
};
export type AdminReferral = {
  referred_user_id?: string;
  referred_phone?: string;
  referrer_user_id?: string;
  referrer_phone?: string;
  code?: string;
  created_at?: string;
  rewarded?: boolean;
  reward_days?: number;
};

export type BillingPlan = { code: string; days: number; price_usdt: string };
export type PaymentOrder = {
  payment_order_id: string;
  plan_code: string;
  duration_days: number;
  amount_usdt: string;
  network: string;
  destination_wallet: string;
  status: string;
  created_at?: string;
  expires_at?: string;
  tx_hash?: string | null;
};

export type ActivityEvent = {
  ts?: string;
  created_at?: string;
  event: string;
  decision_id?: string | null;
  user_id?: string;
  mode?: string;
  symbol?: string;
  strategy?: string;
  reason?: string;
  state?: string;
  regime?: string;
  quality?: number;
  score?: number;
  approved?: boolean;
  [key: string]: unknown;
};
export type ActivityResponse = { mode: string; items: ActivityEvent[]; count: number };

export interface AdminStatistics {
  mode: 'demo' | 'live';
  period: StatisticsPeriod | null;
  history: StatisticsPeriod[];
  metrics: { trades: number; wins: number; losses: number; breakeven: number; pnl: number; win_rate: number; profit_factor: number | null; open_positions: number; excluded_positions: number };
}
export interface StatisticsPeriod {
  reset_id: string; started_at: number; label: string; actor: string;
  previous_metrics: AdminStatistics['metrics'];
}
