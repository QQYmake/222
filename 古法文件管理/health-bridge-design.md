# Health Bridge Design

Date: 2026-07-11
Status: Approved in principle by the user; pending written-spec review

## 1. Objective

Build a private health-data pipeline that:

1. Watches Gadgetbridge's automatically exported SQLite database on a Huawei MatePad.
2. Uploads changed database snapshots to a VPS through HTTPS.
3. Validates, deduplicates, and incrementally normalizes health observations on the VPS.
4. Generates weekly Markdown archives grouped by local date, time, and data type.
5. Publishes authenticated HTTPS query endpoints for latest and historical data.
6. Provides separate Python push and pull programs that can run once or as long-running processes.

The required first-version data types are heart rate, sleep, and steps. All three must have implemented and tested schema mappings before the end-to-end system is considered complete. The ingestion design must allow additional Gadgetbridge data types without redesigning the transport or API.

## 2. Confirmed Environment

- Gadgetbridge runs on a Huawei MatePad.
- Gadgetbridge automatically exports its database to:
  `/storage/emulated/0/Download/health/Gadgetbridge.db`
- Termux is installed on the tablet; a Debian proot is also available.
- The VPS runs Ubuntu and already has Codex CLI access.
- Nginx 1.18 listens on ports 80 and 443.
- `https://oh-my-frontweb.duckdns.org/` redirects to `/chat/`.
- The health service will reuse the main hostname and the `/health/` path.
- All client programs will be Python; no PowerShell implementation is required.
- Timezone for display, weekly grouping, and Markdown archives is `Asia/Shanghai`.
- Weeks run Monday through Sunday and use ISO week identifiers such as `2026-W28`.

## 3. Chosen Architecture

Use a single-user, low-volume architecture:

- Nginx remains the public TLS endpoint.
- A FastAPI service listens only on `127.0.0.1:8765`.
- Nginx proxies `location ^~ /health/` to FastAPI without changing the existing `/chat/` behavior.
- A server-side SQLite database stores normalized observations and ingestion metadata.
- Raw accepted Gadgetbridge snapshots are retained outside the public API surface.
- Python clients communicate only through HTTPS.

PostgreSQL, message queues, containers, and a public database port are intentionally excluded from the first version.

## 4. Components and Boundaries

### 4.1 Tablet push client: `health_push.py`

The push client is independent from the pull client. It supports:

- One-shot mode: inspect and upload once.
- Watch mode: remain running, poll at a configurable interval, and upload only changed exports.

Default behavior:

1. Watch `/storage/emulated/0/Download/health/Gadgetbridge.db`.
2. Require the source file's size and modification time to remain unchanged across two checks before copying it.
3. Copy the source to a private temporary file; never upload directly from the actively replaced export path.
4. Open the copy read-only and run `PRAGMA quick_check`.
5. Compute SHA-256.
6. Skip the upload when the SHA-256 equals the last successfully uploaded snapshot.
7. Gzip the validated copy and upload it using HTTPS multipart form data.
8. Authenticate with a dedicated upload token.
9. Retry transient network and 5xx failures with capped exponential backoff and jitter.
10. Do not retry permanent authentication or validation failures indefinitely.
11. Persist only operational state such as the last successful hash, timestamp, and failure summary.
12. Never log tokens or full response bodies that may contain sensitive data.

Termux native Python is the preferred runtime because it has direct access to Android shared storage after `termux-setup-storage`. The Debian proot may run the same program if the database path is correctly exposed, but it is not required.

### 4.2 Pull client: `health_pull.py`

The pull client is a separate Python program and configuration. It must work on both Termux/Debian and Windows Python.

It supports:

- One-shot queries printed as JSON or saved to a specified file.
- Watch mode that periodically requests the latest value for one or more data types, writes updates atomically to local files, and emits concise change notifications to stdout.
- Querying a type over an explicit time range.
- Listing available weekly archives.
- Downloading a weekly Markdown archive.

Representative commands:

```text
python health_pull.py latest heart_rate
python health_pull.py range heart_rate --from 2026-07-01T00:00:00+08:00 --to 2026-07-08T00:00:00+08:00
python health_pull.py week 2026-W28 --type heart_rate
python health_pull.py watch heart_rate sleep steps --interval 60 --output-dir ./latest
```

The pull client uses a read-only bearer token and cannot upload, delete, or change server data.

### 4.3 VPS API and ingestion service

Public endpoints are rooted at:

```text
https://oh-my-frontweb.duckdns.org/health/api/v1
```

Required endpoints:

- `GET /health/api/v1/health` — liveness and version information; no private observations.
- `POST /health/api/v1/upload` — receive one compressed Gadgetbridge database snapshot.
- `GET /health/api/v1/latest?type=heart_rate` — latest normalized observation for one type.
- `GET /health/api/v1/data?type=heart_rate&from=...&to=...&limit=...` — ordered range query.
- `GET /health/api/v1/weeks` — available ISO week archives.
- `GET /health/api/v1/archive/{week}/{type}` — weekly Markdown for one data type.

Upload and read authentication are separate:

- Upload: `X-Upload-Token` header.
- Read: `Authorization: Bearer <read-token>`.

Tokens are randomly generated, stored in a root-readable or service-user-readable environment file, and never committed to a repository. Token comparisons must use a constant-time comparison.

### 4.4 Gadgetbridge schema adapters

The implementation must inspect an actual Gadgetbridge 0.92.1 export before finalizing mappings. It must not assume that historical table names such as `MI_BAND_ACTIVITY_SAMPLE` apply to Xiaomi protobuf devices.

The parser boundary consists of:

- Schema inspector: lists tables, columns, indexes, foreign keys, and sample non-sensitive shapes.
- Device resolver: maps Gadgetbridge device and user identifiers into stable internal identifiers.
- Type adapters: initially heart rate, sleep, and steps.
- Normalizer: produces a common observation representation.

If an export has an unknown schema:

- Retain the raw validated snapshot.
- Record an `unsupported_schema` ingestion result.
- Return HTTP 202 with `status: unsupported_schema`; the push client treats the snapshot as successfully delivered, records the server status, and does not retry it indefinitely.
- Do not silently generate empty archives.

Corrupt input, non-SQLite input, failed `PRAGMA quick_check`, or an exceeded size limit is a validation failure and returns HTTP 422. A rejected validation failure is distinct from a valid retained snapshot whose Gadgetbridge schema is not yet supported.

### 4.5 Normalized storage

The server SQLite database stores:

- Ingestion snapshot metadata: hash, received time, source database version/schema fingerprint, validation status, import counts, and error summary.
- Stable devices and users without exposing them through unauthenticated endpoints.
- Normalized observations containing data type, UTC timestamp, Asia/Shanghai timestamp, normalized value payload, source table, source identity, and import provenance.
- Archive generation state.

Every normalized record requires a deterministic uniqueness key derived from device, type, source timestamp, source identity, and stable value fields. Re-uploading identical or overlapping Gadgetbridge databases must not duplicate observations.

The original source timestamp and raw source fields needed for future reinterpretation must be retained in structured JSON, while public query responses use normalized fields.

### 4.6 Archive generator

Affected archives are regenerated after a successful import.

Directory layout:

```text
/srv/health-bridge/archives/2026-W28/
  summary.md
  heart_rate.md
  sleep.md
  steps.md
```

Rules:

- ISO week in `Asia/Shanghai`, Monday through Sunday.
- Deterministic output and stable ordering.
- Each type file groups observations by local date, then local time.
- `summary.md` lists available types, record counts, covered dates, and links/paths to type files.
- Heart-rate archives list timestamp and BPM.
- Sleep archives list detected sessions and stages when the source provides them; raw stage codes must not be presented as medically authoritative.
- Step archives provide timestamped samples or daily totals according to what the source schema reliably exposes.
- Atomic replacement prevents readers from seeing partially written files.

Latest JSON files are generated atomically under:

```text
/srv/health-bridge/latest/heart_rate.json
/srv/health-bridge/latest/sleep.json
/srv/health-bridge/latest/steps.json
```

The API may query normalized SQLite directly, but latest files remain useful for inspection, backup, and simple integrations.

## 5. Server Filesystem and Runtime

Target layout:

```text
/srv/health-bridge/
  app/
  data/incoming/
  data/raw/
  data/health.sqlite3
  archives/
  latest/
  logs/
```

Runtime requirements:

- Dedicated unprivileged service account.
- FastAPI/Uvicorn managed by systemd.
- Service binds only to `127.0.0.1:8765`.
- No public firewall rule for port 8765.
- Raw snapshots, tokens, and normalized database are not readable by the Nginx user unless explicitly required.
- Log rotation or journald retention prevents unbounded logs.

## 6. Nginx Integration

Before changes:

1. Identify the active server block for `oh-my-frontweb.duckdns.org`.
2. Back up that configuration.
3. Confirm existing `/chat/` behavior and certificate paths.

Add a path-specific proxy similar to:

```nginx
location ^~ /health/ {
    client_max_body_size 100m;
    proxy_pass http://127.0.0.1:8765;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

The exact change must follow the existing configuration structure. Run `nginx -t` before reload and verify `/chat/` after reload. Roll back immediately if the existing service changes behavior.

## 7. API Response Conventions

- JSON uses UTF-8.
- Timestamps are RFC 3339.
- Each observation includes `timestamp_utc` and `timestamp_local` with offset.
- Range results are ordered ascending unless explicitly requested otherwise.
- Pagination uses a bounded `limit` and cursor; unbounded full-history responses are not allowed.
- Errors use a stable object containing `error.code`, `error.message`, and a request ID.
- Upload responses include snapshot hash, whether it was new or duplicate, validation/import status, and per-type imported counts.
- Markdown archive endpoints return `text/markdown; charset=utf-8`.

## 8. Reliability and Security

- HTTPS certificate validation is mandatory; clients must not expose an insecure-skip option in normal usage.
- Separate upload and read tokens support independent revocation.
- Nginx limits upload size; the API also enforces a decompressed size limit to prevent gzip bombs.
- Validate SQLite magic bytes, decompressed size, read-only opening, and `PRAGMA quick_check`.
- Store uploads under generated server filenames; never trust client paths or filenames.
- Use temporary files and atomic rename for accepted snapshots, latest JSON, and Markdown.
- Hash-based deduplication and deterministic observation keys make retries safe.
- A lock prevents concurrent ingestion of overlapping snapshots.
- Keep the previous valid normalized database backup before migrations.
- Health data is descriptive, not diagnostic; no medical conclusions are generated.

## 9. Configuration

Push and pull configurations are separate. Neither token appears in source code.

Push configuration includes:

- Database source path.
- Upload URL.
- Upload token source.
- Poll interval, stability delay, timeout, retry limits, and state path.

Pull configuration includes:

- API base URL.
- Read token source.
- Default timezone, output format, watch interval, and output directory.

Environment variables and permission-restricted config files are supported. Example configuration files contain placeholders only.

## 10. Testing and Acceptance Criteria

### Client tests

- Stable-file detection rejects an actively changing export.
- SQLite validation accepts a fixture and rejects corrupt input.
- Unchanged hash skips upload.
- Retry policy distinguishes transient and permanent failures.
- Tokens are redacted from logs.
- Pull commands serialize latest, range, and archive responses correctly.
- Watch mode writes files atomically and does not rewrite unchanged results.
- Python clients run on current Termux Python and supported Windows Python.

### Server tests

- Authentication separation is enforced.
- Valid upload imports exactly once; duplicate upload is idempotent.
- Invalid SQLite and oversized gzip are rejected.
- Actual Gadgetbridge sample schema maps heart rate correctly before declaring support complete.
- Heart rate, sleep, and steps are correctly mapped and tested. If the first real sample contains no rows for a required type, use schema evidence plus a representative synthetic fixture, then verify against real rows when they become available; absence of current rows is not permission to omit the adapter.
- Range filters and pagination are correct across timezone and ISO-week boundaries.
- Weekly Markdown output is deterministic.
- Unknown schema preserves raw input and reports a clear state.

### Deployment acceptance

- `https://oh-my-frontweb.duckdns.org/chat/` behaves exactly as before.
- `/health/api/v1/health` responds through Nginx HTTPS.
- Port 8765 is not publicly reachable.
- A tablet one-shot push succeeds and returns the expected hash/import counts.
- Heart-rate latest and range queries work from both Termux and Windows Python.
- Both clients run successfully in long-running mode.
- A weekly Markdown archive is generated from real imported data.
- Service restart does not lose data or duplicate observations.

## 11. Delivery Artifacts

Local deliverables:

- `health_push.py`
- `health_pull.py`
- Example push and pull configuration files without secrets.
- Termux setup and long-running instructions.
- Windows Python setup and long-running instructions.
- Client unit tests.

VPS Codex handoff:

- A complete implementation prompt containing this approved architecture, environment facts, safety constraints, phased checkpoints, verification commands, and required deliverables.
- The VPS implementation is responsible for inspecting the actual exported database before committing schema mappings.

This document specifies the complete end-to-end system, but implementation is split across two workspaces:

- The current local workspace implements and tests the push client, pull client, example configurations, documentation, and the VPS Codex handoff prompt.
- The VPS Codex session implements and deploys the FastAPI service, parser adapters, normalized storage, archive generator, systemd unit, and Nginx integration from that prompt.

The current local implementation plan must not pretend to deploy or verify the VPS directly because this session has no remote-control or SSH connection to it. End-to-end deployment acceptance is completed interactively after the user runs the VPS prompt and returns verification output.

## 12. Deferred Features

- Public dashboards or graphical UI.
- PostgreSQL migration.
- Multiple users or tenant isolation.
- Medical interpretation or alerting.
- Cloud object storage.
- Device command/control through Gadgetbridge.
- Automatic deletion or retention policies for raw snapshots.
