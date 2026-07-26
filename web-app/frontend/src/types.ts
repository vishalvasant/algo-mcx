export interface MarketSummary {
  underlying: string;
  active_underlying?: string;
  commodities?: CommoditySnapshot[];
  spot_ltp: number | null;
  session_vwap: number | null;
  spot_vs_vwap: string | null;
  atm_strike: number | null;
  bias_5m: string;
  market_session: string;
  market_open: boolean;
  strategy: string;
  regime?: string | null;
  confidence?: number | null;
  trading_mode: string;
  ist_time?: string;
  today_pnl: number;
  trade_count: number;
  starting_capital?: number;
  available_capital?: number;
  deployed_capital?: number;
  used_margin?: number;
  has_open_position?: boolean;
  auto_trading_active?: boolean;
  auto_trade_enabled?: boolean;
  scan_interval_seconds?: number;
  candidate_count?: number;
  rejection_count?: number;
  consecutive_losses?: number;
  max_daily_loss?: number;
  max_deployed_pct_of_equity?: number;
  kill_switch?: boolean;
  entries_blocked?: boolean;
  block_reason?: string | null;
  feed_mode?: string;
  expiry_symbol?: string | null;
  instrument_count?: number;
  recent_rejections?: Array<{ tsym: string; reasons: string[] }>;
  open_position?: {
    tsym: string;
    quantity: number;
    entry_price: number;
    current_ltp?: number;
    unrealized_pnl?: number;
    side?: string;
    setup_type?: string;
    premium_deployed?: number;
  };
  open_positions?: Array<{
    tsym: string;
    side: string;
    quantity: number;
    entry_price: number;
    current_ltp: number;
    unrealized_pnl: number;
    premium_deployed: number;
    setup_type: string;
  }>;
  open_position_count?: number;
  unrealized_pnl?: number;
  equity?: number;
  unread_notifications?: number;
}

export type Severity = "info" | "warning" | "critical";

export interface FlattradeSession {
  user_id: string;
  source: string;
  expires_at: string;
  valid: boolean;
}

export interface FlattradeCredentialsStatus {
  source: string;
  user_id: string | null;
  api_key_masked: string | null;
  api_secret_set: boolean;
  password_set: boolean;
  totp_secret_set: boolean;
  redirect_url: string;
  has_api_credentials: boolean;
  has_auto_login: boolean;
}

export interface EngineHealth {
  status: string;
  trading_mode: string;
  db_ok: boolean;
  broker_connected: boolean;
  flattrade_session: FlattradeSession | null;
  flattrade_credentials?: {
    configured: boolean;
    has_auto_login: boolean;
  };
  spot_ltp: string | null;
  instrument_count: number;
  last_quote_ts: string | null;
  kill_switch?: boolean;
  ts: string;
  error?: string;
}

export interface Notification {
  id: string;
  ts: string;
  type: string;
  severity: Severity;
  title: string;
  message: string;
  read: boolean;
}

export interface Trade {
  id: string;
  tsym?: string;
  side?: string;
  instrument_token?: string;
  entry_ts: string;
  exit_ts: string;
  entry_price?: number;
  exit_price?: number;
  quantity?: number;
  lot_size?: number;
  lots?: number;
  pnl: string | number;
  pnl_pct?: number | null;
  mfe?: number | null;
  mae?: number | null;
  exit_reason: string;
  setup_type: string;
  hold_seconds?: number | null;
  mode: string;
}

export interface TradesReportSummary {
  trades: number;
  wins: number;
  losses: number;
  win_rate_pct: number;
  total_pnl: number;
  avg_pnl: number;
  best_trade: number;
  worst_trade: number;
  gross_profit: number;
  gross_loss: number;
}

export interface TradesReport {
  from_date: string | null;
  to_date: string | null;
  generated_at: string;
  summary: TradesReportSummary;
  by_exit_reason: Record<string, { count: number; pnl: number }>;
  by_setup: Record<string, { count: number; pnl: number }>;
  by_day: Record<string, { count: number; pnl: number }>;
  trades: Trade[];
}

export interface WatchlistItem {
  token: string;
  tsym: string;
  strike: number;
  option_type: string;
  is_atm: boolean;
  tradable: boolean;
  lot_size?: number;
  ltp: number | null;
  bid: number | null;
  ask: number | null;
  volume: number | null;
  oi: number | null;
  segment_group?: string;
  segment_label?: string;
  segment_key?: string;
  expiry_label?: string | null;
  group_order?: number;
  iv?: number | null;
  delta?: number | null;
  gamma?: number | null;
  theta?: number | null;
  vega?: number | null;
  greeks_source?: string;
  last_update_ts: string | null;
}

export interface CommoditySnapshot {
  underlying: string;
  display_name?: string;
  spot_ltp: number | null;
  /** Futures LTP for option-chain ATM when spot_ltp is NSE cash index. */
  trading_spot_ltp?: number | null;
  session_open?: number | null;
  change?: number | null;
  change_pct?: number | null;
  atm_strike: number | null;
  expiry_symbol?: string | null;
  expiry_label?: string | null;
  fut_tsym?: string | null;
  card_type?: "tradeable" | "display" | "fut";
  instrument_count?: number;
  strike_band_points?: number;
  strike_step?: number;
  atm_strike_steps?: number;
  strike_count?: number;
  items?: WatchlistItem[];
}

export interface Watchlist {
  underlying: string;
  active_underlying?: string;
  watchlist_mode?: "futures" | "options";
  commodities?: CommoditySnapshot[];
  atm_strike_steps?: number;
  spot_ltp: number | null;
  atm_strike: number | null;
  expiry_symbol?: string | null;
  instrument_count: number;
  strike_count?: number;
  strike_band_points?: number;
  strike_step?: number;
  last_quote_ts: string | null;
  feed_mode?: string;
  market_open?: boolean;
  greeks_source?: string;
  items: WatchlistItem[];
  open_positions?: WatchlistOpenPosition[];
}

export interface WatchlistOpenPosition {
  position_id?: string;
  tsym: string;
  side?: string;
  quantity: number;
  lot_size?: number;
  lots?: number;
  entry_price: number;
  entry_ts?: string;
  current_ltp?: number;
  unrealized_pnl?: number;
  premium_deployed?: number;
  setup_type?: string;
  stop_loss?: number | null;
  target_price?: number | null;
}

export interface ClosedBlotterTrade {
  id: string;
  tsym: string;
  side?: string;
  entry_ts: string;
  exit_ts: string;
  entry_price: number;
  exit_price: number;
  quantity: number;
  lot_size: number;
  lots: number;
  pnl: number;
  exit_reason: string;
  setup_type?: string;
  hold_seconds?: number;
}

export interface DecisionLogEvent {
  id: string;
  ts: string;
  event_type: string;
  severity: string;
  message: string;
  metadata: Record<string, unknown>;
}

export interface TradeBlotter {
  open_positions: WatchlistOpenPosition[];
  closed_trades: ClosedBlotterTrade[];
  today_pnl?: number;
}

export interface OhlcBar {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number | null;
}

export interface ChartCandlesResponse {
  underlying: string;
  interval: string;
  price_source?: string;
  instrument_token?: string | null;
  fut_tsym?: string | null;
  bars: OhlcBar[];
}

export interface AuthUser {
  username: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  username: string;
  expires_at: string;
}
