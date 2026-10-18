"""Chart annotations (drawings) stored server-side so the agent and the UI share them."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select

from ..db import AnnotationRow, session_scope, utcnow
from ..schemas import AnnotationIn, AnnotationPatch, Point
from ..timeutil import get_interval
from .events import bus
from .market import normalize_symbol

MAX_PER_SYMBOL = 500


def to_dict(r: AnnotationRow) -> dict:
    return {
        "id": r.id, "symbol": r.symbol, "interval": r.interval, "type": r.type, "points": r.points,
        "text": r.text, "style": r.style, "author": r.author, "tag": r.tag, "visible": r.visible,
        "created_at": r.created_at.isoformat() + "Z", "updated_at": r.updated_at.isoformat() + "Z",
    }


def _points(points: list[Point]) -> list[dict]:
    return [p.model_dump() for p in points]


def create(symbol: str, items: list[AnnotationIn], author: str = "agent") -> list[dict]:
    symbol = normalize_symbol(symbol)
    out = []
    with session_scope() as s:
        count = len(s.execute(select(AnnotationRow.id).where(AnnotationRow.symbol == symbol)).all())
        if count + len(items) > MAX_PER_SYMBOL:
            raise ValueError(f"Too many annotations on {symbol} ({count}); delete some first")
        for a in items:
            iv = get_interval(a.interval).name if a.interval else None
            row = AnnotationRow(
                id=uuid.uuid4().hex[:12], symbol=symbol, interval=iv, type=a.type, points=_points(a.points),
                text=a.text, style=a.style.model_dump(exclude_none=True), author=author, tag=a.tag, visible=True,
                created_at=utcnow(), updated_at=utcnow(),
            )
            s.add(row)
            s.flush()
            out.append(to_dict(row))
    for d in out:
        bus.publish("annotation.created", d)
    return out


def list_(symbol: str | None = None, interval: str | None = None, author: str | None = None, tag: str | None = None,
          include_hidden: bool = True) -> list[dict]:
    q = select(AnnotationRow).order_by(AnnotationRow.created_at)
    if symbol:
        q = q.where(AnnotationRow.symbol == normalize_symbol(symbol))
    if interval:
        iv = get_interval(interval).name
        q = q.where((AnnotationRow.interval == iv) | (AnnotationRow.interval.is_(None)))
    if author:
        q = q.where(AnnotationRow.author == author)
    if tag:
        q = q.where(AnnotationRow.tag == tag)
    if not include_hidden:
        q = q.where(AnnotationRow.visible.is_(True))
    with session_scope() as s:
        return [to_dict(r) for r in s.execute(q).scalars().all()]


def get(annotation_id: str) -> dict:
    with session_scope() as s:
        r = s.get(AnnotationRow, annotation_id)
        if r is None:
            raise KeyError(f"Annotation {annotation_id} not found")
        return to_dict(r)


def update(annotation_id: str, patch: AnnotationPatch) -> dict:
    with session_scope() as s:
        r = s.get(AnnotationRow, annotation_id)
        if r is None:
            raise KeyError(f"Annotation {annotation_id} not found")
        if patch.points is not None:
            # re-validate the full annotation with the new points
            AnnotationIn(type=r.type, points=patch.points, text=patch.text or r.text)
            r.points = _points(patch.points)
        if patch.text is not None:
            r.text = patch.text
        if patch.style is not None:
            r.style = {**(r.style or {}), **patch.style.model_dump(exclude_none=True)}
        if patch.interval is not None:
            r.interval = get_interval(patch.interval).name if patch.interval else None
        if patch.tag is not None:
            r.tag = patch.tag or None
        if patch.visible is not None:
            r.visible = patch.visible
        r.updated_at = utcnow()
        s.flush()
        d = to_dict(r)
    bus.publish("annotation.updated", d)
    return d


def delete_(ids: list[str] | None = None, symbol: str | None = None, tag: str | None = None,
            author: str | None = None) -> dict:
    if not any([ids, symbol, tag, author]):
        raise ValueError("Specify ids, or a symbol/tag/author filter")
    q = select(AnnotationRow)
    if ids:
        q = q.where(AnnotationRow.id.in_(ids))
    if symbol:
        q = q.where(AnnotationRow.symbol == normalize_symbol(symbol))
    if tag:
        q = q.where(AnnotationRow.tag == tag)
    if author:
        q = q.where(AnnotationRow.author == author)
    with session_scope() as s:
        rows = s.execute(q).scalars().all()
        gone = [(r.id, r.symbol) for r in rows]
        if gone:
            s.execute(delete(AnnotationRow).where(AnnotationRow.id.in_([g[0] for g in gone])))
    for aid, sym in gone:
        bus.publish("annotation.deleted", {"id": aid, "symbol": sym})
    return {"deleted": len(gone), "ids": [g[0] for g in gone]}
