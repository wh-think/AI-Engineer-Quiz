"""Deterministic data for manually exercising the quiz acceptance flow.

The normal demo seed remains focused on the product. This fixture adds the
target Harbor Campaign collection, its positive and negative examples, and
enough matching rows to exercise every page boundary. Re-running it updates
the same named collection and assets rather than adding duplicates.
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

from PIL import Image

from . import ingest
from .audit import now_iso
from .auth import hash_password
from .db import get_conn, init_db, transaction


COLLECTION_UUID = "00000000-0000-4000-8000-00000000a11e"
COLLECTION_NAME = "Harbor Campaign"
BASE_TIME = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _image(seed: str, color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (960, 640), color).save(
        buf, format="JPEG", quality=86, comment=f"quiz seed: {seed}".encode("utf-8")
    )
    return buf.getvalue()


def _ensure_admin(conn) -> dict:
    row = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    if not row:
        conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
            "VALUES ('admin', 'admin@example.org', 'Administrator', ?, 'admin', 1, ?)",
            (hash_password("admin"), now_iso()),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    return dict(row)


def _ensure_collection(conn, admin: dict) -> int:
    row = conn.execute("SELECT id FROM collections WHERE uuid = ?", (COLLECTION_UUID,)).fetchone()
    if row:
        conn.execute(
            "UPDATE collections SET name = ?, description = ? WHERE id = ?",
            (COLLECTION_NAME, "Quiz acceptance fixture: harbor campaign assets", row["id"]),
        )
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO collections (uuid, name, description, kind, created_by, created_at) "
        "VALUES (?, ?, ?, 'folder', ?, ?)",
        (COLLECTION_UUID, COLLECTION_NAME,
         "Quiz acceptance fixture: harbor campaign assets", admin["id"], now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def _asset(conn, admin: dict, collection_id: int, key: str, *, title: str,
           description: str = "", media_type: str = "image", tags: (),
           created_at: datetime, status: str = "active") -> dict:
    filename = f"quiz-{key}.{'txt' if media_type == 'document' else 'jpg'}"
    existing = conn.execute(
        "SELECT * FROM assets WHERE original_filename = ? ORDER BY id LIMIT 1", (filename,)
    ).fetchone()
    if existing:
        asset = dict(existing)
        conn.execute(
            "UPDATE assets SET title = ?, description = ?, media_type = ?, status = ?, "
            "created_at = ?, updated_at = ? WHERE id = ?",
            (title, description, media_type, status, created_at.isoformat(), now_iso(), asset["id"]),
        )
    else:
        palette = [
            (34, 102, 160), (205, 118, 54), (65, 145, 98), (116, 84, 166),
            (176, 70, 94), (62, 138, 150), (149, 132, 62), (85, 97, 160),
        ]
        asset = ingest.ingest_stream(
            io.BytesIO(_image(key, palette[len(key) % len(palette)])),
            filename,
            user=admin,
            run_ai=False,
            title=title,
        )
        conn.execute(
            "UPDATE assets SET description = ?, media_type = ?, status = ?, created_at = ?, "
            "updated_at = ? WHERE id = ?",
            (description, media_type, status, created_at.isoformat(), now_iso(), asset["id"]),
        )
    conn.execute("DELETE FROM asset_tags WHERE asset_id = ?", (asset["id"],))
    for name in tags:
        name = name.strip().lower()
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()["id"]
        conn.execute(
            "INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?, ?, 'manual')",
            (asset["id"], tag_id),
        )
    conn.execute(
        "INSERT INTO collection_assets (collection_id, asset_id, added_at) VALUES (?, ?, ?) "
        "ON CONFLICT(collection_id, asset_id) DO UPDATE SET added_at = excluded.added_at",
        (collection_id, asset["id"], created_at.isoformat()),
    )
    conn.commit()
    ingest.reindex_fts(asset["id"])
    ingest.reindex_embedding(asset["id"])
    return dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset["id"],)).fetchone())


def run_quiz_seed() -> None:
    """Populate an isolated data directory with repeatable acceptance data."""
    init_db()
    conn = get_conn()
    admin = _ensure_admin(conn)
    collection_id = _ensure_collection(conn, admin)

    # All of these match q=harbor, the target collection, both required tags,
    # and none carries the excluded internal tag. They share a timestamp so
    # the id tie-breaker is exercised at both page boundaries.
    for index in range(14):
        description = (
            'Harbor survey asset, with "quotes"\nand a second line'
            if index == 0 else f"Harbor survey campaign asset {index:02d}"
        )
        _asset(
            conn, admin, collection_id, f"target-{index:02d}",
            title=f"Harbor approved campaign {index:02d}",
            description=description, media_type="image",
            tags=("approved", "campaign", "extra"), created_at=BASE_TIME,
        )

    _asset(
        conn, admin, collection_id, "internal",
        title="Harbor approved campaign internal",
        description="Must be excluded because it has the internal tag.",
        tags=("approved", "campaign", "internal"), created_at=BASE_TIME + timedelta(hours=1),
    )
    _asset(
        conn, admin, collection_id, "missing-campaign",
        title="Harbor approved without campaign",
        description="Must be excluded because one required tag is missing.",
        tags=("approved",), created_at=BASE_TIME + timedelta(hours=2),
    )
    _asset(
        conn, admin, collection_id, "deleted",
        title="Harbor deleted campaign",
        description="Must be excluded because it is not active.",
        tags=("approved", "campaign"), created_at=BASE_TIME + timedelta(hours=3),
        status="deleted",
    )

    # A Chinese document verifies media filtering and CSV encoding. Its title
    # intentionally contains the keyword so it is part of the positive set.
    _asset(
        conn, admin, collection_id, "chinese",
        title="Harbor 中文 campaign document",
        description='中文，含逗号与引号 "测试"；导出必须保持完整。',
        media_type="document", tags=("approved", "campaign"),
        created_at=BASE_TIME + timedelta(days=1),
    )

    conn.execute(
        "INSERT INTO tags (name) VALUES ('internal') ON CONFLICT(name) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO tags (name) VALUES ('campaign') ON CONFLICT(name) DO NOTHING"
    )
    conn.execute(
        "INSERT INTO tags (name) VALUES ('approved') ON CONFLICT(name) DO NOTHING"
    )
    conn.commit()
    print(json.dumps({
        "collection": COLLECTION_NAME,
        "collection_id": collection_id,
        "expected_matches_without_media_filter": 15,
        "expected_image_matches": 14,
        "page_size": 12,
        "credentials": "admin / admin",
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    run_quiz_seed()
