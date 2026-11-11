# Changelog

All notable changes to Keepstack are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project aims to follow
semantic versioning.

## [Unreleased]

### Added

- Combined active-asset filtering by keyword, collection, media type, multiple
  required tags, and multiple excluded tags. Lists and exports share one query,
  with stable creation-time ordering and 12-row pagination in the web client.
- `GET /api/assets/export.csv` for an authenticated, UTF-8-BOM CSV export of all
  matching assets. Tags are a sorted JSON array in each row.
- `python -m keepstack quiz-seed` for deterministic, idempotent acceptance data.

- Parse common XMP RDF fields into structured metadata, catalog fields, and
  full-text search.
- OCR images at ingest through the Tesseract binary when it is installed, so
  text inside scans and photos becomes searchable. No new Python dependencies;
  disable with `KEEPSTACK_OCR_ENABLED=false`.
- On-device CLIP vision model (optional `pip install onnxruntime`): real
  semantic search over image content, visual similarity, and zero-shot
  auto-tagging with no API keys, plus a `python -m keepstack reindex` command
  to re-embed an existing catalog.
- Advertise IIIF Image API level 2 and support region cropping, percent
  regions, best-fit sizing, and rotation for IIIF image requests.
- Added `python -m keepstack gc` to remove blob files no longer referenced by
  active assets or their version history.

- IIIF Presentation API 3.0 manifest at `GET /iiif/3/{uuid}/manifest.json`, so
  viewers like Mirador and Universal Viewer open assets directly with their
  label and metadata.
- Login rate limiting: repeated failed attempts for a username and IP are
  locked out with a 429 and `Retry-After`, and the lockout is audit-logged.
  Tunable with `KEEPSTACK_LOGIN_MAX_ATTEMPTS` and `KEEPSTACK_LOGIN_LOCKOUT_SECONDS`.

### Changed

- Switched the web interface to a light theme: white and soft-gray surfaces
  with an indigo accent, applied across the app, placeholders, and share pages.
- Centered the captions in generated seed images so square thumbnails no
  longer crop them.
- Tidied the chrome: background scrolling now locks while the drawer or upload
  modal is open (one scrollbar instead of two), scrollbars are slim and themed,
  and the dropdown controls got a custom chevron with roomier padding.

## [0.1.0] - 2026-06-28

The first working release. A complete DAM core, built to sit in the gap between
standards-rich heritage tools and modern AI photo libraries.

### Added

- Content-addressable blob storage with automatic deduplication and SHA-256 fixity
- Ingest pipeline with EXIF / IPTC / XMP extraction and thumbnail generation
- Search: SQLite FTS5 full-text, faceted filtering, and embedding-based semantic
  search with a "more like this" mode
- Collections, tags (manual and AI-suggested), and per-asset version history
- Four-role access control (admin, editor, contributor, viewer), an audit log on
  every state change, and tokenised expiring share links
- Open standards: Dublin Core mapping, an OAI-PMH 2.0 endpoint, and an IIIF
  Image API endpoint for every asset
- A repository-wide fixity check for digital-preservation integrity
- Optional AI enrichment (Groq vision, Cohere embeddings) with deterministic
  offline fallbacks, so the product is fully useful with zero API keys
- A zero-build two-tone web client and a REST API covering every resource
- A passing test suite, synthetic seed data, and a Docker deployment

### Known limitations

Tracked in [ROADMAP.md](ROADMAP.md) and graded honestly in
[COMPARISON.md](COMPARISON.md): AI is heuristic without a provider key,
preservation is fixity rather than format migration, IIIF is level 1, and there
is no OCR, SSO, or published accessibility conformance report yet.
