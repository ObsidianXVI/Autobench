import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Annotation, type AnnotationType, type ChartViewState, type IndicatorRef, type IndicatorSpec, type Point, type Quote } from "./api";
import { ChartView, type Tool } from "./chart/ChartView";
import { Sidebar, type Activity, type Tab } from "./components/Sidebar";
import { Toolbar } from "./components/Toolbar";
import { TopBar } from "./components/TopBar";
import { useEventStream, type ServerEvent } from "./ws";

const DEFAULT_VIEW: ChartViewState = { symbol: "SPY", interval: "1d", from_time: null, to_time: null, indicators: [] };

let activitySeq = 0;

export default function App() {
  const [view, setView] = useState<ChartViewState | null>(null);
  const [quote, setQuote] = useState<Quote | null>(null);
  const [specs, setSpecs] = useState<IndicatorSpec[]>([]);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tool, setTool] = useState<Tool>("select");
  const [tab, setTab] = useState<Tab>("overview");
  const [activity, setActivity] = useState<Activity[]>([]);
  const [unseen, setUnseen] = useState(0);
  const [tagFilter, setTagFilter] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [provider, setProvider] = useState("…");
  const [versions, setVersions] = useState({ analyses: 0, notes: 0, watchlists: 0, market: 0 });
  const viewRef = useRef(view);
  viewRef.current = view;
  const tabRef = useRef(tab);
  tabRef.current = tab;

  const symbol = view?.symbol ?? DEFAULT_VIEW.symbol;
  const interval = view?.interval ?? DEFAULT_VIEW.interval;

  const notify = useCallback((msg: string) => {
    setToast(msg);
    window.setTimeout(() => setToast((t) => (t === msg ? null : t)), 5000);
  }, []);

  // initial load
  useEffect(() => {
    api.view().then(setView).catch(() => setView(DEFAULT_VIEW));
    api.indicatorList().then(setSpecs).catch(() => {});
    api.health().then((h) => setProvider(h.bar_provider + (h.feed ? `/${h.feed}` : ""))).catch(() => setProvider("offline"));
  }, []);

  // quote polling
  useEffect(() => {
    let live = true;
    const load = () => api.quote(symbol).then((q) => live && setQuote(q)).catch(() => live && setQuote(null));
    setQuote(null);
    load();
    const id = window.setInterval(load, 15000);
    return () => {
      live = false;
      window.clearInterval(id);
    };
  }, [symbol]);

  // annotations for the current symbol
  useEffect(() => {
    setSelectedId(null);
    setTagFilter(null);
    api.annotations(symbol).then(setAnnotations).catch((e) => notify(e.message));
  }, [symbol, notify]);

  const pushActivity = useCallback((text: string, kind: Activity["kind"], sym?: string) => {
    setActivity((list) => {
      const now = Date.now() / 1000;
      const last = list[0];
      // collapse bursts ("drew 3 objects") from the same batch
      if (last && last.kind === kind && last.symbol === sym && now - last.ts < 3 && last.text.startsWith("Drew") && text.startsWith("Drew")) {
        const m = /Drew (\d+) objects?/.exec(last.text);
        const count = (m ? Number(m[1]) : 1) + 1;
        return [{ ...last, text: `Drew ${count} objects on ${sym}`, ts: now }, ...list.slice(1)];
      }
      return [{ id: ++activitySeq, ts: now, text, kind, symbol: sym }, ...list].slice(0, 100);
    });
    if (kind === "agent" && tabRef.current !== "agent") setUnseen((u) => u + 1);
  }, []);

  const onEvent = useCallback((e: ServerEvent) => {
    const cur = viewRef.current;
    const d = e.data ?? {};
    switch (e.type) {
      case "hello":
        if (d.view) setView(d.view);
        break;
      case "view.changed":
        setView(d);
        if (d.updated_by === "agent") pushActivity(`Switched chart to ${d.symbol} · ${d.interval}`, "agent", d.symbol);
        break;
      case "annotation.created":
        if (cur && d.symbol === cur.symbol) setAnnotations((list) => (list.some((a) => a.id === d.id) ? list : [...list, d]));
        if (d.author === "agent") pushActivity(`Drew 1 object on ${d.symbol}`, "agent", d.symbol);
        break;
      case "annotation.updated":
        setAnnotations((list) => list.map((a) => (a.id === d.id ? d : a)));
        break;
      case "annotation.deleted":
        setAnnotations((list) => list.filter((a) => a.id !== d.id));
        break;
      case "analysis.created":
        setVersions((v) => ({ ...v, analyses: v.analyses + 1 }));
        pushActivity(`Saved analysis: ${String(d.action).replace("_", " ").toUpperCase()} ${d.symbol} (${Math.round(d.confidence * 100)}%)`,
          d.author === "agent" ? "agent" : "user", d.symbol);
        break;
      case "note.saved":
      case "note.deleted":
        setVersions((v) => ({ ...v, notes: v.notes + 1 }));
        if (e.type === "note.saved" && d.author === "agent") pushActivity(`Saved note: ${d.title}`, "agent", d.symbol ?? undefined);
        break;
      case "watchlist.updated":
      case "watchlist.deleted":
        setVersions((v) => ({ ...v, watchlists: v.watchlists + 1 }));
        break;
    }
  }, [pushActivity]);

  const status = useEventStream(onEvent);

  useEffect(() => {
    if (tab === "agent") setUnseen(0);
  }, [tab]);

  const changeView = (patch: Partial<ChartViewState>) => {
    const next = { ...(viewRef.current ?? DEFAULT_VIEW), ...patch };
    if (patch.symbol && patch.symbol !== viewRef.current?.symbol) {
      next.from_time = null;
      next.to_time = null;
    }
    if (patch.interval) {
      next.from_time = null;
      next.to_time = null;
    }
    setView(next);
    api.setView({ symbol: next.symbol, interval: next.interval, indicators: next.indicators,
                  ...(patch.symbol || patch.interval ? { from_time: null, to_time: null } : {}) })
      .catch((e) => notify(e.message));
  };

  const onDraw = async (type: AnnotationType, points: Point[], text?: string) => {
    let pts = points;
    if ((type === "long_position" || type === "short_position") && points.length === 2) {
      // two clicks: entry then target; stop defaults to half the target distance (2R)
      const [e, t] = points;
      const stop = (e.price as number) - ((t.price as number) - (e.price as number)) / 2;
      pts = [e, { time: t.time, price: Number(stop.toFixed(4)) }, { time: t.time, price: t.price }];
      const ok = type === "long_position" ? (t.price as number) > (e.price as number) : (t.price as number) < (e.price as number);
      if (!ok) {
        notify(type === "long_position" ? "Long: click the target above the entry" : "Short: click the target below the entry");
        return;
      }
    }
    try {
      const created = await api.createAnnotation(symbol, { type, points: pts, ...(text ? { text } : {}) });
      setAnnotations((list) => (list.some((a) => a.id === created.id) ? list : [...list, created]));
      setSelectedId(created.id);
      setTool("select");
    } catch (e: any) {
      notify(e.message);
    }
  };

  const deleteAnnotation = useCallback(async (a: Annotation) => {
    try {
      await api.deleteAnnotation(a.id);
      setAnnotations((list) => list.filter((x) => x.id !== a.id));
      setSelectedId((s) => (s === a.id ? null : s));
    } catch (e: any) {
      notify(e.message);
    }
  }, [notify]);

  // Delete / Backspace removes the selected drawing (when not typing)
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      if ((e.key === "Delete" || e.key === "Backspace") && selectedId) {
        const a = annotations.find((x) => x.id === selectedId);
        if (a) deleteAnnotation(a);
      }
      if (e.key === "Escape") {
        setTool("select");
        setSelectedId(null);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedId, annotations, deleteAnnotation]);

  if (!view) return <div className="boot">Connecting to Autobench…</div>;

  return (
    <div className="app">
      <TopBar
        symbol={symbol}
        interval={interval}
        quote={quote}
        specs={specs}
        indicators={view.indicators ?? []}
        status={status}
        provider={provider}
        onSymbol={(s) => changeView({ symbol: s })}
        onInterval={(i) => changeView({ interval: i })}
        onIndicators={(ind: IndicatorRef[]) => changeView({ indicators: ind })}
      />
      <main className="main">
        <Toolbar
          tool={tool}
          onTool={setTool}
          onClearMine={() => api.deleteAnnotations(symbol, { author: "user" }).then((r) => notify(`Removed ${r.deleted} of your drawings`))}
          onClearAgent={() => api.deleteAnnotations(symbol, { author: "agent" }).then((r) => notify(`Removed ${r.deleted} agent drawings`))}
        />
        <ChartView
          symbol={symbol}
          interval={interval}
          indicators={view.indicators ?? []}
          annotations={annotations}
          visibleFrom={view.from_time}
          visibleTo={view.to_time}
          tool={tool}
          selectedId={selectedId}
          onSelect={setSelectedId}
          onDraw={onDraw}
          onError={notify}
          onLoaded={(info) => setProvider((p) => (info.source && !p.startsWith(info.source) ? info.source : p))}
        />
        <Sidebar
          tab={tab}
          setTab={setTab}
          symbol={symbol}
          quote={quote}
          annotations={annotations}
          selectedId={selectedId}
          activity={activity}
          versions={versions}
          unseen={unseen}
          tagFilter={tagFilter}
          setTagFilter={setTagFilter}
          onSelect={setSelectedId}
          onToggle={(a) => api.updateAnnotation(a.id, { visible: !a.visible }).catch((e) => notify(e.message))}
          onDelete={deleteAnnotation}
          onPick={(s) => changeView({ symbol: s })}
        />
      </main>
      {toast && <div className="toast" role="status">{toast}</div>}
    </div>
  );
}
