"""Search and discovery over the active asset catalog."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

from . import ai
from .db import get_conn


SORTS = {
    "newest": "a.created_at DESC, a.id DESC",
    "oldest": "a.created_at ASC, a.id ASC",
    "name": "a.title ASC, a.id ASC",
    "largest": "a.size DESC, a.id DESC",
}


@dataclass(frozen=True)
class Query:
    sql: str
    params: dict
    order_sql: str
    conflict: bool = False


def normalize_tags(tags: Optional[Iterable[str]]) -> list[str]:
    """Normalize tag names the same way ingest does, preserving first order."""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = [tags]
    seen: set[str] = set()
    normalized: list[str] = []
    for tag in tags:
        name = (tag or "").strip().lower()
        if name and name not in seen:
            seen.add(name)
            normalized.append(name)
    return normalized


def _fts_query(q: str) -> str:
    """Turn user text into a safe FTS5 prefix query."""
    terms = [t for t in "".join(c if c.isalnum() or c.isspace() else " " for c in q).split() if t]
    return " ".join(f'"{t}"*' for t in terms)


def build_query(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    exclude_tags: Optional[Iterable[str]] = None,
    collection_id: Optional[int] = None,
    sort: str = "newest",
) -> Query:
    """Build the single keyword/filter query used by lists and exports.

    Named parameters keep the JOIN/FTS and structured-filter arguments from
    depending on their textual order. EXISTS clauses also avoid duplicate
    asset rows when an asset has multiple matching tags.
    """
    include = normalize_tags(tags)
    exclude = normalize_tags(exclude_tags)
    where = ["a.status = 'active'"]
    joins = ""
    params: dict[str, object] = {}

    if set(include) & set(exclude):
        return Query("\nFROM assets a\nWHERE 0", {}, SORTS.get(sort, SORTS["newest"]), True)

    fts = _fts_query(q)
    if fts:
        joins = " JOIN asset_fts f ON f.rowid = a.id"
        where.append("asset_fts MATCH :fts")
        params["fts"] = fts

    media_type = (media_type or "").strip()
    if media_type:
        where.append("a.media_type = :media_type")
        params["media_type"] = media_type

    if collection_id is not None:
        where.append(
            "EXISTS (SELECT 1 FROM collection_assets ca "
            "WHERE ca.asset_id = a.id AND ca.collection_id = :collection_id)"
        )
        params["collection_id"] = collection_id

    if include:
        placeholders = ", ".join(f":include_tag_{i}" for i in range(len(include)))
        where.append(
            "(SELECT COUNT(DISTINCT t.name) FROM tags t "
            "JOIN asset_tags at ON at.tag_id = t.id "
            f"WHERE at.asset_id = a.id AND t.name IN ({placeholders})) = :include_count"
        )
        params.update({f"include_tag_{i}": tag for i, tag in enumerate(include)})
        params["include_count"] = len(include)

    if exclude:
        placeholders = ", ".join(f":exclude_tag_{i}" for i in range(len(exclude)))
        where.append(
            "NOT EXISTS (SELECT 1 FROM tags t "
            "JOIN asset_tags at ON at.tag_id = t.id "
            f"WHERE at.asset_id = a.id AND t.name IN ({placeholders}))"
        )
        params.update({f"exclude_tag_{i}": tag for i, tag in enumerate(exclude)})

    sql = f"FROM assets a{joins} WHERE {' AND '.join(where)}"
    order_sql = SORTS.get(sort, SORTS["newest"])
    return Query(sql, params, order_sql)


def search(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    exclude_tags: Optional[Iterable[str]] = None,
    collection_id: Optional[int] = None,
    sort: str = "newest",
    mode: str = "keyword",
    limit: int = 60,
    offset: int = 0,
) -> dict:
    conn = get_conn()

    if mode == "semantic" and q.strip():
        return _semantic_search(q, media_type, limit, offset)

    query = build_query(
        q=q,
        media_type=media_type,
        tags=tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        sort=sort,
    )

    if query.conflict:
        return {
            "total": 0,
            "items": [],
            "facets": _facets(),
            "mode": "keyword",
            "limit": limit,
            "offset": offset,
        }

    total = conn.execute(f"SELECT COUNT(*) AS c {query.sql}", query.params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT a.* {query.sql} ORDER BY {query.order_sql} LIMIT :limit OFFSET :offset",
        {**query.params, "limit": limit, "offset": offset},
    ).fetchall()

    return {
        "total": total,
        "items": [_card(dict(r)) for r in rows],
        "facets": _facets(),
        "mode": "keyword",
        "limit": limit,
        "offset": offset,
    }


def _semantic_search(q: str, media_type: Optional[str], limit: int, offset: int) -> dict:
    """Preserve the existing internal semantic mode for non-quiz callers."""
    conn = get_conn()
    qvec = ai.embed(q)
    where = ["status = 'active'", "embedding IS NOT NULL"]
    params: list[object] = []
    if media_type:
        where.append("media_type = ?")
        params.append(media_type)
    rows = conn.execute(
        f"SELECT * FROM assets WHERE {' AND '.join(where)}", params
    ).fetchall()
    scored = []
    for row in rows:
        score = ai.cosine(qvec, ai.unpack_embedding(row["embedding"]))
        scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
    page = scored[offset:offset + limit]
    items = []
    for score, row in page:
        card = _card(dict(row))
        card["score"] = round(score, 4)
        items.append(card)
    return {
        "total": len(scored),
        "items": items,
        "facets": _facets(),
        "mode": "semantic",
        "limit": limit,
        "offset": offset,
    }


def iter_export_rows(
    *,
    q: str = "",
    media_type: Optional[str] = None,
    tags: Optional[Iterable[str]] = None,
    exclude_tags: Optional[Iterable[str]] = None,
    collection_id: Optional[int] = None,
    sort: str = "newest",
    batch_size: int = 200,
):
    """Yield every matching row, with a sorted JSON array of tag names."""
    query = build_query(
        q=q,
        media_type=media_type,
        tags=tags,
        exclude_tags=exclude_tags,
        collection_id=collection_id,
        sort=sort,
    )
    if query.conflict:
        return

    conn = get_conn()
    cursor = conn.execute(f"SELECT a.* {query.sql} ORDER BY {query.order_sql}", query.params)
    while True:
        rows = cursor.fetchmany(batch_size)
        if not rows:
            break
        for row in rows:
            asset = dict(row)
            tag_rows = conn.execute(
                "SELECT t.name FROM tags t JOIN asset_tags at ON at.tag_id = t.id "
                "WHERE at.asset_id = ? ORDER BY t.name",
                (asset["id"],),
            ).fetchall()
            yield {
                "uuid": asset["uuid"],
                "title": asset["title"] or "",
                "description": asset["description"] or "",
                "filename": asset["filename"] or "",
                "media_type": asset["media_type"] or "",
                "tags": [r["name"] for r in tag_rows],
                "created_at": asset["created_at"] or "",
            }


def more_like(asset_id: int, limit: int = 12) -> list[dict]:
    conn = get_conn()
    base = conn.execute("SELECT embedding FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not base or not base["embedding"]:
        return []
    bvec = ai.unpack_embedding(base["embedding"])
    rows = conn.execute(
        "SELECT * FROM assets WHERE id != ? AND status = 'active' AND embedding IS NOT NULL",
        (asset_id,),
    ).fetchall()
    scored = [(ai.cosine(bvec, ai.unpack_embedding(r["embedding"])), r) for r in rows]
    scored.sort(key=lambda x: x[0], reverse=True)
    out = []
    for score, r in scored[:limit]:
        card = _card(dict(r))
        card["score"] = round(score, 4)
        out.append(card)
    return out


def _facets() -> dict:
    """Global active-catalog facets, not counts for the current match set."""
    conn = get_conn()
    media = conn.execute(
        "SELECT media_type, COUNT(*) c FROM assets WHERE status = 'active' "
        "GROUP BY media_type ORDER BY c DESC"
    ).fetchall()
    tags = conn.execute(
        """SELECT t.name, COUNT(*) c FROM tags t
           JOIN asset_tags at ON at.tag_id = t.id
           JOIN assets a ON a.id = at.asset_id AND a.status = 'active'
           GROUP BY t.name ORDER BY c DESC, t.name LIMIT 25"""
    ).fetchall()
    return {
        "media_types": [{"value": r["media_type"], "count": r["c"]} for r in media],
        "tags": [{"value": r["name"], "count": r["c"]} for r in tags],
    }


def _card(a: dict) -> dict:
    """Compact asset representation for grid/list views."""
    return {
        "id": a["id"],
        "uuid": a["uuid"],
        "title": a["title"],
        "filename": a["filename"],
        "media_type": a["media_type"],
        "mime_type": a["mime_type"],
        "ext": a["ext"],
        "size": a["size"],
        "width": a["width"],
        "height": a["height"],
        "alt_text": a["alt_text"],
        "status": a["status"],
        "created_at": a["created_at"],
    }
