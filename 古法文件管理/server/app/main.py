"""FastAPI application entry point.

Routes all 6 endpoints defined in the architecture plan.
Binds to 127.0.0.1:8765 (behind Nginx reverse proxy).

Endpoints:
  GET  /health/api/v1/health    — liveness check (no auth)
  POST /health/api/v1/upload    — ingest gzip SQLite snapshot (upload token)
  GET  /health/api/v1/latest    — latest values per type (read token)
  GET  /health/api/v1/data      — time-range query (read token)
  GET  /health/api/v1/weeks     — list available archive weeks (read token)
  GET  /health/api/v1/archive/{week} — read archive markdown (read token)
"""

from __future__ import annotations

import gzip
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile, File
from fastapi.responses import JSONResponse, PlainTextResponse

from .auth import verify_upload_token, verify_read_token
from .config import ServerConfig, load_server_config
from .database import connect, init_database, query_latest, query_range
from .ingest import ingest_snapshot, IngestError, IngestStatus
from .archive import list_archives, read_archive, generate_archive
from .latest import generate_latest, read_latest

logger = logging.getLogger("health_bridge")

# ---------------------------------------------------------------------------
# Config singleton (loaded once at startup)
# ---------------------------------------------------------------------------

_config: ServerConfig | None = None


def get_config() -> ServerConfig:
    global _config
    if _config is None:
        _config = load_server_config()
    return _config


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = get_config()
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.raw_dir.mkdir(parents=True, exist_ok=True)
    cfg.incoming_dir.mkdir(parents=True, exist_ok=True)
    cfg.archives_dir.mkdir(parents=True, exist_ok=True)
    cfg.latest_dir.mkdir(parents=True, exist_ok=True)
    init_database(cfg.db_path)
    logger.info("Health-Bridge server started on %s:%d", cfg.listen_host, cfg.listen_port)
    yield
    logger.info("Health-Bridge server shutting down")


app = FastAPI(
    title="Health-Bridge API",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# 1. Liveness check (no auth)
# ---------------------------------------------------------------------------

@app.get("/health/api/v1/health")
async def health_check():
    """Return version + alive status. No token required."""
    return {"status": "ok", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# 2. Upload endpoint (upload token)
# ---------------------------------------------------------------------------

@app.post("/health/api/v1/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    """Accept a gzip-compressed SQLite snapshot.

    INPUT:  multipart/form-data with 'file' field = gzip bytes
    AUTH:   X-Upload-Token header
    OUTPUT: JSON with ingest result (hash, status, counts, affected weeks)
    """
    cfg = get_config()
    verify_upload_token(request, cfg.upload_token)

    raw = await file.read()
    if len(raw) > cfg.max_body_bytes:
        raise HTTPException(status_code=413, detail="Request body too large")

    try:
        result = ingest_snapshot(raw, cfg)
    except IngestError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
        )

    # Generate archive + latest for affected weeks.
    if result.affected_weeks and result.status != IngestStatus.UNSUPPORTED_SCHEMA:
        for week_id in result.affected_weeks:
            try:
                generate_archive(week_id, cfg)
            except Exception:
                logger.exception("Failed to generate archive for %s", week_id)
        try:
            generate_latest(cfg)
        except Exception:
            logger.exception("Failed to generate latest.json")

    status_code = 200 if result.is_new else 200  # duplicate is also 200
    if result.status == IngestStatus.UNSUPPORTED_SCHEMA:
        status_code = 200  # accepted but no data extracted

    return JSONResponse(
        status_code=status_code,
        content={
            "snapshot_hash": result.snapshot_hash,
            "status": result.status.value,
            "is_new": result.is_new,
            "new_count": result.new_count,
            "duplicate_count": result.duplicate_count,
            "schema_fingerprint": result.schema_fingerprint,
            "affected_weeks": result.affected_weeks,
        },
    )


# ---------------------------------------------------------------------------
# 3. Latest endpoint (read token)
# ---------------------------------------------------------------------------

@app.get("/health/api/v1/latest")
async def latest(
    request: Request,
    type: Optional[str] = Query(None, description="Filter by observation type"),
):
    """Return latest values per type, or a specific type.

    INPUT:  optional ?type=heart_rate
    AUTH:   Bearer token
    OUTPUT: JSON with latest observation(s)
    """
    cfg = get_config()
    verify_read_token(request, cfg.read_token)

    data = read_latest(cfg)
    if data is None:
        data = generate_latest(cfg)

    if type is not None:
        return {type: data.get(type)}
    return data


# ---------------------------------------------------------------------------
# 4. Data range query (read token)
# ---------------------------------------------------------------------------

@app.get("/health/api/v1/data")
async def data(
    request: Request,
    type: str = Query(..., description="Observation type"),
    from_ts: Optional[str] = Query(None, alias="from"),
    to_ts: Optional[str] = Query(None, alias="to"),
    limit: int = Query(100, ge=1, le=1000),
    cursor: Optional[str] = Query(None),
):
    """Query observations in a time range with pagination.

    INPUT:  type, optional from/to timestamps, limit, cursor
    AUTH:   Bearer token
    OUTPUT: JSON with observations array + next_cursor
    """
    cfg = get_config()
    verify_read_token(request, cfg.read_token)

    with connect(cfg.db_path) as conn:
        page = query_range(
            conn,
            obs_type=type,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            cursor=cursor,
        )

    return {
        "observations": [
            {
                "type": obs.type,
                "timestamp_utc": obs.timestamp_utc,
                "timestamp_local": obs.timestamp_local,
                "value": obs.value,
                "source_table": obs.source_table,
                "source_identity": obs.source_identity,
            }
            for obs in page.observations
        ],
        "next_cursor": page.next_cursor,
    }


# ---------------------------------------------------------------------------
# 5. List archive weeks (read token)
# ---------------------------------------------------------------------------

@app.get("/health/api/v1/weeks")
async def weeks(request: Request):
    """List all available archive week IDs.

    INPUT:  none
    AUTH:   Bearer token
    OUTPUT: JSON with weeks array
    """
    cfg = get_config()
    verify_read_token(request, cfg.read_token)

    return {"weeks": list_archives(cfg)}


# ---------------------------------------------------------------------------
# 6. Read archive (read token)
# ---------------------------------------------------------------------------

@app.get("/health/api/v1/archive/{week_id}")
async def archive(request: Request, week_id: str):
    """Read a specific week's Markdown archive.

    INPUT:  week_id path param (e.g. "2026-W28")
    AUTH:   Bearer token
    OUTPUT: text/markdown
    """
    cfg = get_config()
    verify_read_token(request, cfg.read_token)

    content = read_archive(week_id, cfg)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Archive {week_id} not found")

    return PlainTextResponse(content, media_type="text/markdown")
