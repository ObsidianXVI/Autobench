// Typed client for the Autobench REST API.

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Point {
  time: number | null;
  price: number | null;
}

export interface AnnotationStyle {
  color?: string;
  line_width?: number;
  line_style?: "solid" | "dashed" | "dotted";
  fill_color?: string;
  text_color?: string;
  font_size?: number;
  extend_left?: boolean;
  extend_right?: boolean;
  shape?: "arrow_up" | "arrow_down" | "circle" | "square";
  position?: "above" | "below" | "at";
  show_labels?: boolean;
}

export type AnnotationType =
  | "trendline" | "ray" | "extended_line" | "horizontal_line" | "horizontal_ray" | "vertical_line"
  | "rectangle" | "parallel_channel" | "fib_retracement" | "price_range" | "polyline" | "arrow"
  | "text" | "marker" | "long_position" | "short_position" | "callout";

export interface Annotation {
  id: string;
  symbol: string;
  interval: string | null;
  type: AnnotationType;
  points: Point[];
  text: string | null;
  style: AnnotationStyle;
  author: "agent" | "user";
  tag: string | null;
  visible: boolean;
  created_at: string;
  updated_at: string;
}

export interface IndicatorSpec {
  name: string;
  title: string;
  category: string;
  overlay: boolean;
  params: Record<string, number | string>;
  outputs: string[];
  description: string;
  levels?: number[];
}

export interface IndicatorRef {
  name: string;
  params: Record<string, number | string>;
}

export interface IndicatorResult {
  name: string;
  label: string;
  params: Record<string, number | string>;
  overlay: boolean;
  outputs: string[];
  levels: number[];
  values: Record<string, (number | null)[]>;
}

export interface ChartViewState {
  symbol: string;
  interval: string;
  from_time: number | null;
  to_time: number | null;
  indicators: IndicatorRef[];
  updated_by?: string;
  updated_at?: string;
}

export interface Quote {
  symbol: string;
  price: number;
  previous_close?: number;
  change?: number;
  change_pct?: number;
  day_high?: number;
  day_low?: number;
  volume?: number;
  source: string;
  feed?: string;
}

export interface Analysis {
  id: string;
  symbol: string;
  created_at: string;
  action: "strong_buy" | "buy" | "hold" | "watch" | "sell" | "strong_sell";
  horizon: string;
  confidence: number;
  price_at_creation: number | null;
  entry_low: number | null;
  entry_high: number | null;
  stop: number | null;
  targets: number[];
  summary: string;
  rationale?: string;
  scores: Record<string, number>;
  risks: string[];
  catalysts: string[];
  invalidation: string | null;
  annotation_tag: string | null;
  author: string;
  evaluation?: Evaluation;
}

export interface Evaluation {
  status: string;
  success?: boolean | null;
  return_pct?: number;
  directional_return_pct?: number | null;
  r_multiple?: number | null;
  max_favorable_pct?: number;
  max_adverse_pct?: number;
  alpha_vs_spy_pct?: number;
  event_date?: string;
  deadline?: string;
  last_price?: number;
}

export interface Note {
  id: string;
  symbol: string | null;
  title: string;
  content: string;
  tags: string[];
  author: string;
  created_at: string;
  updated_at: string;
}

export interface Watchlist {
  name: string;
  symbols: string[];
}

export interface SymbolMatch {
  symbol: string;
  name: string;
  exchange: string;
  type: string;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const body = await res.json();
      msg = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail ?? body);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json() as Promise<T>;
}

const q = (params: Record<string, string | number | undefined | null>) => {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  const s = sp.toString();
  return s ? `?${s}` : "";
};

const json = (body: unknown) => JSON.stringify(body);

export const api = {
  health: () => request<{ ok: boolean; bar_provider: string; feed?: string; alpaca_configured: boolean }>("/api/health"),
  search: (query: string) => request<SymbolMatch[]>(`/api/symbols/search${q({ q: query, limit: 12 })}`),
  quote: (symbol: string) => request<Quote>(`/api/quote/${encodeURIComponent(symbol)}`),
  bars: (symbol: string, interval: string, lookback: string) =>
    request<{ symbol: string; interval: string; source: string; bars: Bar[] }>(
      `/api/bars/${encodeURIComponent(symbol)}${q({ interval, lookback })}`,
    ),
  indicatorList: () => request<IndicatorSpec[]>("/api/indicators"),
  indicators: (symbol: string, interval: string, lookback: string, indicators: IndicatorRef[]) =>
    request<{ time: number[]; results: IndicatorResult[] }>(`/api/indicators/${encodeURIComponent(symbol)}`, {
      method: "POST",
      body: json({ interval, lookback, indicators }),
    }),
  summary: (symbol: string, interval = "1d") =>
    request<Record<string, any>>(`/api/summary/${encodeURIComponent(symbol)}${q({ interval })}`),
  fundamentals: (symbol: string) => request<Record<string, any>>(`/api/fundamentals/${encodeURIComponent(symbol)}`),
  earnings: (symbol: string) => request<Record<string, any>>(`/api/earnings/${encodeURIComponent(symbol)}`),
  news: (symbol: string) => request<{ items: any[]; source: string }>(`/api/news${q({ symbol, limit: 12 })}`),

  annotations: (symbol: string) => request<Annotation[]>(`/api/annotations${q({ symbol })}`),
  createAnnotation: (symbol: string, a: Partial<Annotation>) =>
    request<Annotation>(`/api/annotations/${encodeURIComponent(symbol)}`, { method: "POST", body: json(a) }),
  updateAnnotation: (id: string, patch: Partial<Annotation>) =>
    request<Annotation>(`/api/annotations/${id}`, { method: "PATCH", body: json(patch) }),
  deleteAnnotation: (id: string) => request<{ deleted: number }>(`/api/annotations/${id}`, { method: "DELETE" }),
  deleteAnnotations: (symbol: string, filter: { author?: string; tag?: string }) =>
    request<{ deleted: number }>(`/api/annotations${q({ symbol, ...filter })}`, { method: "DELETE" }),

  view: () => request<ChartViewState>("/api/view"),
  setView: (v: Partial<ChartViewState>) => request<ChartViewState>("/api/view", { method: "PUT", body: json(v) }),

  analyses: (symbol?: string) => request<Analysis[]>(`/api/analyses${q({ symbol, limit: 50 })}`),
  analysis: (id: string) => request<Analysis>(`/api/analyses/${id}`),
  trackRecord: () => request<{ summary: Record<string, any>; evaluations: any[] }>("/api/track-record"),

  notes: (symbol?: string) => request<Note[]>(`/api/notes${q({ symbol })}`),
  createNote: (n: { title: string; content: string; symbol?: string; tags?: string[] }) =>
    request<Note>("/api/notes", { method: "POST", body: json(n) }),
  deleteNote: (id: string) => request<{ deleted: string }>(`/api/notes/${id}`, { method: "DELETE" }),

  watchlists: () => request<Watchlist[]>("/api/watchlists"),
  updateWatchlist: (name: string, patch: { add?: string[]; remove?: string[] }) =>
    request<Watchlist>(`/api/watchlists/${encodeURIComponent(name)}`, { method: "PUT", body: json(patch) }),
};

export const LOOKBACK: Record<string, string> = {
  "1m": "5d", "5m": "30d", "15m": "45d", "30m": "55d", "1h": "1y", "1d": "5y", "1wk": "20y", "1mo": "max",
};

export const INTERVALS = ["1m", "5m", "15m", "30m", "1h", "1d", "1wk", "1mo"];

export const isIntraday = (interval: string) => /m$|h$/.test(interval) && interval !== "1mo";
