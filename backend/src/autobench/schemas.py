"""Pydantic models shared by the REST API and the MCP tools (so both validate identically)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .timeutil import parse_time

AnnotationType = Literal[
    "trendline", "ray", "extended_line", "horizontal_line", "horizontal_ray", "vertical_line",
    "rectangle", "parallel_channel", "fib_retracement", "price_range", "polyline", "arrow",
    "text", "marker", "long_position", "short_position", "callout",
]

# (min points, max points, needs time, needs price)
POINT_RULES: dict[str, tuple[int, int | None, bool, bool]] = {
    "trendline": (2, 2, True, True),
    "ray": (2, 2, True, True),
    "extended_line": (2, 2, True, True),
    "horizontal_line": (1, 1, False, True),
    "horizontal_ray": (1, 1, True, True),
    "vertical_line": (1, 1, True, False),
    "rectangle": (2, 2, True, True),
    "parallel_channel": (3, 3, True, True),
    "fib_retracement": (2, 2, True, True),
    "price_range": (2, 2, True, True),
    "polyline": (2, 50, True, True),
    "arrow": (2, 2, True, True),
    "text": (1, 1, True, True),
    "callout": (2, 2, True, True),
    "marker": (1, 1, True, True),
    "long_position": (3, 3, True, True),
    "short_position": (3, 3, True, True),
}

ANNOTATION_GUIDE = {
    "trendline": "2 points: segment between them",
    "ray": "2 points: starts at point 1, extends through point 2 to the right edge",
    "extended_line": "2 points: infinite line through both points",
    "horizontal_line": "1 point (price; time optional): full-width level (support/resistance)",
    "horizontal_ray": "1 point: level starting at time, extending right",
    "vertical_line": "1 point (time; price optional): event marker (earnings, news)",
    "rectangle": "2 points: opposite corners (zones: supply/demand, consolidation)",
    "parallel_channel": "3 points: p1-p2 define the base line, p3 sets the parallel line's offset",
    "fib_retracement": "2 points: swing start -> swing end; levels drawn between",
    "price_range": "2 points: measures price/percent change between them",
    "polyline": "2-50 points: connected path (e.g. projected path, wave counts)",
    "arrow": "2 points: arrow from p1 to p2",
    "text": "1 point: text label anchored at time/price (set text)",
    "callout": "2 points: p1 = anchor on chart, p2 = label position (set text)",
    "marker": "1 point: small icon at a bar; style.shape = arrow_up|arrow_down|circle|square, style.position = above|below|at",
    "long_position": "3 points: p1 = entry (time, price), p2 = (end time, stop price), p3 = (end time, target price)",
    "short_position": "3 points: same as long_position, stop above entry and target below",
}


class Point(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: int | str | None = Field(None, description="Epoch seconds or ISO date/datetime (e.g. '2025-03-14' or '2025-03-14T10:30-04:00')")
    price: float | None = None

    @field_validator("time", mode="before")
    @classmethod
    def _norm_time(cls, v):
        return parse_time(v) if v is not None and v != "" else None


class Style(BaseModel):
    model_config = ConfigDict(extra="allow")
    color: str | None = Field(None, description="CSS color, e.g. '#22c55e'")
    line_width: int | None = Field(None, ge=1, le=6)
    line_style: Literal["solid", "dashed", "dotted"] | None = None
    fill_color: str | None = Field(None, description="CSS color with alpha for zones, e.g. 'rgba(34,197,94,0.15)'")
    text_color: str | None = None
    font_size: int | None = Field(None, ge=8, le=32)
    extend_left: bool | None = None
    extend_right: bool | None = None
    shape: Literal["arrow_up", "arrow_down", "circle", "square"] | None = None
    position: Literal["above", "below", "at"] | None = None
    show_labels: bool | None = None


class AnnotationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: AnnotationType
    points: list[Point]
    text: str | None = Field(None, max_length=2000, description="Label/description shown with the drawing")
    style: Style = Field(default_factory=Style)
    interval: str | None = Field(None, description="Only show on this interval (e.g. '1d'); omit to show on all")
    tag: str | None = Field(None, max_length=120, description="Group label, e.g. 'analysis-2025-10-18' so related drawings can be managed together")
    layer: str | None = Field(None, max_length=80, description="Chart layer to put the drawing in (shown as a layer in the "
                                                                "user's Layers panel). Agent drawings default to the 'agent' layer")

    @model_validator(mode="after")
    def _check_points(self):
        lo, hi, need_t, need_p = POINT_RULES[self.type]
        n = len(self.points)
        if n < lo or (hi is not None and n > hi):
            want = str(lo) if lo == hi else f"{lo}-{hi}"
            raise ValueError(f"{self.type} needs {want} points ({ANNOTATION_GUIDE[self.type]}); got {n}")
        for i, p in enumerate(self.points):
            if need_t and p.time is None:
                raise ValueError(f"{self.type} point {i + 1} needs a time")
            if need_p and p.price is None:
                raise ValueError(f"{self.type} point {i + 1} needs a price")
        if self.type in ("long_position", "short_position"):
            e, s, t = (p.price for p in self.points)
            if self.type == "long_position" and not (s < e < t):
                raise ValueError("long_position requires stop < entry < target")
            if self.type == "short_position" and not (t < e < s):
                raise ValueError("short_position requires target < entry < stop")
        if self.type in ("text", "callout") and not self.text:
            raise ValueError(f"{self.type} requires text")
        return self


class AnnotationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    points: list[Point] | None = None
    text: str | None = None
    style: Style | None = None
    interval: str | None = None
    tag: str | None = None
    layer: str | None = None
    visible: bool | None = None


Action = Literal["strong_buy", "buy", "hold", "watch", "sell", "strong_sell"]
Horizon = Literal["intraday", "swing", "position", "long_term"]


class AnalysisIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    action: Action
    horizon: Horizon = Field(description="intraday (<2 days), swing (days-weeks), position (weeks-months), long_term (months-years)")
    confidence: float = Field(ge=0, le=1, description="Probability-like confidence 0..1 that the call plays out (calibrate honestly)")
    summary: str = Field(max_length=1000, description="One-paragraph thesis")
    rationale: str = Field(max_length=20000, description="Full reasoning in markdown: evidence for and against")
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = Field(None, description="Invalidation price")
    targets: list[float] = Field(default_factory=list)
    scores: dict[str, float] = Field(default_factory=dict, description="Component scores -100..100, e.g. {technical, fundamental, sentiment, valuation, risk}")
    risks: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    invalidation: str | None = Field(None, description="What would prove the thesis wrong")
    annotation_tag: str | None = Field(None, description="Tag of the chart drawings that illustrate this analysis")

    @model_validator(mode="after")
    def _check_levels(self):
        bullish = self.action in ("buy", "strong_buy")
        bearish = self.action in ("sell", "strong_sell")
        ref = self.entry_high if bullish else self.entry_low
        ref = ref or self.entry_low or self.entry_high
        if ref and self.stop:
            if bullish and self.stop >= ref:
                raise ValueError("For a buy, the stop must be below the entry")
            if bearish and self.stop <= ref:
                raise ValueError("For a sell, the stop must be above the entry")
        if ref and self.targets:
            if bullish and any(t <= ref for t in self.targets):
                raise ValueError("For a buy, targets must be above the entry")
            if bearish and any(t >= ref for t in self.targets):
                raise ValueError("For a sell, targets must be below the entry")
        return self


class ChartView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str | None = None
    interval: str | None = None
    from_time: int | str | None = Field(None, description="Left edge of the visible range")
    to_time: int | str | None = Field(None, description="Right edge of the visible range")
    indicators: list[dict[str, Any]] | None = Field(
        None, description="Indicators shown on the chart, e.g. [{'name':'sma','params':{'length':50}}, {'name':'rsi'}]")

    @field_validator("from_time", "to_time", mode="before")
    @classmethod
    def _norm(cls, v):
        return parse_time(v) if v not in (None, "") else None


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(max_length=200)
    content: str = Field(max_length=50000)
    symbol: str | None = None
    tags: list[str] = Field(default_factory=list)


# --- trading -------------------------------------------------------------------------------------

OrderType = Literal["market", "limit", "stop", "stop_limit"]


class OrderSpec(BaseModel):
    """An order request. stop_loss / take_profit make it a bracket: protective orders that activate
    once the entry fills and cancel each other."""

    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=12)
    side: Literal["buy", "sell"]
    qty: float = Field(gt=0, description="Whole shares")
    order_type: OrderType = "limit"
    limit_price: float | None = Field(None, gt=0)
    stop_price: float | None = Field(None, gt=0, description="Trigger price for stop / stop_limit orders")
    tif: Literal["day", "gtc"] = "day"
    outside_rth: bool = False
    stop_loss: float | None = Field(None, gt=0, description="Protective stop price (bracket)")
    take_profit: float | None = Field(None, gt=0, description="Profit target limit price (bracket)")
    rationale: str | None = Field(None, max_length=4000)
    analysis_id: str | None = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        return v.strip().upper()

    @model_validator(mode="after")
    def _check(self) -> "OrderSpec":
        if self.qty != int(self.qty):
            raise ValueError("qty must be whole shares")
        if self.order_type in ("limit", "stop_limit") and self.limit_price is None:
            raise ValueError(f"{self.order_type} orders need limit_price")
        if self.order_type in ("stop", "stop_limit") and self.stop_price is None:
            raise ValueError(f"{self.order_type} orders need stop_price")
        if self.order_type in ("market", "stop"):
            self.limit_price = None
        if self.order_type in ("market", "limit"):
            self.stop_price = None
        return self


class OrderModify(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qty: float | None = Field(None, gt=0)
    limit_price: float | None = Field(None, gt=0)
    stop_price: float | None = Field(None, gt=0)


class ClosePosition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    qty: float | None = Field(None, gt=0, description="Shares to close (default: the whole position)")
    order_type: Literal["market", "limit"] = "market"
    limit_price: float | None = Field(None, gt=0)
    rationale: str | None = Field(None, max_length=4000)


class RiskLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_order_value: float = Field(10_000, gt=0, description="Largest new-position order, in USD")
    max_position_pct: float = Field(20, gt=0, le=100, description="Largest position as % of net liquidation")
    max_daily_loss_pct: float = Field(2, gt=0, le=100, description="No new positions after losing this % today")
    max_orders_per_day: int = Field(30, ge=1)
    max_risk_pct: float = Field(1, gt=0, le=100, description="Largest entry-to-stop risk per order, % of equity")
    allow_short: bool = False
    allow_market_entries: bool = False
    allow_outside_rth: bool = False
    require_stop_for_agent: bool = True
    symbol_allowlist: list[str] = Field(default_factory=list)
    symbol_blocklist: list[str] = Field(default_factory=list)


class TradingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["confirm", "auto"] = Field("confirm", description="confirm: you approve each agent order; "
                                             "auto: agent orders within the limits go straight to a paper account")
    ticket_ttl_minutes: int = Field(240, ge=5, le=10080)
    limits: RiskLimits = Field(default_factory=RiskLimits)


class BrokerConnection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    broker: Literal["ibkr", "sim", "none"]
    host: str | None = None
    port: int | None = Field(None, ge=1, le=65535)
    client_id: int | None = Field(None, ge=0)
