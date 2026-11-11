"""The ingest pipeline.

One function, ``ingest_stream``, takes raw bytes and a filename and runs the
full intake: content-addressable store + dedup, technical metadata
extraction (EXIF/IPTC/XMP), thumbnail generation, descriptive-field
seeding, optional AI enrichment, embedding for semantic search, and
full-text indexing. Everything an asset needs to be findable is computed
here, once, at upload time.
"""
from __future__ import annotations

import json
import uuid as uuidlib
from typing import BinaryIO, Optional

from . import ai, metadata, ocr, storage, thumbnails
from .audit import now_iso
from .config import config
from .db import get_conn, transaction
from .enums import AssetStatus


def _set_tags(conn, asset_id: int, tags: list[tuple[str, float]], source: str) -> None:
    for name, confidence in tags:
        name = name.strip().lower()
        if not name:
            continue
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source, confidence) VALUES (?, ?, ?, ?)",
            (asset_id, tag_id, source, confidence),
        )


def reindex_fts(asset_id: int) -> None:
    """Rebuild the FTS row for an asset from its current state."""
    conn = get_conn()
    a = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not a:
        return
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN asset_tags at ON at.tag_id = t.id WHERE at.asset_id = ?",
        (asset_id,),
    )]
    meta = conn.execute("SELECT raw_json FROM asset_metadata WHERE asset_id = ?", (asset_id,)).fetchone()
    meta_text = ""
    if meta and meta["raw_json"]:
        try:
            meta_text = metadata.metadata_text(json.loads(meta["raw_json"]))
        except Exception:
            meta_text = ""
    custom = [r["value"] for r in conn.execute(
        "SELECT value FROM asset_custom_values WHERE asset_id = ? AND value IS NOT NULL", (asset_id,)
    )]
    conn.execute("DELETE FROM asset_fts WHERE rowid = ?", (asset_id,))
    conn.execute(
        """INSERT INTO asset_fts (rowid, title, description, alt_text, filename, tags, metadata_text, custom_text)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            asset_id, a["title"] or "", a["description"] or "", a["alt_text"] or "",
            a["filename"] or "", " ".join(tags), meta_text, " ".join(custom),
        ),
    )
    conn.commit()


def reindex_embedding(asset_id: int) -> None:
    conn = get_conn()
    a = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if not a:
        return
    tags = [r["name"] for r in conn.execute(
        "SELECT t.name FROM tags t JOIN asset_tags at ON at.tag_id = t.id WHERE at.asset_id = ?",
        (asset_id,),
    )]
    text = " ".join(filter(None, [a["title"], a["description"], a["alt_text"], a["filename"], " ".join(tags)]))
    vec = None
    if a["media_type"] == "image":
        # Prefer a visual embedding when the local model is installed: text
        # queries then match image content directly in the shared CLIP space.
        vec = ai.embed_image(storage.blob_path(a["storage_key"]))
    if vec is None:
        vec = ai.embed(text or a["filename"])
    conn.execute("UPDATE assets SET embedding = ? WHERE id = ?", (ai.pack_embedding(vec), asset_id))
    conn.commit()


def ingest_stream(
    src: BinaryIO,
    filename: str,
    *,
    user: Optional[dict] = None,
    run_ai: bool = True,
    title: Optional[str] = None,
) -> dict:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    sha, key, size = storage.store_stream(src, ext)

    conn = get_conn()
    existing = conn.execute(
        "SELECT * FROM assets WHERE sha256 = ? AND status != 'deleted'", (sha,)
    ).fetchone()
    if existing:
        # Blob-level dedup surfaced an identical asset already in the catalog.
        out = dict(existing)
        out["duplicate"] = True
        return out

    blob = storage.blob_path(key)
    meta = metadata.extract(blob, filename)
    if meta["media_type"] == "image" and config.ocr_enabled and ocr.available():
        text = ocr.extract_text(blob)
        if text:
            meta["ocr_text"] = text
    thumb_key = thumbnails.ensure_thumbnail(sha, key, meta["media_type"], ext)

    desc = meta.get("descriptive", {})
    now = now_iso()
    asset_uuid = str(uuidlib.uuid4())
    with transaction() as conn:
        cur = conn.execute(
            """INSERT INTO assets
               (uuid, sha256, filename, original_filename, mime_type, media_type, ext,
                size, width, height, duration, title, description, alt_text, status,
                storage_key, thumb_key, dc_creator, dc_subject, dc_publisher, dc_date,
                credit, rights_statement,
                uploaded_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                asset_uuid, sha, filename, filename, meta["mime"], meta["media_type"], ext,
                size, meta["width"], meta["height"], meta["duration"],
                title or desc.get("title") or filename,
                desc.get("description"), None, AssetStatus.ACTIVE.value,
                key, thumb_key, desc.get("creator"),
                ", ".join(desc["keywords"]) if isinstance(desc.get("keywords"), list) else desc.get("keywords"),
                desc.get("publisher"), desc.get("date"),
                desc.get("credit"), desc.get("rights"),
                (user or {}).get("id"), now, now,
            ),
        )
        asset_id = cur.lastrowid
        conn.execute(
            "INSERT INTO asset_metadata (asset_id, exif_json, iptc_json, xmp_json, raw_json) VALUES (?,?,?,?,?)",
            (
                asset_id,
                json.dumps(meta.get("exif", {})),
                json.dumps(meta.get("iptc", {})),
                json.dumps(meta.get("xmp", {})),
                json.dumps(meta),
            ),
        )
        if desc.get("keywords"):
            kws = desc["keywords"]
            if isinstance(kws, str):
                kws = [kws]
            _set_tags(conn, asset_id, [(k, 1.0) for k in kws], "embedded")

    # AI enrichment (image only) after the row exists.
    if run_ai and meta["media_type"] == "image":
        enr = ai.enrich_image(blob, meta["mime"])
        with transaction() as conn:
            if enr.get("tags"):
                _set_tags(conn, asset_id, enr["tags"], "ai")
            updates, vals = [], []
            a = conn.execute("SELECT alt_text, description FROM assets WHERE id = ?", (asset_id,)).fetchone()
            if enr.get("alt_text") and not a["alt_text"]:
                updates.append("alt_text = ?"); vals.append(enr["alt_text"])
            if enr.get("caption") and not a["description"]:
                updates.append("description = ?"); vals.append(enr["caption"])
            if updates:
                vals.append(asset_id)
                conn.execute(f"UPDATE assets SET {', '.join(updates)} WHERE id = ?", vals)

    reindex_fts(asset_id)
    reindex_embedding(asset_id)

    return dict(get_conn().execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone())
