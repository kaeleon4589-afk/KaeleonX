import type { ActivityResponse, AdminDashboard, AdminReferral, AdminUser, BillingPlan, Entitlement, Execution, Operations, PaymentOrder, Performance, ReferralSummary, TradingConfig, User } from '../types';

const API_BASE = '/api';
const TOKEN_KEY = 'kaeleon_access_token';

export const session = {
  get: () => localStorage.getItem(TOKEN_KEY),
  set: (token: string) => localStorage.setItem(TOKEN_KEY, token),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

export class ApiError extends Error {
  status: number;
  detail: string;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init: RequestInit = {}, auth = true): Promise<T> {
  const headers = new Headers(init.headers);
  if (!headers.has('Content-Type') && init.body) headers.set('Content-Type', 'application/json');
  const token = session.get();
  if (auth && token) headers.set('Authorization', `Bearer ${token}`);

  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { ...init, headers });
  } catch {
    // Include the failing route. A browser can surface an unhandled backend
    // exception as a fetch/CORS error, so the route is essential diagnostics.
    throw new ApiError(0, `backend_unreachable:${path}`);
  }

  const raw = await response.text();
  let data: unknown = null;
  if (raw) {
    try { data = JSON.parse(raw); } catch { data = raw; }
  }
  if (!response.ok) {
    const detail = typeof data === 'object' && data && 'detail' in data ? String((data as {detail: unknown}).detail) : String(data || response.statusText);
    if (response.status === 401 && auth) session.clear();
    throw new ApiError(response.status, detail);
  }
  return data as T;
}

export const api = {
  baseUrl: API_BASE,
  health: () => request<{status: string; version: string}>('/health', {}, false),
  register: (body: {phone: string; password: string; country_code?: string; referral_code?: string}) =>
    request<{user_id: string; challenge: string; phone: string; telegram_verification_url: string; telegram_required: boolean}>('/auth/register', { method: 'POST', body: JSON.stringify(body) }, false),
  verify: (challenge: string) => request<{verified: boolean}>('/auth/verify', { method: 'POST', body: JSON.stringify({ challenge }) }, false),
  login: (phone: string, password: string, country_code = '') => request<{access_token: string; token_type: string}>('/auth/login', { method: 'POST', body: JSON.stringify({ phone, password, country_code }) }, false),
  me: () => request<User>('/auth/me'),
  logout: () => request<{logged_out: boolean}>('/auth/logout', { method: 'POST' }),
  tutorial: (completed = true) => request<{tutorial_completed: boolean}>('/auth/tutorial', { method: 'POST', body: JSON.stringify({ completed }) }),
  config: () => request<TradingConfig>('/user/trading-config'),
  saveConfig: (body: {execution_mode: 'demo'|'live'; trading_enabled: boolean; operating_capital?: number; coinw_api_key?: string; coinw_api_secret?: string}) => request('/user/trading-config', { method: 'PUT', body: JSON.stringify(body) }),
  deleteCredentials: () => request('/user/coinw-credentials', { method: 'DELETE' }),
  testCoinW: () => request<{connected: boolean; verified: boolean; available_equity: number; demo_equity: number; api_key?: string}>('/user/coinw/test', { method: 'POST' }),
  execution: () => request<Execution>('/user/execution'),
  operations: () => request<Operations>('/user/operations'),
  performance: () => request<Performance>('/user/performance'),
  activity: (limit = 120, mode?: string) => request<ActivityResponse>(`/user/activity?limit=${limit}${mode ? `&mode=${encodeURIComponent(mode)}` : ''}`),
  entitlement: () => request<Entitlement>('/billing/entitlement'),
  activateTrial: () => request<Entitlement>('/billing/live/activate-trial', { method: 'POST' }),
  billingPlans: () => request<{plans: BillingPlan[]}>('/billing/plans', {}, false),
  billingOrders: () => request<{items: PaymentOrder[]}>('/billing/orders'),
  createBillingOrder: (plan_code: string) => request<PaymentOrder>('/billing/orders', {method:'POST', body:JSON.stringify({plan_code})}),
  submitBillingTx: (payment_order_id: string, tx_hash: string) => request<PaymentOrder>('/billing/orders/tx', {method:'POST', body:JSON.stringify({payment_order_id,tx_hash})}),
  verifyBillingPayment: (payment_order_id: string, tx_hash: string) => request<{confirmed:boolean;live_state:string;live_expires_at?:string|null}>('/billing/orders/verify', {method:'POST', body:JSON.stringify({payment_order_id,tx_hash})}),

  referrals: () => request<ReferralSummary>('/user/referrals'),
  adminMe: () => request<User>('/admin/me'),
  adminDashboard: () => request<AdminDashboard>('/admin/dashboard'),
  adminUsers: () => request<{items: AdminUser[]}>('/admin/users'),
  adminPayments: () => request<{items: Record<string, unknown>[]}>('/admin/payments'),
  adminReferrals: () => request<{items: AdminReferral[]; total: number; rewarded: number}>('/admin/referrals'),
  adminSubscriptions: () => request<{items: Record<string, unknown>[]}>('/admin/subscriptions'),
  adminOperations: () => request<{items: Record<string, unknown>[]}>('/admin/operations'),
  adminEvents: () => request<{items: Record<string, unknown>[]}>('/admin/system/events'),
  adminConfig: () => request<Record<string, unknown>>('/admin/system/config'),
  adminGrantLiveDays: (body: {phone?: string; telegram_user_id?: string; days: number}) => request('/admin/users/live-days', {method:'POST', body:JSON.stringify(body)}),
  adminBanUser: (body: {phone?: string; telegram_user_id?: string; days?: number; reason?: string}) => request('/admin/users/ban', {method:'POST', body:JSON.stringify(body)}),
  adminUnbanUser: (body: {phone?: string; telegram_user_id?: string}) => request('/admin/users/unban', {method:'POST', body:JSON.stringify(body)}),
};

export function humanizeError(error: unknown): string {
  const detail = error instanceof ApiError ? error.detail : error instanceof Error ? error.message : 'unknown_error';
  const map: Record<string, string> = {
    backend_unreachable: 'No se pudo conectar con el servidor.',
    invalid_credential_encryption_key: 'La clave CREDENTIAL_ENCRYPTION_KEY del backend no es una clave Fernet válida.',
    credential_encryption_key_required: 'Falta CREDENTIAL_ENCRYPTION_KEY en el backend.',
    invalid_credentials: 'Teléfono o contraseña incorrectos.',
    phone_already_registered: 'Ese teléfono ya está registrado.',
    invalid_phone: 'El número de teléfono no es válido.',
    telegram_verification_required_or_invalid: 'La verificación de Telegram todavía no se ha completado.',
    telegram_verification_not_configured: 'La verificación de Telegram no está configurada en el servidor.',
    invalid_or_expired_session: 'Tu sesión venció. Inicia sesión nuevamente.',
    live_not_entitled: 'Tu cuenta todavía no tiene acceso a Live Trading.',
    coinw_credentials_required_for_live: 'Configura tus credenciales CoinW antes de usar Live Trading.',
    coinw_credentials_not_configured: 'Todavía no has configurado CoinW.',
    operating_capital_below_platform_minimum: 'El capital operativo está por debajo del mínimo permitido.',
    coinw_verification_required_for_capital: 'Verifica primero tu API Key y API Secret de CoinW para configurar capital.',
    coinw_verification_required_for_trading: 'Verifica primero tu conexión CoinW antes de activar el trading.',
    operating_capital_required_before_trading: 'Configura el capital operativo antes de activar el trading.',
    demo_capital_exceeds_virtual_balance: 'En DEMO el capital no puede superar los 100 USDT virtuales.',
    live_capital_exceeds_available_balance: 'El capital LIVE supera el saldo USDT disponible en CoinW.',
    pause_live_before_removing_credentials: 'Pausa LIVE antes de eliminar las credenciales CoinW.',
    close_live_position_before_switching_to_demo: 'Cierra la posición LIVE antes de volver a Demo.',
    switch_to_demo_before_removing_credentials: 'Cambia a Demo antes de eliminar las credenciales.',
    disable_live_trading_and_close_position_before_changing_credentials: 'Pausa LIVE y cierra la posición antes de cambiar credenciales.',
    payment_wallet_not_configured: 'La wallet receptora de pagos no está configurada en el backend.',
    payment_verifier_not_configured: 'El verificador de pagos BSC no está configurado correctamente.',
    payment_order_expired: 'La orden de pago venció. Crea una nueva.',
    payment_order_not_found: 'No se encontró la orden de pago.',
    payment_order_not_verifiable: 'Esta orden ya no puede verificarse.',
    tx_already_used: 'Ese hash de transacción ya fue utilizado.',
    tx_not_confirmed: 'La transacción todavía no está confirmada en BNB Smart Chain.',
    wrong_network: 'La transacción no pertenece a BNB Smart Chain.',
    amount_or_destination_mismatch: 'El importe o la wallet receptora no coinciden con la orden.',
    bsc_verification_error: 'No se pudo validar la transacción en BNB Smart Chain. Inténtalo nuevamente.',
  };
  if (detail.startsWith('backend_unreachable:')) {
    const route = detail.slice('backend_unreachable:'.length);
    return `No se pudo completar la conexión con el backend en ${route}.`;
  }
  if (detail.startsWith('coinw_balance_check_failed:')) return `No se pudo verificar el saldo de CoinW: ${detail.slice('coinw_balance_check_failed:'.length)}`;
  if (detail.startsWith('coinw_connection_failed:')) return `CoinW rechazó la conexión: ${detail.slice('coinw_connection_failed:'.length)}`;
  return map[detail] || detail.replaceAll('_', ' ');
}
