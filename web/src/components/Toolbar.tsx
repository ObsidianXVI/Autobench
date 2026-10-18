import type { Tool } from "../chart/ChartView";

const TOOLS: { tool: Tool; title: string; icon: string }[] = [
  { tool: "select", title: "Select (click a drawing; Delete removes it)", icon: "M5 3l14 8-6 2-2 6z" },
  { tool: "trendline", title: "Trend line", icon: "M4 19L20 5" },
  { tool: "ray", title: "Ray", icon: "M4 18l16-9M18 7l3 1-1 3" },
  { tool: "horizontal_line", title: "Horizontal line", icon: "M3 12h18" },
  { tool: "horizontal_ray", title: "Horizontal ray", icon: "M6 12h15M6 9v6" },
  { tool: "vertical_line", title: "Vertical line", icon: "M12 3v18" },
  { tool: "rectangle", title: "Rectangle / zone", icon: "M4 6h16v12H4z" },
  { tool: "fib_retracement", title: "Fibonacci retracement", icon: "M3 5h18M3 9h18M3 13h18M3 19h18" },
  { tool: "price_range", title: "Measure (price range)", icon: "M12 3v18M8 7l4-4 4 4M8 17l4 4 4-4" },
  { tool: "arrow", title: "Arrow", icon: "M5 19L19 5M11 5h8v8" },
  { tool: "long_position", title: "Long position (click entry, then target; stop defaults to 2R)", icon: "M4 4h16v8H4zM4 12h16v6H4z" },
  { tool: "short_position", title: "Short position (click entry, then target)", icon: "M4 6h16v6H4zM4 12h16v8H4z" },
  { tool: "text", title: "Text label", icon: "M5 6h14M12 6v13" },
];

export function Toolbar({ tool, onTool, onClearMine, onClearAgent }: {
  tool: Tool;
  onTool: (t: Tool) => void;
  onClearMine: () => void;
  onClearAgent: () => void;
}) {
  return (
    <nav className="toolbar" aria-label="Drawing tools">
      {TOOLS.map((t) => (
        <button key={t.tool} title={t.title} aria-label={t.title} className={tool === t.tool ? "active" : ""}
                onClick={() => onTool(t.tool)}>
          <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8"
               strokeLinecap="round" strokeLinejoin="round"><path d={t.icon} /></svg>
        </button>
      ))}
      <div className="toolbar-sep" />
      <button title="Remove my drawings on this symbol" aria-label="Remove my drawings" onClick={onClearMine}>
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M5 7h14M9 7V4h6v3M7 7l1 13h8l1-13" /></svg>
      </button>
      <button title="Remove the agent's drawings on this symbol" aria-label="Remove agent drawings" className="agent" onClick={onClearAgent}>
        <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="1.8"><path d="M12 3l2 5 5 2-5 2-2 5-2-5-5-2 5-2z" /><path d="M4 20l16-16" /></svg>
      </button>
    </nav>
  );
}
