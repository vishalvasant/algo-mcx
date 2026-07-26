import type {
  AuthUser,
  ChartCandlesResponse,
  DecisionLogEvent,
  EngineHealth,
  LoginResponse,
  MarketSummary,
  Notification,
  Trade,
  TradeBlotter,
  TradesReport,
  Watchlist,
} from "../types";
import { getStoredToken, setStoredToken } from "../auth/storage";

const API = "/api";

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  if (!headers.has("Content-Type") && init?.body) {
    headers.set("Content-Type", "application/json");
  }
  const token = getStoredToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${API}${path}`, {
    ...init,
    headers,
    credentials: "include",
  });

  if (res.status === 401 && !path.startsWith("/auth/login")) {
    setStoredToken(null);
    if (!window.location.pathname.startsWith("/login")) {
      window.location.href = "/login";
    }
    throw new ApiError("Session expired", 401);
  }

  if (!res.ok) {
    const text = await res.text();
    let message = text || `HTTP ${res.status}`;
    try {
      const parsed = JSON.parse(text) as { detail?: string };
      if (parsed.detail) message = parsed.detail;
    } catch {
      /* use raw text */
    }
    throw new ApiError(message, res.status);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export function login(username: string, password: string): Promise<LoginResponse> {
  return request<LoginResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
}

export function logout(): Promise<{ ok: boolean }> {
  return request("/auth/logout", { method: "POST" });
}

export function fetchMe(): Promise<AuthUser> {
  return request<AuthUser>("/auth/me");
}

export function fetchHealth(): Promise<EngineHealth> {
  return request<EngineHealth>("/health");
}

export function fetchMarketSummary(): Promise<MarketSummary> {
  return request<MarketSummary>("/market-summary");
}

export function fetchNotifications(limit = 30, unreadOnly = false): Promise<Notification[]> {
  const q = new URLSearchParams({ limit: String(limit), unread_only: String(unreadOnly) });
  return request<Notification[]>(`/notifications?${q}`);
}

export function markNotificationRead(id: string): Promise<{ read: boolean }> {
  return request(`/notifications/${id}/read`, { method: "POST" });
}

export function fetchTradesToday(limit = 100): Promise<Trade[]> {
  return request<Trade[]>(`/trades/today?limit=${limit}`);
}

export function fetchTrades(
  limit = 500,
  opts: { todayOnly?: boolean; fromDate?: string; toDate?: string } = {},
): Promise<Trade[]> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (opts.todayOnly) q.set("today_only", "true");
  if (opts.fromDate) q.set("from_date", opts.fromDate);
  if (opts.toDate) q.set("to_date", opts.toDate);
  return request<Trade[]>(`/trades?${q}`);
}

export function fetchTradesReport(opts: {
  fromDate?: string;
  toDate?: string;
  limit?: number;
} = {}): Promise<TradesReport> {
  const q = new URLSearchParams();
  if (opts.fromDate) q.set("from_date", opts.fromDate);
  if (opts.toDate) q.set("to_date", opts.toDate);
  if (opts.limit) q.set("limit", String(opts.limit));
  const qs = q.toString();
  return request(`/trades/report${qs ? `?${qs}` : ""}`);
}

export function fetchTradeDates(): Promise<string[]> {
  return request<string[]>("/trades/dates");
}

export function setKillSwitch(enabled: boolean): Promise<{ kill_switch: boolean }> {
  return request(`/control/kill-switch?enabled=${enabled}`, { method: "POST" });
}

export function setTradingMode(mode: "paper" | "live"): Promise<{
  ok: boolean;
  trading_mode: string;
}> {
  return request(`/control/trading-mode?mode=${mode}`, { method: "POST" });
}

export function setAutoTrade(enabled: boolean): Promise<{
  auto_trade_enabled: boolean;
  kill_switch: boolean;
  entries_blocked: boolean;
  block_reason: string | null;
}> {
  return request(`/control/auto-trade?enabled=${enabled}`, { method: "POST" });
}

export function syncMissingData(): Promise<{
  ok: boolean;
  error?: string;
  message?: string;
  universe: Record<string, unknown>;
  candles: Record<string, unknown>;
  quotes: Record<string, unknown>;
  spot_ltp?: number | null;
}> {
  return request("/control/sync-missing", { method: "POST" });
}

export interface DecisionLogsResponse {
  scan_interval_seconds: number;
  decisions_today: number;
  total: number;
  limit: number;
  offset: number;
  events: DecisionLogEvent[];
}

export function fetchDecisionLogs(options: {
  limit?: number;
  offset?: number;
  eventType?: string;
} = {}): Promise<DecisionLogsResponse> {
  const { limit = 25, offset = 0, eventType } = options;
  const q = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (eventType) q.set("event_type", eventType);
  return request(`/decision-logs?${q}`);
}

export function reauthenticate(force = true): Promise<{
  ok: boolean;
  user_id: string;
  expires_at: string;
  valid: boolean;
  broker_connected: boolean;
}> {
  return request(`/control/reauth?force=${force}`, { method: "POST" });
}

export function fetchWatchlist(): Promise<Watchlist> {
  return request<Watchlist>("/watchlist");
}

export function fetchTradeBlotter(limit = 200): Promise<TradeBlotter> {
  return request<TradeBlotter>(`/trade-blotter?limit=${limit}`);
}

export function fetchChartCandles(
  underlying: string,
  interval = "15m",
  days = 30,
  opts?: { token?: string; exchange?: string; tsym?: string },
): Promise<ChartCandlesResponse> {
  const params = new URLSearchParams({
    underlying,
    interval,
    days: String(days),
  });
  if (opts?.token) params.set("token", opts.token);
  if (opts?.exchange) params.set("exchange", opts.exchange);
  if (opts?.tsym) params.set("tsym", opts.tsym);
  return request<ChartCandlesResponse>(`/chart/candles?${params}`);
}

export function resetPaperAccount(): Promise<{
  ok: boolean;
  starting_capital: number;
  available_capital: number;
  expiry_symbol?: string | null;
  instrument_count?: number;
}> {
  return request("/control/reset-paper-account", { method: "POST" });
}

export function fetchFlattradeCredentials(): Promise<import("../types").FlattradeCredentialsStatus> {
  return request("/settings/flattrade");
}

export function saveFlattradeCredentials(body: {
  user_id?: string;
  api_key?: string;
  api_secret?: string;
  password?: string;
  totp_secret?: string;
  redirect_url?: string;
}): Promise<import("../types").FlattradeCredentialsStatus> {
  return request("/settings/flattrade", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function exitPosition(positionId: string): Promise<{
  ok: boolean;
  position_id: string;
  tsym: string;
  quantity: number;
  entry_price: number;
  exit_price: number;
  pnl: number;
  exit_reason: string;
}> {
  return request(`/control/exit-position?position_id=${encodeURIComponent(positionId)}`, {
    method: "POST",
  });
}

/** Live option-chain tick stream (SSE). Cookie/bearer session must already be active. */
export function openWatchlistStream(
  onTick: (watchlist: Watchlist) => void,
  onError?: (err: Event) => void,
): () => void {
  let source: EventSource | null = null;
  let closed = false;
  let retryMs = 1000;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  const connect = () => {
    if (closed) return;
    const token = getStoredToken();
    const url = token
      ? `${API}/quotes/stream?access_token=${encodeURIComponent(token)}`
      : `${API}/quotes/stream`;
    source = new EventSource(url, { withCredentials: true });
    source.onmessage = (ev) => {
      retryMs = 1000;
      try {
        onTick(JSON.parse(ev.data) as Watchlist);
      } catch {
        /* ignore malformed frames */
      }
    };
    source.onerror = (err) => {
      onError?.(err);
      source?.close();
      source = null;
      if (!closed) {
        retryTimer = setTimeout(connect, retryMs);
        retryMs = Math.min(retryMs * 2, 10_000);
      }
    };
  };

  connect();

  return () => {
    closed = true;
    if (retryTimer != null) clearTimeout(retryTimer);
    source?.close();
  };
}
