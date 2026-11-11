"""Core pipeline tests: storage dedup, ingest, metadata, search, auth, fixity.

These run against a throwaway data directory so they never touch a real
repository. Run with:  python -m pytest -q
"""
import io
import json
import os
import tempfile
from pathlib import Path

import pytest

os.environ["KEEPSTACK_DATA_DIR"] = tempfile.mkdtemp(prefix="keepstack-test-")
os.environ["KEEPSTACK_AI_ENABLED"] = "false"

from PIL import Image  # noqa: E402

from keepstack import ai, ingest, ocr, search, standards, storage, thumbnails, vision  # noqa: E402
from keepstack.auth import (hash_password, make_token, role_at_least,  # noqa: E402
                         verify_password, verify_token)
from keepstack.db import get_conn, init_db  # noqa: E402


def _img_bytes(color=(90, 130, 240), size=(400, 300)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG")
    return buf.getvalue()


def _xmp_img_bytes():
    xmp = """<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
  <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
           xmlns:dc="http://purl.org/dc/elements/1.1/"
           xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/">
    <rdf:Description>
      <dc:title><rdf:Alt><rdf:li xml:lang="x-default">XMP Harbor Archive</rdf:li></rdf:Alt></dc:title>
      <dc:description><rdf:Alt><rdf:li xml:lang="x-default">NeedleMuseum catalog caption</rdf:li></rdf:Alt></dc:description>
      <dc:creator><rdf:Seq><rdf:li>Ada Archivist</rdf:li></rdf:Seq></dc:creator>
      <dc:subject><rdf:Bag><rdf:li>estuaries</rdf:li><rdf:li>tidal maps</rdf:li></rdf:Bag></dc:subject>
      <dc:publisher>NeedleMuseum Press</dc:publisher>
      <dc:date>2026-07-02</dc:date>
      <dc:rights>Creative Commons Test License</dc:rights>
      <photoshop:Credit>Archive Credit Line</photoshop:Credit>
    </rdf:Description>
  </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    buf = io.BytesIO()
    Image.new("RGB", (180, 120), (20, 90, 130)).save(buf, format="JPEG", xmp=xmp.encode("utf-8"))
    return buf.getvalue()


def _split_img_bytes():
    buf = io.BytesIO()
    img = Image.new("RGB", (100, 60), (20, 40, 80))
    for x in range(100):
        for y in range(60):
            img.putpixel((x, y), (220, 30, 30) if x < 50 else (30, 180, 80))
    img.save(buf, format="JPEG")
    return buf.getvalue()


def setup_module(_):
    init_db()


def test_password_hashing():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)


def test_token_roundtrip():
    tok = make_token(42, "admin")
    payload = verify_token(tok)
    assert payload["sub"] == 42 and payload["role"] == "admin"
    assert verify_token(tok + "x") is None


def test_role_hierarchy():
    assert role_at_least("admin", "viewer")
    assert role_at_least("editor", "contributor")
    assert not role_at_least("viewer", "editor")


def test_ingest_creates_asset_with_dimensions_and_thumb():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes()), "skyline.jpg")
    assert a["media_type"] == "image"
    assert a["width"] == 400 and a["height"] == 300
    assert a["sha256"] and storage.blob_path(a["storage_key"]).exists()
    assert storage.thumb_path(a["thumb_key"]).exists()


def test_content_addressable_dedup():
    data = _img_bytes(color=(10, 200, 120), size=(256, 256))
    first = ingest.ingest_stream(io.BytesIO(data), "dup_a.jpg")
    second = ingest.ingest_stream(io.BytesIO(data), "dup_b.jpg")
    assert second.get("duplicate") is True
    assert first["sha256"] == second["sha256"]


def test_keyword_search_finds_by_title():
    ingest.ingest_stream(io.BytesIO(_img_bytes(color=(200, 50, 90), size=(420, 260))),
                         "unique_harbor_photo.jpg", title="Unique Harbor Photo")
    res = search.search(q="harbor")
    assert res["total"] >= 1
    assert any("harbor" in (i["title"] or "").lower() for i in res["items"])


def test_semantic_search_returns_ranked_results():
    ingest.ingest_stream(io.BytesIO(_img_bytes(color=(40, 150, 90), size=(330, 500))),
                         "mountain_trail.jpg", title="Mountain trail in the alpine forest")
    res = search.search(q="alpine forest trail", mode="semantic")
    assert res["mode"] == "semantic"
    assert res["total"] >= 1
    # scores should be present and sorted descending
    scores = [i.get("score", 0) for i in res["items"]]
    assert scores == sorted(scores, reverse=True)


def test_embedding_similarity_is_symmetric_and_bounded():
    v = ai.embed("a civic poster about public transit")
    assert 0.99 <= ai.cosine(v, v) <= 1.01
    other = ai.embed("a watershed environmental survey diagram")
    assert -1.01 <= ai.cosine(v, other) <= 1.01


def test_fixity_detects_intact_and_corruption():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(size=(120, 120))), "fixity.jpg")
    assert storage.verify_fixity(a["storage_key"], a["sha256"]) is True
    # corrupt the blob and confirm the check fails
    storage.blob_path(a["storage_key"]).write_bytes(b"tampered")
    assert storage.verify_fixity(a["storage_key"], a["sha256"]) is False


def test_gc_removes_blob_after_asset_delete():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(color=(5, 20, 250), size=(111, 111))), "gc-delete.jpg")
    path = storage.blob_path(a["storage_key"])
    assert path.exists()

    conn = get_conn()
    conn.execute("UPDATE assets SET status = 'deleted' WHERE id = ?", (a["id"],))
    conn.commit()

    result = storage.collect_orphan_blobs()

    assert result["removed"] >= 1
    assert not path.exists()


def test_gc_keeps_active_asset_versions():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(color=(250, 20, 5), size=(112, 112))), "gc-version.jpg")
    old_key = a["storage_key"]
    _new_sha, new_key, new_size = storage.store_bytes(b"replacement version", "txt")

    conn = get_conn()
    conn.execute(
        "INSERT INTO asset_versions (asset_id, version_no, sha256, storage_key, size, note, created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (a["id"], 1, a["sha256"], old_key, a["size"], "previous current", "2026-07-01T00:00:00Z"),
    )
    conn.execute(
        "UPDATE assets SET storage_key = ?, size = ? WHERE id = ?",
        (new_key, new_size, a["id"]),
    )
    conn.commit()

    result = storage.collect_orphan_blobs()

    assert old_key not in {b["key"] for b in result["blobs"]}
    assert storage.blob_path(old_key).exists()
    assert storage.blob_path(new_key).exists()


def test_dublin_core_mapping():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(color=(120, 90, 210), size=(360, 360))),
                             "dc_test.jpg", title="DC Test")
    dc = standards.dublin_core(a)
    assert dc["title"] == "DC Test"
    assert dc["type"] == "image"
    assert dc["identifier"] == a["uuid"]


def test_xmp_fields_seed_catalog_and_search():
    a = ingest.ingest_stream(io.BytesIO(_xmp_img_bytes()), "xmp_harbor.jpg")

    assert a["title"] == "XMP Harbor Archive"
    assert a["description"] == "NeedleMuseum catalog caption"
    assert a["dc_creator"] == "Ada Archivist"
    assert a["dc_subject"] == "estuaries, tidal maps"
    assert a["dc_publisher"] == "NeedleMuseum Press"
    assert a["dc_date"] == "2026-07-02"
    assert a["rights_statement"] == "Creative Commons Test License"
    assert a["credit"] == "Archive Credit Line"

    dc = standards.dublin_core(a)
    assert dc["publisher"] == "NeedleMuseum Press"

    meta = get_conn().execute("SELECT xmp_json FROM asset_metadata WHERE asset_id = ?", (a["id"],)).fetchone()
    xmp = json.loads(meta["xmp_json"])
    assert xmp["fields"]["dc:publisher"] == "NeedleMuseum Press"

    res = search.search(q="NeedleMuseum")
    assert any(i["uuid"] == a["uuid"] for i in res["items"])


def test_iiif_info_advertises_level_2():
    info = standards.iiif_info({"uuid": "asset-1", "width": 100, "height": 60})
    assert info["profile"] == "level2"
    assert info["tiles"][0]["width"] == 512


def test_iiif_rendition_crops_and_rotates():
    sha, key, _ = storage.store_bytes(_split_img_bytes(), "jpg")

    data = thumbnails.iiif_rendition(sha, key, "image", "10,20,30,20", "full", "90")
    img = Image.open(io.BytesIO(data))

    assert img.size == (20, 30)


def test_iiif_rendition_supports_percent_region_and_best_fit_size():
    sha, key, _ = storage.store_bytes(_split_img_bytes(), "jpg")

    data = thumbnails.iiif_rendition(sha, key, "image", "pct:0,0,50,100", "!25,25", "0")
    img = Image.open(io.BytesIO(data))

    assert img.size == (21, 25)


def test_oai_identify_is_valid_xml():
    body, status = standards.oai_response({"verb": "Identify"})
    assert status == 200
    assert "<repositoryName>" in body and "OAI-PMH" in body


def test_login_throttle_locks_success_clears_and_expires(monkeypatch):
    from keepstack import auth
    from keepstack.config import config
    monkeypatch.setattr(config, "login_max_attempts", 3)
    monkeypatch.setattr(config, "login_max_attempts_per_ip", 100)  # isolate the pair bucket
    monkeypatch.setattr(config, "login_lockout_seconds", 60)
    auth.reset_login_throttle()
    user, ip = "admin", "203.0.113.9"

    # First two failures do not lock; the third trips it.
    assert auth.note_login_failure(user, ip, now=1000) is False
    assert auth.note_login_failure(user, ip, now=1000) is False
    assert auth.note_login_failure(user, ip, now=1000) is True
    assert auth.login_locked(user, ip, now=1000) > 0
    assert auth.login_locked(user, "198.51.100.4", now=1000) == 0  # a different IP is open

    # A successful login clears an ACTIVE lockout (not just an empty bucket).
    auth.note_login_success(user, ip)
    assert auth.login_locked(user, ip, now=1000) == 0

    # A fresh lockout later expires once the window passes.
    for _ in range(3):
        auth.note_login_failure(user, ip, now=2000)
    assert auth.login_locked(user, ip, now=2000) > 0
    assert auth.login_locked(user, ip, now=2000 + 61) == 0


def test_login_throttle_per_ip_catches_username_spray(monkeypatch):
    from keepstack import auth
    from keepstack.config import config
    monkeypatch.setattr(config, "login_max_attempts", 100)  # pair bucket effectively off
    monkeypatch.setattr(config, "login_max_attempts_per_ip", 4)
    monkeypatch.setattr(config, "login_lockout_seconds", 60)
    auth.reset_login_throttle()
    ip = "203.0.113.50"
    # Every username is distinct, so no pair bucket ever trips, but the per-IP
    # bucket does after four sprayed attempts, locking the whole source IP.
    for i in range(4):
        assert auth.login_locked(f"user{i}", ip, now=500) == 0
        auth.note_login_failure(f"user{i}", ip, now=500)
    assert auth.login_locked("someone-else", ip, now=500) > 0


def test_client_ip_ignores_spoofed_xff(monkeypatch):
    from keepstack.app import client_ip
    from keepstack.config import config

    class _Req:
        def __init__(self, peer, xff=None):
            self.client = type("C", (), {"host": peer})()
            self.headers = {"X-Forwarded-For": xff} if xff else {}

    # No trusted proxies: X-Forwarded-For is ignored, the real peer wins.
    monkeypatch.setattr(config, "trusted_proxies", [])
    assert client_ip(_Req("198.51.100.7", xff="1.2.3.4")) == "198.51.100.7"

    # Behind a trusted proxy: the right-most non-proxy hop is the client.
    monkeypatch.setattr(config, "trusted_proxies", ["10.0.0.0/8"])
    assert client_ip(_Req("10.0.0.9", xff="9.9.9.9, 10.0.0.5")) == "9.9.9.9"
    # An untrusted direct peer still cannot spoof its way past.
    assert client_ip(_Req("203.0.113.1", xff="9.9.9.9")) == "203.0.113.1"


def test_iiif_manifest_shape():
    a = ingest.ingest_stream(io.BytesIO(_img_bytes(size=(300, 200))),
                             "manifest_test.jpg", title="Manifest Test")
    m = standards.iiif_manifest(a)
    assert m["type"] == "Manifest"
    assert m["@context"].endswith("presentation/3/context.json")
    assert m["label"]["none"] == ["Manifest Test"]

    canvas = m["items"][0]
    assert canvas["type"] == "Canvas"
    assert canvas["width"] == 300 and canvas["height"] == 200

    anno = canvas["items"][0]["items"][0]
    assert anno["motivation"] == "painting"
    assert anno["body"]["id"].endswith("/full/max/0/default.jpg")
    service = anno["body"]["service"][0]
    assert service["type"] == "ImageService3"
    assert service["id"].endswith(f"/iiif/3/{a['uuid']}")


@pytest.mark.skipif(not vision.ready(), reason="local vision model not installed")
def test_local_clip_cross_modal_similarity():
    from PIL import ImageDraw, ImageFont
    data_dir = Path(os.environ["KEEPSTACK_DATA_DIR"])

    page = Image.new("RGB", (448, 448), (250, 250, 250))
    draw = ImageDraw.Draw(page)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except Exception:
        font = ImageFont.load_default(size=28)
    for i in range(12):
        draw.text((20, 20 + i * 36), "Quarterly report on estuary water quality.", font=font, fill=(20, 20, 20))
    page_path = data_dir / "clip_textpage.png"
    page.save(page_path)
    color_path = data_dir / "clip_colorfield.png"
    Image.new("RGB", (448, 448), (215, 30, 30)).save(color_path)

    tvec = vision.embed_text("a page of printed text")
    pvec = vision.embed_image(page_path)
    cvec = vision.embed_image(color_path)

    assert tvec and pvec and cvec
    assert ai.cosine(tvec, pvec) > ai.cosine(tvec, cvec)


@pytest.mark.skipif(not ocr.available(), reason="tesseract binary not installed")
def test_ocr_makes_image_text_searchable():
    from PIL import ImageDraw, ImageFont
    img = Image.new("RGB", (900, 240), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 96)
    except Exception:
        font = ImageFont.load_default(size=96)
    draw.text((40, 60), "QUILLFEATHER ARCHIVE", font=font, fill=(0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")

    a = ingest.ingest_stream(io.BytesIO(buf.getvalue()), "ocr_sample.png")

    res = search.search(q="quillfeather")
    assert any(i["uuid"] == a["uuid"] for i in res["items"])


def test_version_endpoint_reports_name_and_version():
    from keepstack.app import version
    payload = version()
    assert payload["name"] == "keepstack"
    assert payload["version"]


# ---------------------------------------------------------------------------
# Quiz acceptance: combined filtering, stable pagination, and CSV export
# ---------------------------------------------------------------------------
def _quiz_asset(filename, *, title="", description="", media_type="image", created_at=None,
                tags=(), collection=None, status="active"):
    buf = io.BytesIO()
    Image.new("RGB", (100, 70), (70, 100, 150)).save(
        buf, format="JPEG", comment=f"quiz fixture: {filename}".encode("utf-8")
    )
    data = buf.getvalue()
    asset = ingest.ingest_stream(io.BytesIO(data), filename, run_ai=False, title=title)
    conn = get_conn()
    conn.execute(
        "UPDATE assets SET description = ?, media_type = ?, status = ?, created_at = ? WHERE id = ?",
        (description, media_type, status, created_at or asset["created_at"], asset["id"]),
    )
    for name in tags:
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name.lower(),))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name.lower(),)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id, source) VALUES (?,?, 'manual')",
                     (asset["id"], tag_id))
    if collection is not None:
        conn.execute("INSERT OR IGNORE INTO collection_assets (collection_id, asset_id, added_at) "
                     "VALUES (?,?, ?)", (collection, asset["id"], created_at or asset["created_at"]))
    conn.commit()
    ingest.reindex_fts(asset["id"])
    return dict(conn.execute("SELECT * FROM assets WHERE id = ?", (asset["id"],)).fetchone())


@pytest.fixture()
def quiz_catalog():
    conn = get_conn()
    conn.execute("DELETE FROM collection_assets")
    conn.execute("DELETE FROM asset_tags")
    conn.execute("DELETE FROM collections")
    conn.execute("DELETE FROM asset_fts")
    conn.execute("DELETE FROM assets")
    conn.execute("DELETE FROM tags")
    conn.commit()
    admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    if not admin:
        conn.execute(
            "INSERT INTO users (username, email, full_name, password_hash, role, is_active, created_at) "
            "VALUES ('admin', 'admin@example.org', 'Administrator', ?, 'admin', 1, ?)",
            (hash_password("admin"), "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        admin = conn.execute("SELECT * FROM users WHERE username = 'admin'").fetchone()
    conn.execute("INSERT INTO collections (uuid, name, created_at) VALUES ('quiz-target', 'Harbor Campaign', '2026-01-01T00:00:00Z')")
    conn.execute("INSERT INTO collections (uuid, name, created_at) VALUES ('quiz-other', 'Other', '2026-01-01T00:00:00Z')")
    target = conn.execute("SELECT id FROM collections WHERE uuid='quiz-target'").fetchone()["id"]
    other = conn.execute("SELECT id FROM collections WHERE uuid='quiz-other'").fetchone()["id"]
    conn.commit()

    base = "2026-08-01T12:00:00+00:00"
    assets = []
    for i in range(15):
        assets.append(_quiz_asset(
            f"quiz-target-{i:02d}.jpg",
            title=f"Harbor approved campaign {i:02d}",
            description='target, with "quotes"\nand a newline', created_at=base,
            tags=["approved", "campaign", "extra"], collection=target,
        ))
    assets.append(_quiz_asset("quiz-internal.jpg", title="Harbor internal",
                              created_at="2026-08-02T12:00:00+00:00",
                              tags=["approved", "campaign", "internal"], collection=target))
    assets.append(_quiz_asset("quiz-missing.jpg", title="Harbor missing campaign",
                              created_at="2026-08-03T12:00:00+00:00",
                              tags=["approved"], collection=target))
    assets.append(_quiz_asset("quiz-other-collection.jpg", title="Harbor elsewhere",
                              created_at="2026-08-04T12:00:00+00:00",
                              tags=["approved", "campaign"], collection=other))
    assets.append(_quiz_asset("quiz-deleted.jpg", title="Harbor deleted",
                              created_at="2026-08-05T12:00:00+00:00",
                              tags=["approved", "campaign"], collection=target, status="deleted"))
    assets.append(_quiz_asset("quiz-document.txt", title="Harbor Chinese 中文",
                              description="中文，含逗号与引号 \"测试\"", media_type="document",
                              created_at="2026-08-06T12:00:00+00:00",
                              tags=["approved", "campaign"], collection=target))
    assets.append(_quiz_asset("quiz-other-media.txt", title="Harbor Other Media",
                              media_type="document", created_at="2026-08-07T12:00:00+00:00",
                              tags=["approved", "campaign"], collection=target))
    return {"target": target, "other": other, "assets": assets, "admin": dict(admin)}


def _quiz_ids(result):
    return [item["id"] for item in result["items"]]


def test_quiz_combined_filter_and_tag_semantics(quiz_catalog):
    result = search.search(q="harbor", collection_id=quiz_catalog["target"],
                           tags=["approved", "campaign"], exclude_tags=["internal"],
                           limit=100)
    assert result["total"] == 17
    assert len(result["items"]) == 17
    assert all(item["status"] == "active" for item in result["items"])

    images = search.search(q="harbor", collection_id=quiz_catalog["target"],
                           tags=["approved", "campaign"], exclude_tags=["internal"],
                           media_type="image", limit=100)
    assert images["total"] == 15
    assert all(item["media_type"] == "image" for item in images["items"])

    duplicate_tags = search.search(q="harbor", collection_id=quiz_catalog["target"],
                                   tags=["approved", "approved", " campaign "],
                                   exclude_tags=["internal", "internal"], limit=100)
    assert duplicate_tags["total"] == result["total"]

    conflict = search.search(q="harbor", tags=["approved"], exclude_tags=["APPROVED"])
    assert conflict["total"] == 0 and conflict["items"] == []


def test_quiz_keyword_sort_is_stable_across_pages(quiz_catalog):
    page1 = search.search(q="harbor", collection_id=quiz_catalog["target"],
                          tags=["approved", "campaign"], exclude_tags=["internal"],
                          sort="oldest", limit=6, offset=0)
    page2 = search.search(q="harbor", collection_id=quiz_catalog["target"],
                          tags=["approved", "campaign"], exclude_tags=["internal"],
                          sort="oldest", limit=6, offset=6)
    page3 = search.search(q="harbor", collection_id=quiz_catalog["target"],
                          tags=["approved", "campaign"], exclude_tags=["internal"],
                          sort="oldest", limit=6, offset=12)
    ids = _quiz_ids(page1) + _quiz_ids(page2) + _quiz_ids(page3)
    assert len(ids) == 17 and len(set(ids)) == 17
    assert page1["total"] == page2["total"] == 17
    expected = search.search(q="harbor", collection_id=quiz_catalog["target"],
                             tags=["approved", "campaign"], exclude_tags=["internal"],
                             sort="oldest", limit=100)["items"]
    assert ids == [item["id"] for item in expected]

    newest = search.search(q="harbor", collection_id=quiz_catalog["target"],
                           tags=["approved", "campaign"], exclude_tags=["internal"],
                           sort="newest", limit=100)["items"]
    assert newest[0]["title"] == "Harbor Other Media"
    assert newest[1]["title"].endswith("中文")
    assert newest[-1]["created_at"] == "2026-08-01T12:00:00+00:00"


def test_quiz_export_matches_all_filtered_rows(quiz_catalog):
    import csv
    from fastapi.testclient import TestClient
    from keepstack.app import app

    client = TestClient(app)
    token = make_token(quiz_catalog["admin"]["id"], quiz_catalog["admin"]["role"])
    params = [
        ("q", "harbor"), ("collection_id", str(quiz_catalog["target"])),
        ("tag", "approved"), ("tag", "campaign"),
        ("exclude_tag", "internal"), ("sort", "oldest"),
        ("limit", "1"), ("offset", "100"),
    ]
    response = client.get("/api/assets/export.csv", params=params,
                          headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"\xef\xbb\xbf")
    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    rows = list(reader)
    assert list(reader.fieldnames) == ["uuid", "title", "description", "filename",
                                       "media_type", "tags", "created_at"]
    assert len(rows) == 17
    expected = search.search(q="harbor", collection_id=quiz_catalog["target"],
                             tags=["approved", "campaign"], exclude_tags=["internal"],
                             sort="oldest", limit=100)["items"]
    assert [row["uuid"] for row in rows] == [item["uuid"] for item in expected]
    newline = next(row for row in rows if "newline" in row["description"])
    assert 'target, with "quotes"\nand a newline' == newline["description"]
    chinese = next(row for row in rows if "中文" in row["title"])
    assert "中文" in chinese["title"]
    assert "中文，含逗号" in chinese["description"]
    assert '"测试"' in chinese["description"]
    assert json.loads(chinese["tags"]) == ["approved", "campaign"]


def test_quiz_export_empty_headers_and_auth(quiz_catalog):
    import csv
    from fastapi.testclient import TestClient
    from keepstack.app import app

    client = TestClient(app)
    assert client.get("/api/assets/export.csv").status_code == 401
    token = make_token(quiz_catalog["admin"]["id"], quiz_catalog["admin"]["role"])
    response = client.get("/api/assets/export.csv", params={"q": "definitely-not-present"},
                          headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert rows == []
    assert response.content.decode("utf-8-sig").splitlines()[0] == \
        "uuid,title,description,filename,media_type,tags,created_at"


def test_quiz_filter_options_require_auth_and_include_active_types(quiz_catalog):
    from fastapi.testclient import TestClient
    from keepstack.app import app

    client = TestClient(app)
    assert client.get("/api/filter-options").status_code == 401
    token = make_token(quiz_catalog["admin"]["id"], quiz_catalog["admin"]["role"])
    response = client.get("/api/filter-options", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    payload = response.json()
    assert {collection["name"] for collection in payload["collections"]} == {"Harbor Campaign", "Other"}
    assert "approved" in payload["tags"] and "internal" in payload["tags"]
    assert "image" in payload["media_types"] and "document" in payload["media_types"]


def test_quiz_http_rejects_invalid_pagination_and_status_override(quiz_catalog):
    from fastapi.testclient import TestClient
    from keepstack.app import app

    client = TestClient(app)
    token = make_token(quiz_catalog["admin"]["id"], quiz_catalog["admin"]["role"])
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/assets", params={"limit": 0}, headers=headers).status_code == 422
    assert client.get("/api/assets", params={"limit": 101}, headers=headers).status_code == 422
    assert client.get("/api/assets", params={"offset": -1}, headers=headers).status_code == 422
    assert client.get("/api/assets", params={"sort": "unsupported"}, headers=headers).status_code == 422
    response = client.get("/api/assets", params={"status": "deleted"}, headers=headers)
    assert response.status_code == 200
    assert all(item["status"] == "active" for item in response.json()["items"])


def test_quiz_mutations_are_visible_in_search_and_export(quiz_catalog):
    import csv
    from fastapi.testclient import TestClient
    from keepstack.app import app
    from keepstack.config import config
    from keepstack.auth import reset_login_throttle

    reset_login_throttle()
    client = TestClient(app)
    user = quiz_catalog["admin"]
    token = make_token(user["id"], user["role"])
    headers = {"Authorization": f"Bearer {token}"}
    asset = quiz_catalog["assets"][0]

    assert client.patch(f"/api/assets/{asset['uuid']}", json={
        "title": "Harbor Updated Title", "description": "Updated 中文 description",
    }, headers=headers).status_code == 200
    updated = client.get("/api/assets", params={"q": "updated"}, headers=headers).json()
    assert updated["total"] == 1 and updated["items"][0]["uuid"] == asset["uuid"]

    assert client.post(f"/api/assets/{asset['uuid']}/tags", json={"name": "new-tag"},
                       headers=headers).status_code == 200
    with_tag = client.get("/api/assets", params={"tag": "new-tag"}, headers=headers).json()
    assert with_tag["total"] == 1
    assert client.delete(f"/api/assets/{asset['uuid']}/tags/new-tag", headers=headers).status_code == 200
    without_tag = client.get("/api/assets", params={"tag": "new-tag"}, headers=headers).json()
    assert without_tag["total"] == 0

    export = client.get("/api/assets/export.csv", params={"q": "updated"}, headers=headers)
    rows = list(csv.DictReader(io.StringIO(export.content.decode("utf-8-sig"))))
    assert len(rows) == 1
    assert rows[0]["title"] == "Harbor Updated Title"
    assert rows[0]["description"] == "Updated 中文 description"


def test_quiz_seed_is_repeatable_and_builds_expected_fixture(quiz_catalog):
    from keepstack.quiz_seed import COLLECTION_NAME, COLLECTION_UUID, run_quiz_seed

    run_quiz_seed()
    first_assets = get_conn().execute("SELECT COUNT(*) c FROM assets").fetchone()["c"]
    run_quiz_seed()
    conn = get_conn()
    assert conn.execute("SELECT COUNT(*) c FROM assets").fetchone()["c"] == first_assets
    collection = conn.execute(
        "SELECT id, name FROM collections WHERE uuid = ?", (COLLECTION_UUID,)
    ).fetchone()
    assert collection is not None
    assert collection["name"] == COLLECTION_NAME
    result = search.search(
        q="harbor", collection_id=collection["id"], tags=["approved", "campaign"],
        exclude_tags=["internal"], media_type="image", limit=100,
    )
    assert result["total"] == 14
    document = search.search(
        q="harbor", collection_id=collection["id"], tags=["approved", "campaign"],
        exclude_tags=["internal"], media_type="document", limit=100,
    )
    assert document["total"] == 1
    assert document["items"][0]["title"] == "Harbor 中文 campaign document"
