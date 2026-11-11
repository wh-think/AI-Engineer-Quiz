# AI Engineer Quiz Submission

## Repository and branch

- Fork: https://github.com/wh-think/AI-Engineer-Quiz
- Branch: `codex/quiz-search-export`
- Base: `3aa137c` (`main` when the work started)
- Final commit SHA: the branch HEAD reported in the submission email. The SHA is
  not embedded here so that this document can remain part of the final commit.

The upstream and quiz history are preserved. Work is split into logical commits:

- `473b7d7` combined filtering and CSV export
- `84b8991` filter controls, pagination, and browser download
- `cd556bf` reproducible acceptance data
- `ebb4a27` batched tag lookup for exports
- `7347431` submission notes and changelog
- `661231f` page-only pagination parameters
- `39af266` typed search and asset-state enums
- `f6f1ead` POST encoding for JSON requests and explicit button types
- The accurate final SHA is the branch HEAD at review time; report it in the
  submission email.

## Run and verify

```bash
uv venv --python 3.12
uv pip install -e '.[dev]'

export KEEPSTACK_AI_ENABLED=false
export KEEPSTACK_OCR_ENABLED=false
export KEEPSTACK_DATA_DIR=/tmp/keepstack-quiz

uv run --no-sync python -m keepstack seed
uv run --no-sync python -m keepstack quiz-seed
uv run --no-sync python -m keepstack
```

Open <http://localhost:8000> and sign in with `admin` / `admin`.

The regular seed creates 24 demo assets and three demo collections. The quiz
fixture adds 19 assets in `Harbor Campaign`, for 41 active assets in total. The
fixture is safe to run repeatedly. It creates 19 assets in `Harbor Campaign`:
15 match the business filter and 14 of those are images. The 14 images share one
`created_at`, so the second page exercises the stable `created_at, id` ordering
tie-breaker. The fixture also includes an internal-tag exclusion, a missing
required-tag case, a deleted asset, a Chinese document, and CSV special
characters (comma, quote, and newline).

Manual scenario:

1. Search for `harbor`.
2. Select `Harbor Campaign`.
3. Add `approved` and `campaign` under Include tags and `internal` under Exclude tags.
4. Select `image`, then switch between Oldest and Newest and use Previous/Next.
5. Export CSV. The download contains all 14 image rows, not just the current page.

Run tests:

```bash
uv run --no-sync python -m pytest -q
```

## Test results

Local run on 2026-11-11:

```text
31 passed, 2 skipped
```

The skipped tests require the optional local CLIP model and Tesseract binary.
Automated coverage includes combined filters, required/excluded tag semantics,
active-only results, duplicates and conflicts, stable multipage ordering, list
and export equality, Chinese and CSV quoting, empty export headers, invalid
pagination, authentication, and metadata/tag changes after query and export.

Browser verification with Playwright at 1440px and 390px wide confirmed:

- 24 default seeded assets render as two pages of 12 with working Previous/Next.
- Include/exclude tag chips, conflict-empty state, and Clear all work.
- CSV download starts with a UTF-8 BOM; a forced 500 response displays an inline error.
- Desktop and mobile pages have no horizontal overflow.

## APIs and data format

`GET /api/assets` accepts one or more `tag` parameters (all are required) and one
or more `exclude_tag` parameters (any match excludes the asset). It preserves the
single-value compatibility form `?tag=approved`. It accepts `q`, `media_type`,
`collection_id`, `sort=newest|oldest`, `limit=1..100`, and `offset>=0`.
Responses include `total`, `items`, `limit`, and `offset`. Only active assets are
returned.

`GET /api/assets/export.csv` accepts the same filter and sort fields and requires
the existing Bearer login. It has no pagination contract; any `limit` or
`offset` query values are ignored so it always returns every matching row.
Columns are `uuid,title,description,filename,media_type,tags,created_at`.

The CSV is UTF-8 with a BOM for spreadsheet interoperability. `tags` is a JSON
array serialized in sorted name order, for example `["approved","campaign"]`.
The standard-library CSV writer handles Chinese text, commas, quotes, and
newlines; zero matches still produce the header.

## Tradeoffs and limits

- Search reuses the existing FTS fields and tokenizer. Keyword query cleaning now
  treats input with no searchable terms as no keyword restriction.
- List and export share one SQL builder, named parameters, and stable ordering.
  Collection and tag conditions use `EXISTS` subqueries to avoid duplicate rows.
- Pagination is offset-based. It is consistent for unchanged data, as requested;
  it is not a cross-request snapshot or cursor protocol.
- Facet counts describe the whole active catalog and are explicitly labeled that
  way in the UI. They are not current-result counts.
- Export generates rows with a streaming response and batches tag lookup. It is
  still synchronous and is not intended for an untrusted multi-million-row job.
- Semantic search remains available internally but its page control is hidden;
  the quiz UI uses keyword search only.

## Known out-of-scope issues

- Asset-version replacement does not refresh MIME, dimensions, metadata, or the
  thumbnail cache.
- The public share page interpolates title and description into HTML without
  escaping, leaving a potential persistent XSS risk.
- FastAPI's existing startup event uses the deprecated `on_event` API.
- The optional CLIP and OCR tests are skipped when their local dependencies are
  absent.

No database, uploaded blob, virtual environment, or credential is included in the
commits.
