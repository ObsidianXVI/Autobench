import { useEffect, useMemo, useRef, useState } from "react";
import {
  CandlestickSeries,
  createChart,
  HistogramSeries,
  LineSeries,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type SeriesType,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { api, isIntraday, LOOKBACK, type Annotation, type AnnotationType, type Bar, type IndicatorRef, type IndicatorResult, type Point } from "../api";
import { AnnotationLayer, type Draft } from "./annotations";

const INTERVAL_SECONDS: Record<string, number> = {
  "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "1d": 86400, "1wk": 604800, "1mo": 2592000,
};
const PALETTE = ["#f59e0b", "#38bdf8", "#e879f9", "#34d399", "#f87171", "#a3e635", "#fb923c", "#60a5fa"];
const HIDDEN_OUTPUTS = new Set(["bandwidth", "percent_b", "direction", "slope_pct", "r2"]);
const HISTOGRAM_OUTPUTS = new Set(["histogram", "momentum", "squeeze_on"]);

/** Points a tool needs before it is committed. */
export const TOOL_POINTS: Partial<Record<AnnotationType, number>> = {
  horizontal_line: 1, horizontal_ray: 1, vertical_line: 1, text: 1, trendline: 2, ray: 2, rectangle: 2,
  fib_retracement: 2, arrow: 2, price_range: 2, long_position: 2, short_position: 2,
};

export type Tool = "select" | AnnotationType;

interface Props {
  symbol: string;
  interval: string;
  indicators: IndicatorRef[];
  annotations: Annotation[];
  visibleFrom: number | null;
  visibleTo: number | null;
  tool: Tool;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onDraw: (type: AnnotationType, points: Point[], text?: string) => void;
  onError: (msg: string) => void;
  onLoaded?: (info: { source: string; bars: number; last: Bar | null }) => void;
}

interface LegendItem {
  label: string;
  color: string;
  value: string;
}

function cssVar(name: string, fallback: string) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function themeOptions() {
  const bg = cssVar("--chart-bg", "#0b1220");
  const text = cssVar("--text-muted", "#94a3b8");
  const grid = cssVar("--chart-grid", "rgba(148,163,184,0.08)");
  const border = cssVar("--border", "#1e293b");
  return {
    layout: { background: { color: bg }, textColor: text, fontSize: 11, panes: { separatorColor: border } },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    rightPriceScale: { borderColor: border },
    timeScale: { borderColor: border },
  };
}

function formatter(intraday: boolean) {
  const opts: Intl.DateTimeFormatOptions = intraday
    ? { timeZone: "America/New_York", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }
    : { timeZone: "UTC", year: "numeric", month: "short", day: "numeric" };
  const f = new Intl.DateTimeFormat("en-US", opts);
  return (t: number) => f.format(new Date(t * 1000));
}

function tickFormatter(intraday: boolean) {
  const tz = intraday ? "America/New_York" : "UTC";
  const day = new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "short", day: "numeric" });
  const time = new Intl.DateTimeFormat("en-US", { timeZone: tz, hour: "2-digit", minute: "2-digit", hour12: false });
  const month = new Intl.DateTimeFormat("en-US", { timeZone: tz, month: "short" });
  const year = new Intl.DateTimeFormat("en-US", { timeZone: tz, year: "numeric" });
  // tickMarkType: 0 year, 1 month, 2 day of month, 3 time, 4 time with seconds
  return (t: Time, type: number) => {
    const d = new Date((t as number) * 1000);
    if (type === 0) return year.format(d);
    if (type === 1) return month.format(d);
    if (type === 2) return day.format(d);
    return time.format(d);
  };
}

const fmt = (v: number | null | undefined) =>
  v == null || Number.isNaN(v) ? "–" : Math.abs(v) >= 1e6 ? v.toExponential(2) : v.toFixed(Math.abs(v) >= 100 ? 2 : Math.abs(v) >= 1 ? 2 : 4);

export function ChartView(p: Props) {
  const box = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const volRef = useRef<ISeriesApi<"Histogram"> | null>(null);
  const layerRef = useRef<AnnotationLayer | null>(null);
  const indSeries = useRef<{ series: ISeriesApi<SeriesType>; label: string; color: string; values: (number | null)[] }[]>([]);
  const barsRef = useRef<Bar[]>([]);
  const draftRef = useRef<Draft | null>(null);
  const propsRef = useRef(p);
  propsRef.current = p;

  const [bars, setBars] = useState<Bar[]>([]);
  const [loading, setLoading] = useState(false);
  const [futureBars, setFutureBars] = useState(0); // how far drawings extend beyond the last bar
  const [legend, setLegend] = useState<{ bar: Bar | null; items: LegendItem[] }>({ bar: null, items: [] });
  const [textPrompt, setTextPrompt] = useState<{ x: number; y: number; point: Point } | null>(null);
  const intraday = isIntraday(p.interval);

  // --- create chart once ------------------------------------------------------------------
  useEffect(() => {
    const chart = createChart(box.current!, {
      autoSize: true,
      ...themeOptions(),
      crosshair: { mode: 0 },
      timeScale: { ...themeOptions().timeScale, rightOffset: 8, barSpacing: 7 },
    });
    const candles = chart.addSeries(CandlestickSeries, {
      upColor: "#22c55e", downColor: "#ef4444", borderVisible: false, wickUpColor: "#22c55e", wickDownColor: "#ef4444",
    });
    const vol = chart.addSeries(HistogramSeries, { priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    const layer = new AnnotationLayer();
    candles.attachPrimitive(layer);
    chartRef.current = chart;
    candleRef.current = candles;
    volRef.current = vol;
    layerRef.current = layer;
    if (import.meta.env.DEV) (window as any).__autobench = { chart, layer };

    const media = window.matchMedia("(prefers-color-scheme: light)");
    const onTheme = () => chart.applyOptions(themeOptions());
    media.addEventListener("change", onTheme);
    return () => {
      media.removeEventListener("change", onTheme);
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  // --- interval-dependent formatting -------------------------------------------------------
  useEffect(() => {
    chartRef.current?.applyOptions({
      localization: { timeFormatter: (t: Time) => formatter(intraday)(t as number) },
      timeScale: { timeVisible: intraday, secondsVisible: false, tickMarkFormatter: tickFormatter(intraday) },
    });
  }, [intraday]);

  // --- bars ----------------------------------------------------------------------------------
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api.bars(p.symbol, p.interval, LOOKBACK[p.interval] ?? "1y")
      .then((res) => {
        if (cancelled) return;
        const b = res.bars;
        barsRef.current = b;
        candleRef.current!.setData(b.map((x) => ({ time: x.time as UTCTimestamp, open: x.open, high: x.high, low: x.low, close: x.close })));
        volRef.current!.setData(b.map((x) => ({
          time: x.time as UTCTimestamp, value: x.volume,
          color: x.close >= x.open ? "rgba(34,197,94,0.35)" : "rgba(239,68,68,0.35)",
        })));
        layerRef.current!.mapper.set(b.map((x) => x.time), INTERVAL_SECONDS[p.interval] ?? 86400, isIntraday(p.interval));
        layerRef.current!.update();
        setBars(b);
        setLegend((l) => ({ ...l, bar: b[b.length - 1] ?? null }));
        propsRef.current.onLoaded?.({ source: res.source, bars: b.length, last: b[b.length - 1] ?? null });
        if (!propsRef.current.visibleFrom) {
          const n = b.length;
          const show = intraday ? 160 : p.interval === "1d" ? 180 : 150;
          chartRef.current!.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - show), to: n + 6 });
        }
      })
      .catch((e) => !cancelled && propsRef.current.onError(`Bars: ${e.message}`))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [p.symbol, p.interval]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- indicators ----------------------------------------------------------------------------
  const indKey = JSON.stringify(p.indicators);
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !bars.length) return;
    let cancelled = false;
    const clear = () => {
      for (const s of indSeries.current) chart.removeSeries(s.series);
      indSeries.current = [];
      for (let i = chart.panes().length - 1; i > 0; i--) chart.removePane(i);
    };
    if (!p.indicators.length) {
      clear();
      setLegend((l) => ({ ...l, items: [] }));
      return;
    }
    api.indicators(p.symbol, p.interval, LOOKBACK[p.interval] ?? "1y", p.indicators)
      .then((res) => {
        if (cancelled) return;
        clear();
        const times = res.time;
        let pane = 0;
        let colorIdx = 0;
        res.results.forEach((r: IndicatorResult) => {
          const target = r.overlay ? 0 : ++pane;
          let first: ISeriesApi<SeriesType> | null = null;
          for (const out of r.outputs) {
            if (r.overlay && HIDDEN_OUTPUTS.has(out)) continue;
            if (!r.overlay && (out === "direction")) continue;
            const vals = r.values[out];
            const color = PALETTE[colorIdx++ % PALETTE.length];
            const isHist = !r.overlay && HISTOGRAM_OUTPUTS.has(out);
            const dotted = r.name === "psar";
            const series: ISeriesApi<SeriesType> = isHist
              ? chart.addSeries(HistogramSeries, { priceLineVisible: false, lastValueVisible: false }, target)
              : chart.addSeries(LineSeries, {
                  color, lineWidth: r.overlay ? 1 : 2, priceLineVisible: false, lastValueVisible: !r.overlay,
                  crosshairMarkerVisible: false, lineVisible: !dotted, pointMarkersVisible: dotted, pointMarkersRadius: 1.5,
                  lineStyle: out === "middle" || out === "signal" ? LineStyle.Dashed : LineStyle.Solid,
                }, target);
            series.setData(
              times.map((t, i) => {
                const v = vals[i];
                if (v == null) return { time: t as UTCTimestamp };
                return isHist
                  ? { time: t as UTCTimestamp, value: v, color: v >= 0 ? "rgba(34,197,94,0.6)" : "rgba(239,68,68,0.6)" }
                  : { time: t as UTCTimestamp, value: v };
              }),
            );
            if (!first) first = series;
            indSeries.current.push({ series, label: `${r.label}${r.outputs.length > 1 ? "." + out : ""}`, color, values: vals });
          }
          if (first && !r.overlay) {
            for (const lv of r.levels) first.createPriceLine({ price: lv, color: "rgba(148,163,184,0.5)", lineStyle: LineStyle.Dotted, lineWidth: 1, axisLabelVisible: false });
          }
        });
        const panes = chart.panes();
        for (let i = 1; i < panes.length; i++) panes[i].setHeight(110);
        const last = times.length - 1;
        setLegend((l) => ({ ...l, items: indSeries.current.map((s) => ({ label: s.label, color: s.color, value: fmt(s.values[last]) })) }));
      })
      .catch((e) => !cancelled && propsRef.current.onError(`Indicators: ${e.message}`));
    return () => {
      cancelled = true;
    };
  }, [indKey, bars]); // eslint-disable-line react-hooks/exhaustive-deps

  // --- annotations -----------------------------------------------------------------------------
  useEffect(() => {
    const layer = layerRef.current;
    if (!layer) return;
    layer.selectedId = p.selectedId;
    const shown = p.annotations.filter((a) => !a.interval || a.interval === p.interval);
    layer.setAnnotations(shown);
    // leave room on the right for drawings that extend into the future (trade plans, event lines)
    const n = barsRef.current.length;
    if (n && chartRef.current) {
      let maxLg = n - 1;
      for (const a of shown) {
        if (!a.visible) continue;
        for (const pt of a.points) {
          if (pt.time == null) continue;
          const lg = layer.mapper.logical(pt.time);
          if (lg != null) maxLg = Math.max(maxLg, lg);
        }
      }
      const ahead = Math.min(76, Math.max(0, Math.ceil(maxLg - (n - 1))));
      chartRef.current.timeScale().applyOptions({ rightOffset: Math.max(8, ahead + 4) });
      setFutureBars(ahead);
    }
  }, [p.annotations, p.selectedId, p.interval, bars]);

  // --- visible range requested by the agent/user view -----------------------------------------
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart || !bars.length || !p.visibleFrom) return;
    const layer = layerRef.current!;
    const from = layer.mapper.logical(p.visibleFrom);
    const to = p.visibleTo ? layer.mapper.logical(p.visibleTo) : bars.length - 1 + Math.max(8, futureBars + 4);
    if (from != null && to != null) chart.timeScale().setVisibleLogicalRange({ from, to });
  }, [p.visibleFrom, p.visibleTo, bars, futureBars]);

  // --- mouse: drawing tools, selection, legend ------------------------------------------------
  useEffect(() => {
    const chart = chartRef.current!;
    const candles = candleRef.current!;
    const layer = layerRef.current!;

    const toPoint = (param: MouseEventParams<Time>): Point | null => {
      if (!param.point) return null;
      const lg = chart.timeScale().coordinateToLogical(param.point.x);
      if (lg == null) return null;
      const b = barsRef.current;
      const idx = Math.round(lg);
      let time = layer.mapper.time(lg);
      let price: number | null = candles.coordinateToPrice(param.point.y);
      // magnet: snap to the bar's time and to its OHLC when the cursor is within 8px
      if (idx >= 0 && idx < b.length) {
        time = b[idx].time;
        const bar = b[idx];
        let best: number | null = null;
        let bestD = 8;
        for (const v of [bar.open, bar.high, bar.low, bar.close]) {
          const y = candles.priceToCoordinate(v);
          if (y != null && Math.abs(y - param.point.y) < bestD) {
            bestD = Math.abs(y - param.point.y);
            best = v;
          }
        }
        if (best != null) price = best;
      }
      if (price == null || time == null) return null;
      return { time, price: Number(price.toFixed(4)) };
    };

    const onClick = (param: MouseEventParams<Time>) => {
      const { tool } = propsRef.current;
      if (!param.point) return;
      if (tool === "select") {
        propsRef.current.onSelect(layer.hit(param.point.x, param.point.y));
        return;
      }
      const pt = toPoint(param);
      if (!pt) return;
      if (tool === "text") {
        setTextPrompt({ x: param.point.x, y: param.point.y, point: pt });
        return;
      }
      const need = TOOL_POINTS[tool] ?? 2;
      const pts = [...(draftRef.current?.points.slice(0, -1) ?? []), pt];
      if (pts.length >= need) {
        draftRef.current = null;
        layer.draft = null;
        layer.update();
        propsRef.current.onDraw(tool, pts);
      } else {
        draftRef.current = { type: tool, points: [...pts, pt] };
        layer.draft = draftRef.current;
        layer.update();
      }
    };

    let raf = 0;
    const onMove = (param: MouseEventParams<Time>) => {
      const { tool } = propsRef.current;
      if (draftRef.current && param.point) {
        const pt = toPoint(param);
        if (pt) {
          draftRef.current.points[draftRef.current.points.length - 1] = pt;
          layer.update();
        }
      }
      if (param.point) {
        const hov = tool === "select" ? layer.hit(param.point.x, param.point.y) : null;
        if (hov !== layer.hoverId) {
          layer.hoverId = hov;
          layer.update();
        }
        box.current!.style.cursor = tool !== "select" ? "crosshair" : hov ? "pointer" : "default";
      }
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => {
        const b = barsRef.current;
        let idx = param.logical != null ? Math.round(param.logical as number) : b.length - 1;
        if (idx < 0 || idx >= b.length) idx = b.length - 1;
        const items = indSeries.current.map((s) => ({ label: s.label, color: s.color, value: fmt(s.values[idx]) }));
        setLegend({ bar: b[idx] ?? null, items });
      });
    };

    chart.subscribeClick(onClick);
    chart.subscribeCrosshairMove(onMove);
    return () => {
      chart.unsubscribeClick(onClick);
      chart.unsubscribeCrosshairMove(onMove);
      cancelAnimationFrame(raf);
    };
  }, []);

  // cancel an in-progress drawing when the tool changes or Escape is pressed
  useEffect(() => {
    draftRef.current = null;
    if (layerRef.current) {
      layerRef.current.draft = null;
      layerRef.current.update();
    }
    setTextPrompt(null);
  }, [p.tool, p.symbol]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && draftRef.current) {
        draftRef.current = null;
        layerRef.current!.draft = null;
        layerRef.current!.update();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const bar = legend.bar;
  const prev = useMemo(() => {
    if (!bar) return null;
    const i = bars.findIndex((b) => b.time === bar.time);
    return i > 0 ? bars[i - 1] : null;
  }, [bar, bars]);
  const chg = bar && prev ? ((bar.close - prev.close) / prev.close) * 100 : null;

  return (
    <div className="chart-wrap">
      <div ref={box} className="chart" />
      <div className="legend">
        <div className="legend-row">
          <span className="legend-symbol">{p.symbol}</span>
          <span className="muted">{p.interval}</span>
          {bar && (
            <span className="ohlc">
              {formatter(intraday)(bar.time)} O <b>{fmt(bar.open)}</b> H <b>{fmt(bar.high)}</b> L <b>{fmt(bar.low)}</b> C <b>{fmt(bar.close)}</b>
              {chg != null && <b className={chg >= 0 ? "up" : "down"}> {chg >= 0 ? "+" : ""}{chg.toFixed(2)}%</b>}
              <span className="muted"> Vol {Intl.NumberFormat("en", { notation: "compact" }).format(bar.volume)}</span>
            </span>
          )}
          {loading && <span className="muted">loading…</span>}
        </div>
        {legend.items.map((it) => (
          <div key={it.label} className="legend-row small">
            <span className="dot" style={{ background: it.color }} />
            <span className="muted">{it.label}</span> <span>{it.value}</span>
          </div>
        ))}
      </div>
      {textPrompt && (
        <form
          className="text-prompt"
          style={{ left: textPrompt.x, top: textPrompt.y }}
          onSubmit={(e) => {
            e.preventDefault();
            const v = (e.currentTarget.elements.namedItem("t") as HTMLInputElement).value.trim();
            if (v) propsRef.current.onDraw("text", [textPrompt.point], v);
            setTextPrompt(null);
          }}
        >
          <input name="t" autoFocus placeholder="Label… (Enter)" onKeyDown={(e) => e.key === "Escape" && setTextPrompt(null)} />
        </form>
      )}
    </div>
  );
}
