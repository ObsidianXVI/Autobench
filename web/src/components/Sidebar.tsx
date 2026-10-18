import { useEffect, useState } from "react";
import { api, type Analysis, type Annotation, type Note, type Quote, type Watchlist } from "../api";
import { Markdown } from "./Markdown";

export interface Activity {
  id: number;
  ts: number;
  text: string;
  symbol?: string;
  kind: "agent" | "user" | "system";
}

const ACTION_LABEL: Record<string, string> = {
  strong_buy: "Strong buy", buy: "Buy", hold: "Hold", watch: "Watch", sell: "Sell", strong_sell: "Strong sell",
};

const n = (v: unknown, d = 2) => (typeof v === "number" && Number.isFinite(v) ? v.toFixed(d) : "–");
const pct = (v: unknown) => (typeof v === "number" ? `${v >= 0 ? "+" : ""}${v.toFixed(2)}%` : "–");
const compact = (v: unknown) => (typeof v === "number" ? Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 2 }).format(v) : "–");
const ago = (iso: string) => {
  const s = (Date.now() - new Date(iso.endsWith("Z") ? iso : iso + "Z").getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
};

function Score({ label, value }: { label: string; value: number | null | undefined }) {
  const v = typeof value === "number" ? value : 0;
  return (
    <div className="score">
      <span className="muted small">{label}</span>
      <div className="score-bar">
        <div className="score-mid" />
        <div className={`score-fill ${v >= 0 ? "pos" : "neg"}`}
             style={{ left: v >= 0 ? "50%" : `${50 + v / 2}%`, width: `${Math.abs(v) / 2}%` }} />
      </div>
      <span className="small">{typeof value === "number" ? value.toFixed(0) : "–"}</span>
    </div>
  );
}

// ------------------------------------------------------------------------------- overview

function Overview({ symbol, quote, version }: { symbol: string; quote: Quote | null; version: number }) {
  const [tech, setTech] = useState<Record<string, any> | null>(null);
  const [fund, setFund] = useState<Record<string, any> | null>(null);
  const [news, setNews] = useState<any[]>([]);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setTech(null);
    setFund(null);
    setErr(null);
    api.summary(symbol).then((t) => live && setTech(t)).catch((e) => live && setErr(e.message));
    api.fundamentals(symbol).then((f) => live && setFund(f)).catch(() => live && setFund({}));
    api.news(symbol).then((r) => live && setNews(r.items ?? [])).catch(() => live && setNews([]));
    return () => {
      live = false;
    };
  }, [symbol, version]);

  const t = tech;
  const f = fund;
  return (
    <div className="panel-body">
      {err && <div className="error small">{err}</div>}
      {f?.profile?.name && (
        <section>
          <div className="title-row"><b>{f.profile.name}</b><span className="muted small">{f.profile.sector} · {f.profile.industry}</span></div>
        </section>
      )}
      {t ? (
        <section>
          <h4>Technical read <span className="muted small">({t.as_of})</span></h4>
          <div className="bias">{t.mechanical_bias}</div>
          <Score label="Trend" value={t.trend?.score} />
          <Score label="Momentum" value={t.momentum?.score} />
          <dl className="kv">
            <dt>RSI 14</dt><dd>{n(t.momentum?.rsi14, 1)} <span className="muted">{t.momentum?.rsi_state}</span></dd>
            <dt>MA stack</dt><dd>{t.trend?.ma_stack ?? "–"}</dd>
            <dt>Structure</dt><dd>{t.trend?.structure}</dd>
            <dt>ADX</dt><dd>{n(t.trend?.adx, 1)} <span className="muted">{t.trend?.trend_strength}</span></dd>
            <dt>ATR 14</dt><dd>{n(t.volatility?.atr14)} ({n(t.volatility?.atr_pct)}%)</dd>
            <dt>Vol regime</dt><dd>{t.volatility?.regime ?? "–"}{t.volatility?.squeeze_on ? " · squeeze" : ""}</dd>
            <dt>Returns</dt><dd>1m {pct(t.returns_pct?.["1m"])} · 3m {pct(t.returns_pct?.["3m"])} · 1y {pct(t.returns_pct?.["1y"])}</dd>
            {t.relative_strength && <><dt>vs SPY 3m</dt><dd>{pct(t.relative_strength.outperformance_3m_pct)}</dd></>}
            {t.momentum?.divergence && <><dt>Divergence</dt><dd>{t.momentum.divergence}</dd></>}
          </dl>
          <div className="levels">
            {(t.levels?.resistances ?? []).slice(0, 3).reverse().map((l: any) => (
              <div key={`r${l.price}`} className="level res"><span>R {n(l.price)}</span><span className="muted small">{l.touches}× · {pct(l.distance_pct)}</span></div>
            ))}
            <div className="level cur"><span>Last {n(t.price)}</span></div>
            {(t.levels?.supports ?? []).slice(0, 3).map((l: any) => (
              <div key={`s${l.price}`} className="level sup"><span>S {n(l.price)}</span><span className="muted small">{l.touches}× · {pct(l.distance_pct)}</span></div>
            ))}
          </div>
          {t.recent_events?.length > 0 && (
            <>
              <h4>Recent events</h4>
              <ul className="events">
                {t.recent_events.slice(0, 6).map((e: any, i: number) => (
                  <li key={i}><span className="muted small">{e.date}</span> <span className={e.bias === "bullish" ? "up" : e.bias === "bearish" ? "down" : ""}>{e.event}</span></li>
                ))}
              </ul>
            </>
          )}
        </section>
      ) : !err && <div className="muted small">Loading technicals…</div>}
      {f?.valuation && (
        <section>
          <h4>Fundamentals</h4>
          <dl className="kv">
            <dt>Market cap</dt><dd>{compact(f.size?.market_cap)}</dd>
            <dt>P/E · fwd</dt><dd>{n(f.valuation.trailing_pe, 1)} · {n(f.valuation.forward_pe, 1)}</dd>
            <dt>EV/EBITDA</dt><dd>{n(f.valuation.ev_to_ebitda, 1)}</dd>
            <dt>Gross / op margin</dt><dd>{n(f.profitability?.gross_margin_pct, 1)}% / {n(f.profitability?.operating_margin_pct, 1)}%</dd>
            <dt>Revenue growth</dt><dd>{pct(f.growth?.revenue_growth_yoy_pct)}</dd>
            <dt>Analysts</dt><dd>{f.analysts?.recommendation ?? "–"} · target {n(f.analysts?.target_mean)} ({pct(f.analysts?.upside_to_mean_target_pct)})</dd>
            <dt>Next earnings</dt><dd>{f.other?.next_earnings ?? "–"}</dd>
            <dt>Short % float</dt><dd>{n(f.short_interest_and_ownership?.short_pct_of_float, 1)}%</dd>
          </dl>
        </section>
      )}
      {quote && <div className="muted small">Quote source: {quote.source}{quote.feed ? ` (${quote.feed})` : ""}</div>}
      {news.length > 0 && (
        <section>
          <h4>Headlines</h4>
          <ul className="news">
            {news.slice(0, 8).map((it, i) => (
              <li key={i}>
                <a href={it.url ?? "#"} target="_blank" rel="noreferrer">{it.headline}</a>
                <div className="muted small">{(it.published_at ?? "").slice(0, 10)} · {it.source}</div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------- agent

function AnalysisCard({ a, onShowTag }: { a: Analysis; onShowTag: (tag: string) => void }) {
  const [open, setOpen] = useState(false);
  const [full, setFull] = useState<Analysis | null>(null);
  useEffect(() => {
    if (open && !full) api.analysis(a.id).then(setFull).catch(() => {});
  }, [open, full, a.id]);
  const ev = full?.evaluation;
  const dir = a.action.includes("buy") ? "up" : a.action.includes("sell") ? "down" : "neutral";
  return (
    <article className="card">
      <header onClick={() => setOpen((o) => !o)}>
        <span className={`badge ${dir}`}>{ACTION_LABEL[a.action]}</span>
        <b>{a.symbol}</b>
        <span className="muted small">{a.horizon} · conf {(a.confidence * 100).toFixed(0)}%</span>
        <span className="spacer" />
        <span className="muted small">{ago(a.created_at)}</span>
      </header>
      <p className="small">{a.summary}</p>
      <div className="plan small">
        <span>@ {n(a.price_at_creation)}</span>
        {(a.entry_low || a.entry_high) && <span>entry {n(a.entry_low)}–{n(a.entry_high)}</span>}
        {a.stop && <span className="down">stop {n(a.stop)}</span>}
        {a.targets.length > 0 && <span className="up">targets {a.targets.map((t) => n(t)).join(" / ")}</span>}
      </div>
      {open && (
        <div className="card-detail">
          {ev && (
            <div className={`eval ${ev.success === true ? "up" : ev.success === false ? "down" : ""}`}>
              <b>{ev.status.replace("_", " ")}</b>
              {ev.return_pct != null && <span> · {pct(ev.return_pct)} since</span>}
              {ev.r_multiple != null && <span> · {ev.r_multiple}R</span>}
              {ev.alpha_vs_spy_pct != null && <span> · α {pct(ev.alpha_vs_spy_pct)}</span>}
              {ev.deadline && <span className="muted"> · horizon ends {ev.deadline}</span>}
            </div>
          )}
          {Object.keys(a.scores).length > 0 && (
            <div className="scores">{Object.entries(a.scores).map(([k, v]) => <Score key={k} label={k} value={v} />)}</div>
          )}
          {a.invalidation && <p className="small"><b>Invalidation:</b> {a.invalidation}</p>}
          {a.risks.length > 0 && <p className="small"><b>Risks:</b> {a.risks.join("; ")}</p>}
          {a.catalysts.length > 0 && <p className="small"><b>Catalysts:</b> {a.catalysts.join("; ")}</p>}
          {a.annotation_tag && <button className="btn small" onClick={() => onShowTag(a.annotation_tag!)}>Show drawings ({a.annotation_tag})</button>}
          {full?.rationale ? <Markdown text={full.rationale} /> : <div className="muted small">Loading rationale…</div>}
        </div>
      )}
    </article>
  );
}

function AgentPanel({ symbol, activity, version, onShowTag }: {
  symbol: string;
  activity: Activity[];
  version: number;
  onShowTag: (tag: string) => void;
}) {
  const [all, setAll] = useState(false);
  const [items, setItems] = useState<Analysis[]>([]);
  const [record, setRecord] = useState<Record<string, any> | null>(null);
  useEffect(() => {
    api.analyses(all ? undefined : symbol).then(setItems).catch(() => setItems([]));
  }, [symbol, all, version]);
  useEffect(() => {
    api.trackRecord().then((r) => setRecord(r.summary)).catch(() => setRecord(null));
  }, [version]);

  return (
    <div className="panel-body">
      <section>
        <h4>Live activity</h4>
        {activity.length === 0 ? (
          <div className="muted small">Agent actions (drawings, view changes, saved analyses) appear here in real time.
            Connect an MCP client to <code>/mcp</code>.</div>
        ) : (
          <ul className="activity">
            {activity.slice(0, 12).map((a) => (
              <li key={a.id} className={a.kind}>
                <span className="muted small">{new Date(a.ts * 1000).toLocaleTimeString()}</span> {a.text}
              </li>
            ))}
          </ul>
        )}
      </section>
      {record && record.total > 0 && (
        <section>
          <h4>Track record</h4>
          <dl className="kv">
            <dt>Calls</dt><dd>{record.total} ({record.resolved} resolved, {record.open} open)</dd>
            {record.hit_rate_pct != null && <><dt>Hit rate</dt><dd>{record.hit_rate_pct}%</dd></>}
            {record.avg_r_multiple != null && <><dt>Avg R</dt><dd>{record.avg_r_multiple}</dd></>}
            {record.avg_alpha_vs_spy_pct != null && <><dt>Avg α vs SPY</dt><dd>{pct(record.avg_alpha_vs_spy_pct)}</dd></>}
            {record.brier_score != null && <><dt>Brier</dt><dd>{record.brier_score}</dd></>}
          </dl>
        </section>
      )}
      <section>
        <div className="title-row">
          <h4>Analyses</h4>
          <label className="small muted"><input type="checkbox" checked={all} onChange={(e) => setAll(e.target.checked)} /> all symbols</label>
        </div>
        {items.length === 0 && <div className="muted small">No saved analyses{all ? "" : ` for ${symbol}`}. Ask the agent: “research {symbol} for a swing trade”.</div>}
        {items.map((a) => <AnalysisCard key={a.id} a={a} onShowTag={onShowTag} />)}
      </section>
    </div>
  );
}

// ------------------------------------------------------------------------------- drawings

function DrawingsPanel({ annotations, selectedId, onSelect, onToggle, onDelete, tagFilter, setTagFilter }: {
  annotations: Annotation[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  onToggle: (a: Annotation) => void;
  onDelete: (a: Annotation) => void;
  tagFilter: string | null;
  setTagFilter: (t: string | null) => void;
}) {
  const shown = tagFilter ? annotations.filter((a) => a.tag === tagFilter) : annotations;
  return (
    <div className="panel-body">
      {tagFilter && (
        <div className="chip">tag: {tagFilter} <button aria-label="clear filter" onClick={() => setTagFilter(null)}>×</button></div>
      )}
      {shown.length === 0 && <div className="muted small">No drawings. Use the toolbar on the left, or let the agent draw.</div>}
      <ul className="drawings">
        {shown.map((a) => (
          <li key={a.id} className={a.id === selectedId ? "selected" : ""} onClick={() => onSelect(a.id)}>
            <span className={`author ${a.author}`}>{a.author === "agent" ? "✦ AI" : "You"}</span>
            <span className="ellipsis">{a.type.replace(/_/g, " ")}{a.text ? ` — ${a.text}` : ""}</span>
            <span className="spacer" />
            <button className="icon" title={a.visible ? "Hide" : "Show"} onClick={(e) => { e.stopPropagation(); onToggle(a); }}>{a.visible ? "◉" : "○"}</button>
            <button className="icon" title="Delete" onClick={(e) => { e.stopPropagation(); onDelete(a); }}>×</button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ------------------------------------------------------------------------------- notes

function NotesPanel({ symbol, version }: { symbol: string; version: number }) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const load = () => api.notes(symbol).then(setNotes).catch(() => setNotes([]));
  useEffect(() => {
    load();
  }, [symbol, version]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="panel-body">
      <form className="note-form" onSubmit={async (e) => {
        e.preventDefault();
        if (!title.trim()) return;
        await api.createNote({ title, content, symbol });
        setTitle("");
        setContent("");
        load();
      }}>
        <input placeholder={`New note on ${symbol}…`} value={title} onChange={(e) => setTitle(e.target.value)} />
        {title && <textarea placeholder="Markdown…" value={content} onChange={(e) => setContent(e.target.value)} rows={4} />}
        {title && <button className="btn primary small" type="submit">Save</button>}
      </form>
      {notes.length === 0 && <div className="muted small">No notes for {symbol}. The agent stores theses, key dates and lessons here.</div>}
      {notes.map((nt) => (
        <article key={nt.id} className="card">
          <header onClick={() => setOpenId(openId === nt.id ? null : nt.id)}>
            <span className={`author ${nt.author}`}>{nt.author === "agent" ? "✦ AI" : "You"}</span>
            <b className="ellipsis">{nt.title}</b>
            <span className="spacer" />
            <span className="muted small">{ago(nt.updated_at)}</span>
          </header>
          {nt.tags.length > 0 && <div className="chips">{nt.tags.map((t) => <span key={t} className="chip small">{t}</span>)}</div>}
          {openId === nt.id && (
            <>
              <Markdown text={nt.content} />
              <button className="btn small" onClick={async () => { await api.deleteNote(nt.id); load(); }}>Delete</button>
            </>
          )}
        </article>
      ))}
    </div>
  );
}

// ------------------------------------------------------------------------------- watchlist

function WatchlistPanel({ symbol, onPick, version }: { symbol: string; onPick: (s: string) => void; version: number }) {
  const [lists, setLists] = useState<Watchlist[]>([]);
  const [name, setName] = useState("default");
  const [quotes, setQuotes] = useState<Record<string, Quote>>({});
  const [add, setAdd] = useState("");
  const load = () => api.watchlists().then((l) => {
    setLists(l);
    if (!l.find((x) => x.name === name) && l[0]) setName(l[0].name);
  });
  useEffect(() => {
    load();
  }, [version]); // eslint-disable-line react-hooks/exhaustive-deps
  const current = lists.find((l) => l.name === name);
  useEffect(() => {
    for (const s of current?.symbols ?? []) {
      api.quote(s).then((q) => setQuotes((m) => ({ ...m, [s]: q }))).catch(() => {});
    }
  }, [current?.symbols.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const update = async (patch: { add?: string[]; remove?: string[] }) => {
    await api.updateWatchlist(name, patch);
    load();
  };
  return (
    <div className="panel-body">
      <div className="row gap">
        <select value={name} onChange={(e) => setName(e.target.value)}>
          {lists.map((l) => <option key={l.name}>{l.name}</option>)}
        </select>
        <button className="btn small" onClick={() => update({ add: [symbol] })}>+ {symbol}</button>
      </div>
      <form className="row gap" onSubmit={(e) => {
        e.preventDefault();
        const syms = add.split(/[\s,]+/).filter(Boolean);
        if (syms.length) update({ add: syms });
        setAdd("");
      }}>
        <input placeholder="Add tickers (comma separated)" value={add} onChange={(e) => setAdd(e.target.value)} />
      </form>
      <ul className="watch">
        {(current?.symbols ?? []).map((s) => {
          const q = quotes[s];
          return (
            <li key={s} className={s === symbol ? "selected" : ""} onClick={() => onPick(s)}>
              <b>{s}</b>
              <span className="spacer" />
              <span>{q ? q.price.toFixed(2) : "…"}</span>
              <span className={`chg ${q && (q.change_pct ?? 0) >= 0 ? "up" : "down"}`}>{q?.change_pct != null ? pct(q.change_pct) : ""}</span>
              <button className="icon" title="Remove" onClick={(e) => { e.stopPropagation(); update({ remove: [s] }); }}>×</button>
            </li>
          );
        })}
      </ul>
      {current && current.symbols.length === 0 && <div className="muted small">Empty. Add tickers above, or ask the agent to build a watchlist.</div>}
    </div>
  );
}

// ------------------------------------------------------------------------------- shell

export type Tab = "overview" | "agent" | "drawings" | "notes" | "watchlist";

export function Sidebar(p: {
  tab: Tab;
  setTab: (t: Tab) => void;
  symbol: string;
  quote: Quote | null;
  annotations: Annotation[];
  selectedId: string | null;
  activity: Activity[];
  versions: { analyses: number; notes: number; watchlists: number; market: number };
  unseen: number;
  tagFilter: string | null;
  setTagFilter: (t: string | null) => void;
  onSelect: (id: string) => void;
  onToggle: (a: Annotation) => void;
  onDelete: (a: Annotation) => void;
  onPick: (s: string) => void;
}) {
  const tabs: [Tab, string][] = [["overview", "Overview"], ["agent", "Agent"], ["drawings", "Drawings"], ["notes", "Notes"], ["watchlist", "Watchlist"]];
  return (
    <aside className="sidebar">
      <div className="tabs" role="tablist">
        {tabs.map(([t, label]) => (
          <button key={t} role="tab" aria-selected={p.tab === t} className={p.tab === t ? "active" : ""} onClick={() => p.setTab(t)}>
            {label}{t === "agent" && p.unseen > 0 && <span className="pill">{p.unseen}</span>}
            {t === "drawings" && p.annotations.length > 0 && <span className="muted small"> {p.annotations.length}</span>}
          </button>
        ))}
      </div>
      {p.tab === "overview" && <Overview symbol={p.symbol} quote={p.quote} version={p.versions.market} />}
      {p.tab === "agent" && <AgentPanel symbol={p.symbol} activity={p.activity} version={p.versions.analyses}
                                        onShowTag={(t) => { p.setTagFilter(t); p.setTab("drawings"); }} />}
      {p.tab === "drawings" && <DrawingsPanel annotations={p.annotations} selectedId={p.selectedId} onSelect={p.onSelect}
                                              onToggle={p.onToggle} onDelete={p.onDelete} tagFilter={p.tagFilter} setTagFilter={p.setTagFilter} />}
      {p.tab === "notes" && <NotesPanel symbol={p.symbol} version={p.versions.notes} />}
      {p.tab === "watchlist" && <WatchlistPanel symbol={p.symbol} onPick={p.onPick} version={p.versions.watchlists} />}
    </aside>
  );
}
