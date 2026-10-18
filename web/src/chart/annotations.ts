// Renders server-side annotations (agent + user drawings) on a lightweight-charts series.
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  Logical,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type { Annotation, AnnotationType, Point } from "../api";

export interface Draft {
  type: AnnotationType;
  points: Point[];
  text?: string | null;
}

const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const FIB_COLORS = ["#94a3b8", "#f87171", "#fb923c", "#facc15", "#4ade80", "#22d3ee", "#94a3b8"];
export const AGENT_COLOR = "#a78bfa";
export const USER_COLOR = "#38bdf8";

type XY = { x: number; y: number };

/** Maps epoch-second times to chart x coordinates, interpolating between bars and
 *  extrapolating beyond the loaded data (e.g. a trade plan that ends in the future). */
export class TimeMapper {
  times: number[] = [];
  barSeconds = 86400;
  intraday = false;

  set(times: number[], barSeconds: number, intraday: boolean) {
    this.times = times;
    this.barSeconds = barSeconds;
    this.intraday = intraday;
  }

  /** Approximate number of bars spanning `seconds` of calendar time (trading time only). */
  private barsFor(seconds: number): number {
    if (this.intraday) return (seconds * (6.5 / 24) * (5 / 7)) / this.barSeconds;
    if (this.barSeconds >= 7 * 86400) return seconds / this.barSeconds;
    return (seconds / 86400) * (5 / 7); // business days
  }

  logical(t: number): number | null {
    const ts = this.times;
    const n = ts.length;
    if (!n) return null;
    if (t <= ts[0]) return -this.barsFor(ts[0] - t);
    if (t >= ts[n - 1]) return n - 1 + this.barsFor(t - ts[n - 1]);
    let lo = 0;
    let hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (ts[mid] <= t) lo = mid;
      else hi = mid;
    }
    return lo + (t - ts[lo]) / (ts[hi] - ts[lo]);
  }

  /** Inverse of logical(): chart logical index -> epoch seconds. */
  time(logical: number): number | null {
    const ts = this.times;
    const n = ts.length;
    if (!n) return null;
    if (logical <= 0) return Math.round(ts[0] + logical * this.barSecondsCalendar());
    if (logical >= n - 1) return Math.round(ts[n - 1] + (logical - (n - 1)) * this.barSecondsCalendar());
    const i = Math.floor(logical);
    return Math.round(ts[i] + (logical - i) * (ts[i + 1] - ts[i]));
  }

  private barSecondsCalendar(): number {
    if (this.intraday) return this.barSeconds / ((6.5 / 24) * (5 / 7));
    if (this.barSeconds >= 7 * 86400) return this.barSeconds;
    return 86400 * (7 / 5);
  }
}

function dash(ctx: CanvasRenderingContext2D, style?: string) {
  ctx.setLineDash(style === "dashed" ? [6, 4] : style === "dotted" ? [2, 3] : []);
}

function fmtPrice(p: number): string {
  const a = Math.abs(p);
  return p.toFixed(a >= 1000 ? 1 : a >= 10 ? 2 : a >= 1 ? 3 : 4);
}

function label(ctx: CanvasRenderingContext2D, text: string, x: number, y: number, color: string,
               opts: { align?: "left" | "right" | "center"; bg?: string; fg?: string; size?: number } = {}) {
  const size = opts.size ?? 11;
  ctx.font = `500 ${size}px ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`;
  const w = ctx.measureText(text).width + 10;
  const h = size + 7;
  let left = x;
  if (opts.align === "right") left = x - w;
  if (opts.align === "center") left = x - w / 2;
  ctx.setLineDash([]);
  ctx.fillStyle = opts.bg ?? "rgba(15,23,42,0.85)";
  ctx.strokeStyle = color;
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.roundRect(left, y - h / 2, w, h, 4);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = opts.fg ?? "#e2e8f0";
  ctx.textBaseline = "middle";
  ctx.fillText(text, left + 5, y + 0.5);
}

function segDist(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = dx * dx + dy * dy;
  const t = len === 0 ? 0 : Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len));
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

/** Extend the line a->b to the given x (keeping slope). */
function lineAt(a: XY, b: XY, x: number): XY {
  if (b.x === a.x) return { x, y: b.y };
  return { x, y: a.y + ((b.y - a.y) * (x - a.x)) / (b.x - a.x) };
}

class Renderer implements IPrimitivePaneRenderer {
  constructor(private layer: AnnotationLayer) {}

  draw(target: CanvasRenderingTarget2D) {
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const L = this.layer;
      for (const a of L.annotations) {
        if (!a.visible) continue;
        L.drawOne(ctx, a, mediaSize.width, mediaSize.height, a.id === L.selectedId, a.id === L.hoverId);
      }
      if (L.draft && L.draft.points.length) {
        L.drawOne(ctx, { ...L.draft, id: "__draft", author: "user", style: { line_style: "dashed" } } as any,
          mediaSize.width, mediaSize.height, true, false);
      }
    });
  }
}

class PaneView implements IPrimitivePaneView {
  constructor(private layer: AnnotationLayer) {}
  renderer() {
    return new Renderer(this.layer);
  }
  zOrder() {
    return "top" as const;
  }
}

export class AnnotationLayer implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  private requestUpdate: (() => void) | null = null;
  private view = new PaneView(this);
  readonly mapper = new TimeMapper();

  annotations: Annotation[] = [];
  draft: Draft | null = null;
  selectedId: string | null = null;
  hoverId: string | null = null;
  private width = 0;
  private height = 0;

  attached(p: SeriesAttachedParameter<Time>) {
    this.chart = p.chart as IChartApi;
    this.series = p.series;
    this.requestUpdate = p.requestUpdate;
  }

  detached() {
    this.chart = this.series = this.requestUpdate = null;
  }

  paneViews() {
    return [this.view];
  }

  update() {
    this.requestUpdate?.();
  }

  setAnnotations(a: Annotation[]) {
    this.annotations = a;
    this.update();
  }

  // --- coordinates ----------------------------------------------------------------------

  x(t: number | null): number | null {
    if (t == null || !this.chart) return null;
    const lg = this.mapper.logical(t);
    if (lg == null) return null;
    return this.chart.timeScale().logicalToCoordinate(lg as Logical);
  }

  y(p: number | null): number | null {
    if (p == null || !this.series) return null;
    return this.series.priceToCoordinate(p);
  }

  pt(p: Point): XY | null {
    const x = this.x(p.time);
    const y = this.y(p.price);
    return x == null || y == null ? null : { x, y };
  }

  // --- drawing --------------------------------------------------------------------------

  drawOne(ctx: CanvasRenderingContext2D, a: Annotation, W: number, H: number, selected: boolean, hover: boolean) {
    this.width = W;
    this.height = H;
    const base = a.style?.color ?? (a.author === "agent" ? AGENT_COLOR : USER_COLOR);
    const lw = (a.style?.line_width ?? (a.type === "horizontal_line" ? 1.5 : 2)) + (selected || hover ? 1 : 0);
    ctx.save();
    ctx.strokeStyle = base;
    ctx.fillStyle = base;
    ctx.lineWidth = lw;
    dash(ctx, a.style?.line_style);
    const pts = a.points.map((p) => this.pt(p));
    const text = a.text ?? "";
    const badge = a.author === "agent" ? "✦ " : "";

    const line = (p: XY, q: XY) => {
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(q.x, q.y);
      ctx.stroke();
    };

    switch (a.type) {
      case "trendline":
      case "ray":
      case "extended_line":
      case "arrow": {
        const [p, q] = pts;
        if (!p || !q) break;
        let s = p;
        let e = q;
        if (a.type === "ray" || a.type === "extended_line" || a.style?.extend_right) e = lineAt(p, q, q.x >= p.x ? W : 0);
        if (a.type === "extended_line" || a.style?.extend_left) s = lineAt(p, q, q.x >= p.x ? 0 : W);
        line(s, e);
        if (a.type === "arrow") {
          const ang = Math.atan2(q.y - p.y, q.x - p.x);
          ctx.setLineDash([]);
          ctx.beginPath();
          ctx.moveTo(q.x, q.y);
          ctx.lineTo(q.x - 10 * Math.cos(ang - 0.4), q.y - 10 * Math.sin(ang - 0.4));
          ctx.lineTo(q.x - 10 * Math.cos(ang + 0.4), q.y - 10 * Math.sin(ang + 0.4));
          ctx.closePath();
          ctx.fill();
        }
        if (text) label(ctx, badge + text, (p.x + q.x) / 2, (p.y + q.y) / 2 - 12, base, { align: "center" });
        if (selected) this.handles(ctx, [p, q], base);
        break;
      }
      case "horizontal_line":
      case "horizontal_ray": {
        const y = this.y(a.points[0].price);
        if (y == null) break;
        const x0 = a.type === "horizontal_ray" ? (this.x(a.points[0].time) ?? 0) : 0;
        line({ x: x0, y }, { x: W, y });
        const priceTxt = fmtPrice(a.points[0].price as number);
        label(ctx, `${badge}${text ? text + " · " : ""}${priceTxt}`, W - 60, y, base, { align: "right" });
        break;
      }
      case "vertical_line": {
        const x = this.x(a.points[0].time);
        if (x == null) break;
        line({ x, y: 0 }, { x, y: H });
        if (text) label(ctx, badge + text, x + 4, 14, base);
        break;
      }
      case "rectangle": {
        const [p, q] = pts;
        if (!p || !q) break;
        ctx.fillStyle = a.style?.fill_color ?? hexAlpha(base, 0.12);
        const l = Math.min(p.x, q.x), t = Math.min(p.y, q.y), w = Math.abs(q.x - p.x), h = Math.abs(q.y - p.y);
        ctx.fillRect(l, t, w, h);
        ctx.strokeRect(l, t, w, h);
        if (text) label(ctx, badge + text, l + 4, t + 12, base);
        if (selected) this.handles(ctx, [p, q], base);
        break;
      }
      case "parallel_channel": {
        const [p, q, r] = pts;
        if (!p || !q || !r) break;
        const off = r.y - lineAt(p, q, r.x).y;
        const p2 = { x: p.x, y: p.y + off };
        const q2 = { x: q.x, y: q.y + off };
        ctx.fillStyle = a.style?.fill_color ?? hexAlpha(base, 0.08);
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(q.x, q.y);
        ctx.lineTo(q2.x, q2.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.closePath();
        ctx.fill();
        line(p, q);
        line(p2, q2);
        ctx.setLineDash([4, 4]);
        line({ x: p.x, y: (p.y + p2.y) / 2 }, { x: q.x, y: (q.y + q2.y) / 2 });
        if (text) label(ctx, badge + text, q.x + 4, q.y, base);
        if (selected) this.handles(ctx, [p, q, r], base);
        break;
      }
      case "fib_retracement": {
        const [p, q] = pts;
        if (!p || !q) break;
        const p0 = a.points[0].price as number;
        const p1 = a.points[1].price as number;
        const left = Math.min(p.x, q.x);
        FIB.forEach((r, i) => {
          const price = p1 - r * (p1 - p0);
          const y = this.y(price);
          if (y == null) return;
          ctx.strokeStyle = FIB_COLORS[i];
          ctx.setLineDash(r === 0 || r === 1 ? [] : [4, 3]);
          line({ x: left, y }, { x: W, y });
          if (a.style?.show_labels !== false) {
            ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
            ctx.fillStyle = FIB_COLORS[i];
            ctx.textBaseline = "bottom";
            ctx.fillText(`${r.toFixed(3)} (${fmtPrice(price)})`, left + 4, y - 2);
          }
        });
        ctx.strokeStyle = base;
        ctx.setLineDash([2, 3]);
        line(p, q);
        if (text) label(ctx, badge + text, left + 4, Math.min(p.y, q.y) - 14, base);
        if (selected) this.handles(ctx, [p, q], base);
        break;
      }
      case "price_range": {
        const [p, q] = pts;
        if (!p || !q) break;
        const d = (a.points[1].price as number) - (a.points[0].price as number);
        const pct = (d / (a.points[0].price as number)) * 100;
        const col = d >= 0 ? "#22c55e" : "#ef4444";
        ctx.fillStyle = hexAlpha(col, 0.12);
        ctx.fillRect(Math.min(p.x, q.x), Math.min(p.y, q.y), Math.abs(q.x - p.x), Math.abs(q.y - p.y));
        ctx.strokeStyle = col;
        line({ x: q.x, y: p.y }, { x: q.x, y: q.y });
        label(ctx, `${d >= 0 ? "+" : ""}${fmtPrice(d)} (${pct >= 0 ? "+" : ""}${pct.toFixed(2)}%)${text ? " · " + text : ""}`,
          q.x, q.y + (d >= 0 ? -14 : 14), col, { align: "center" });
        break;
      }
      case "polyline": {
        const ok = pts.filter((p): p is XY => !!p);
        if (ok.length < 2) break;
        ctx.beginPath();
        ok.forEach((p, i) => (i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
        ctx.stroke();
        if (text) label(ctx, badge + text, ok[ok.length - 1].x + 4, ok[ok.length - 1].y, base);
        if (selected) this.handles(ctx, ok, base);
        break;
      }
      case "text": {
        const p = pts[0];
        if (!p) break;
        label(ctx, badge + text, p.x, p.y, base, { size: a.style?.font_size ?? 12, fg: a.style?.text_color });
        break;
      }
      case "callout": {
        const [p, q] = pts;
        if (!p || !q) break;
        ctx.setLineDash([]);
        line(p, q);
        ctx.beginPath();
        ctx.arc(p.x, p.y, 3, 0, Math.PI * 2);
        ctx.fill();
        label(ctx, badge + text, q.x, q.y, base, { size: a.style?.font_size ?? 12, fg: a.style?.text_color });
        break;
      }
      case "marker": {
        const p = pts[0];
        if (!p) break;
        const shape = a.style?.shape ?? "circle";
        const pos = a.style?.position ?? (shape === "arrow_up" ? "below" : shape === "arrow_down" ? "above" : "at");
        const y = p.y + (pos === "below" ? 14 : pos === "above" ? -14 : 0);
        ctx.setLineDash([]);
        ctx.beginPath();
        if (shape === "arrow_up") {
          ctx.moveTo(p.x, y - 7);
          ctx.lineTo(p.x - 6, y + 5);
          ctx.lineTo(p.x + 6, y + 5);
        } else if (shape === "arrow_down") {
          ctx.moveTo(p.x, y + 7);
          ctx.lineTo(p.x - 6, y - 5);
          ctx.lineTo(p.x + 6, y - 5);
        } else if (shape === "square") {
          ctx.rect(p.x - 5, y - 5, 10, 10);
        } else {
          ctx.arc(p.x, y, 5, 0, Math.PI * 2);
        }
        ctx.closePath();
        ctx.fill();
        if (text) label(ctx, text, p.x, y + (pos === "above" ? -16 : 16), base, { align: "center", size: 10 });
        break;
      }
      case "long_position":
      case "short_position": {
        const [e, s, t] = pts;
        if (!e || !s || !t) break;
        const x1 = e.x;
        const x2 = Math.max(s.x, x1 + 30);
        const entry = a.points[0].price as number;
        const stop = a.points[1].price as number;
        const tgt = a.points[2].price as number;
        const rr = Math.abs(tgt - entry) / Math.abs(entry - stop);
        ctx.setLineDash([]);
        ctx.fillStyle = "rgba(34,197,94,0.18)";
        ctx.fillRect(x1, Math.min(e.y, t.y), x2 - x1, Math.abs(t.y - e.y));
        ctx.fillStyle = "rgba(239,68,68,0.18)";
        ctx.fillRect(x1, Math.min(e.y, s.y), x2 - x1, Math.abs(s.y - e.y));
        ctx.strokeStyle = "#94a3b8";
        ctx.lineWidth = 1;
        line({ x: x1, y: e.y }, { x: x2, y: e.y });
        const pct = (v: number) => `${v >= entry ? "+" : ""}${(((v - entry) / entry) * 100).toFixed(2)}%`;
        label(ctx, `${badge}Target ${fmtPrice(tgt)} (${pct(tgt)}) · ${rr.toFixed(2)}R`, x1 + 4, t.y, "#22c55e");
        label(ctx, `Stop ${fmtPrice(stop)} (${pct(stop)})`, x1 + 4, s.y, "#ef4444");
        label(ctx, `${a.type === "long_position" ? "Long" : "Short"} ${fmtPrice(entry)}${text ? " · " + text : ""}`,
          x1 + 4, e.y, "#94a3b8");
        if (selected) this.handles(ctx, [e, { x: x2, y: s.y }, { x: x2, y: t.y }], base);
        break;
      }
    }
    ctx.restore();
  }

  private handles(ctx: CanvasRenderingContext2D, pts: XY[], color: string) {
    ctx.setLineDash([]);
    for (const p of pts) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, 4, 0, Math.PI * 2);
      ctx.fillStyle = "#0f172a";
      ctx.fill();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
  }

  // --- hit testing ----------------------------------------------------------------------

  /** Id of the topmost visible annotation within `tol` px of (x, y). */
  hit(x: number, y: number, tol = 6): string | null {
    const P = { x, y };
    const W = this.width || 10000;
    for (let i = this.annotations.length - 1; i >= 0; i--) {
      const a = this.annotations[i];
      if (!a.visible) continue;
      const pts = a.points.map((p) => this.pt(p));
      let d = Infinity;
      switch (a.type) {
        case "horizontal_line":
        case "horizontal_ray": {
          const yy = this.y(a.points[0].price);
          if (yy != null) d = Math.abs(yy - y);
          break;
        }
        case "vertical_line": {
          const xx = this.x(a.points[0].time);
          if (xx != null) d = Math.abs(xx - x);
          break;
        }
        case "rectangle":
        case "price_range":
        case "fib_retracement": {
          const [p, q] = pts;
          if (p && q) {
            const inside = x >= Math.min(p.x, q.x) - tol && x <= Math.max(p.x, q.x, a.type === "fib_retracement" ? W : 0) + tol &&
              y >= Math.min(p.y, q.y) - tol && y <= Math.max(p.y, q.y) + tol;
            d = inside ? 0 : Infinity;
          }
          break;
        }
        case "long_position":
        case "short_position": {
          const [e, s, t] = pts;
          if (e && s && t) {
            const inside = x >= e.x - tol && x <= Math.max(s.x, e.x + 30) + tol &&
              y >= Math.min(s.y, t.y) - tol && y <= Math.max(s.y, t.y) + tol;
            d = inside ? 0 : Infinity;
          }
          break;
        }
        case "text":
        case "marker":
        case "callout": {
          const p = pts[a.type === "callout" ? 1 : 0];
          if (p) d = Math.hypot(p.x - x, p.y - y) - 10;
          break;
        }
        default: {
          const ok = pts.filter((p): p is XY => !!p);
          for (let k = 0; k + 1 < ok.length; k++) {
            let s = ok[k], e = ok[k + 1];
            if (a.type === "ray" || a.type === "extended_line") e = lineAt(s, e, e.x >= s.x ? W : 0);
            if (a.type === "extended_line") s = lineAt(ok[0], ok[1], ok[1].x >= ok[0].x ? 0 : W);
            d = Math.min(d, segDist(P, s, e));
          }
          if (a.type === "parallel_channel" && ok.length === 3) {
            const off = ok[2].y - lineAt(ok[0], ok[1], ok[2].x).y;
            d = Math.min(d, segDist(P, { x: ok[0].x, y: ok[0].y + off }, { x: ok[1].x, y: ok[1].y + off }));
          }
        }
      }
      if (d <= tol) return a.id;
    }
    return null;
  }
}

function hexAlpha(color: string, alpha: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(color);
  if (!m) return color;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
}
