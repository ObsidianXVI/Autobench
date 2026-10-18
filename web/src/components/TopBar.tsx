import { useEffect, useRef, useState } from "react";
import { api, INTERVALS, type IndicatorRef, type IndicatorSpec, type Quote, type SymbolMatch } from "../api";

function SymbolSearch({ symbol, onPick }: { symbol: string; onPick: (s: string) => void }) {
  const [q, setQ] = useState("");
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<SymbolMatch[]>([]);
  const [active, setActive] = useState(0);
  const timer = useRef<number>(undefined);

  useEffect(() => {
    window.clearTimeout(timer.current);
    if (!q.trim()) {
      setItems([]);
      return;
    }
    timer.current = window.setTimeout(() => {
      api.search(q).then((r) => {
        setItems(r);
        setActive(0);
      }).catch(() => setItems([]));
    }, 180);
  }, [q]);

  const pick = (s: string) => {
    onPick(s.toUpperCase());
    setQ("");
    setOpen(false);
  };

  return (
    <div className="search">
      <input
        aria-label="Search symbol"
        placeholder={`${symbol} — search ticker or company`}
        value={q}
        onChange={(e) => {
          setQ(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={() => window.setTimeout(() => setOpen(false), 150)}
        onKeyDown={(e) => {
          if (e.key === "ArrowDown") setActive((a) => Math.min(a + 1, items.length - 1));
          else if (e.key === "ArrowUp") setActive((a) => Math.max(a - 1, 0));
          else if (e.key === "Enter") pick(items[active]?.symbol ?? q.trim());
          else if (e.key === "Escape") setOpen(false);
        }}
      />
      {open && items.length > 0 && (
        <ul className="search-results" role="listbox">
          {items.map((it, i) => (
            <li key={it.symbol} className={i === active ? "active" : ""} onMouseDown={() => pick(it.symbol)}>
              <b>{it.symbol}</b>
              <span className="muted ellipsis">{it.name}</span>
              <span className="muted small">{it.exchange}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function IndicatorMenu({ specs, active, onChange }: {
  specs: IndicatorSpec[];
  active: IndicatorRef[];
  onChange: (v: IndicatorRef[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState("");
  const [editing, setEditing] = useState<IndicatorSpec | null>(null);
  const [params, setParams] = useState<Record<string, string>>({});
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (e: MouseEvent) => ref.current && !ref.current.contains(e.target as Node) && setOpen(false);
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, []);

  const label = (r: IndicatorRef) => {
    const vals = Object.values(r.params);
    return vals.length ? `${r.name}(${vals.join(",")})` : r.name;
  };

  const add = () => {
    if (!editing) return;
    const p: Record<string, number | string> = {};
    for (const [k, def] of Object.entries(editing.params)) {
      const raw = params[k];
      if (raw === undefined || raw === "" || raw === String(def)) continue;
      p[k] = typeof def === "number" ? Number(raw) : raw;
    }
    onChange([...active, { name: editing.name, params: p }]);
    setEditing(null);
  };

  const shown = specs.filter((s) => !filter || `${s.name} ${s.title}`.toLowerCase().includes(filter.toLowerCase()));

  return (
    <div className="menu" ref={ref}>
      <button className="btn" onClick={() => setOpen((o) => !o)}>ƒx Indicators{active.length ? ` · ${active.length}` : ""}</button>
      {open && (
        <div className="menu-panel">
          {active.length > 0 && (
            <div className="chips">
              {active.map((r, i) => (
                <span key={i} className="chip">
                  {label(r)}
                  <button aria-label="remove" onClick={() => onChange(active.filter((_, j) => j !== i))}>×</button>
                </span>
              ))}
            </div>
          )}
          {editing ? (
            <div className="ind-edit">
              <div className="ind-title">{editing.title}</div>
              <div className="muted small">{editing.description}</div>
              {Object.entries(editing.params).map(([k, def]) => (
                <label key={k} className="field">
                  <span>{k}</span>
                  <input defaultValue={String(def)} onChange={(e) => setParams((p) => ({ ...p, [k]: e.target.value }))} />
                </label>
              ))}
              <div className="row gap">
                <button className="btn primary" onClick={add}>Add</button>
                <button className="btn" onClick={() => setEditing(null)}>Back</button>
              </div>
            </div>
          ) : (
            <>
              <input className="menu-filter" placeholder="Filter…" value={filter} onChange={(e) => setFilter(e.target.value)} autoFocus />
              <ul className="menu-list">
                {shown.map((s) => (
                  <li key={s.name} onClick={() => {
                    setParams({});
                    setEditing(s);
                  }}>
                    <span>{s.title}</span>
                    <span className="muted small">{s.overlay ? "overlay" : "pane"} · {s.category}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
    </div>
  );
}

export function TopBar(p: {
  symbol: string;
  interval: string;
  quote: Quote | null;
  specs: IndicatorSpec[];
  indicators: IndicatorRef[];
  status: string;
  provider: string;
  onSymbol: (s: string) => void;
  onInterval: (i: string) => void;
  onIndicators: (v: IndicatorRef[]) => void;
}) {
  const q = p.quote;
  return (
    <header className="topbar">
      <div className="brand">
        <img src="/favicon.svg" alt="" width={22} height={22} />
        <span>Autobench</span>
      </div>
      <SymbolSearch symbol={p.symbol} onPick={p.onSymbol} />
      {q && (
        <div className="quote">
          <b>{p.symbol}</b>
          <span className="price">{q.price.toFixed(2)}</span>
          {q.change_pct != null && (
            <span className={q.change_pct >= 0 ? "up" : "down"}>
              {q.change != null && (q.change >= 0 ? "+" : "")}{q.change?.toFixed(2)} ({q.change_pct >= 0 ? "+" : ""}{q.change_pct.toFixed(2)}%)
            </span>
          )}
        </div>
      )}
      <div className="intervals" role="group" aria-label="Interval">
        {INTERVALS.map((iv) => (
          <button key={iv} className={iv === p.interval ? "active" : ""} onClick={() => p.onInterval(iv)}>{iv}</button>
        ))}
      </div>
      <IndicatorMenu specs={p.specs} active={p.indicators} onChange={p.onIndicators} />
      <div className="spacer" />
      <div className="status" title="Live connection to the backend (agent updates arrive here)">
        <span className={`dot ${p.status === "open" ? "ok" : "warn"}`} />
        <span className="muted small">{p.status === "open" ? "live" : p.status} · {p.provider}</span>
      </div>
    </header>
  );
}
