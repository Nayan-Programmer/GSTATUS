"""
Supabase-backed storage for G-Status
=====================================

Replaces the old SQLite file (rank_data/rank.db) as the place where the rank
ceremony's text + photos, and the student photo library, live.

Why: SQLite is a file on the server's local disk. On Render's free plan and
on Vercel that disk is ephemeral (or read-only, or a different copy per
serverless instance), so the ceremony could vanish or differ between
machines. Supabase is a small hosted Postgres + object storage service, so
every server / every worker / every visitor sees the same data.

Only two Supabase features are used, both over plain HTTPS with urllib (no
extra pip dependency, consistent with how this project already talks to
Google Sheets):

  1. Storage  - holds the actual image bytes (rank photos, student photos).
  2. PostgREST - a single tiny table `gstatus_kv` (key TEXT primary key,
     value JSONB) holds the ceremony's names / order / photo references and
     the student-photo library's index. One row per "document".

Setup (see README for the full walkthrough):
  1. Create a free project at https://supabase.com.
  2. Storage -> New bucket -> name it (default expected: "gstatus").
  3. SQL editor -> run:
         create table if not exists gstatus_kv (
             key text primary key,
             value jsonb not null,
             updated_at timestamptz not null default now()
         );
  4. Project Settings -> API -> copy the Project URL and the
     `service_role` key (NOT the public anon key - this app writes on the
     server, the service_role key is what is allowed to bypass Row Level
     Security for that).
  5. Set environment variables SUPABASE_URL, SUPABASE_KEY and (optionally)
     SUPABASE_BUCKET on Render / Vercel, the same way ADMIN_TOKEN is set.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

HTTP_TIMEOUT_SECONDS = 15
KV_TABLE = "gstatus_kv"


class SupabaseError(Exception):
    """Raised when Supabase is not configured, or a call to it fails."""


def _url() -> str:
    return os.environ.get("SUPABASE_URL", "").strip().rstrip("/")


def _key() -> str:
    return os.environ.get("SUPABASE_KEY", "").strip()


def _bucket() -> str:
    return os.environ.get("SUPABASE_BUCKET", "gstatus").strip() or "gstatus"


def configured() -> bool:
    return bool(_url() and _key())


def _require_configured() -> None:
    if not configured():
        raise SupabaseError(
            "Supabase is not configured. Set the SUPABASE_URL and SUPABASE_KEY "
            "environment variables (see README)."
        )


def _request(
    url: str,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict | None = None,
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method=method)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise SupabaseError(f"Could not reach Supabase: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Key/value document store (replaces the sqlite `rank_settings` / `rank_slots`
# tables). Each "document" is one JSON value under one key.
# ---------------------------------------------------------------------------
def kv_get(key: str) -> dict | None:
    """Return the stored JSON value for `key`, or None if there isn't one."""
    _require_configured()
    url = f"{_url()}/rest/v1/{KV_TABLE}?key=eq.{key}&select=value"
    status, body = _request(
        url,
        headers={
            "apikey": _key(),
            "Authorization": f"Bearer {_key()}",
            "Accept": "application/json",
        },
    )
    if status >= 400:
        raise SupabaseError(f"Supabase read failed ({status}): {body.decode(errors='replace')[:200]}")
    rows = json.loads(body or b"[]")
    return rows[0]["value"] if rows else None


def kv_set(key: str, value: dict) -> None:
    """Create or overwrite the JSON value stored under `key`."""
    _require_configured()
    url = f"{_url()}/rest/v1/{KV_TABLE}"
    payload = json.dumps([{"key": key, "value": value}]).encode("utf-8")
    status, body = _request(
        url,
        method="POST",
        data=payload,
        headers={
            "apikey": _key(),
            "Authorization": f"Bearer {_key()}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
    )
    if status >= 400:
        raise SupabaseError(f"Supabase write failed ({status}): {body.decode(errors='replace')[:200]}")


# ---------------------------------------------------------------------------
# Photo storage
# ---------------------------------------------------------------------------
def photo_upload(path: str, blob: bytes, content_type: str) -> None:
    """Upload (or replace) the object at `path` inside the configured bucket."""
    _require_configured()
    url = f"{_url()}/storage/v1/object/{_bucket()}/{path}"
    status, body = _request(
        url,
        method="POST",
        data=blob,
        headers={
            "apikey": _key(),
            "Authorization": f"Bearer {_key()}",
            "Content-Type": content_type,
            "x-upsert": "true",
        },
    )
    if status >= 400:
        raise SupabaseError(f"Supabase photo upload failed ({status}): {body.decode(errors='replace')[:200]}")


def photo_download(path: str) -> bytes | None:
    """Return the raw bytes at `path`, or None if it doesn't exist."""
    _require_configured()
    url = f"{_url()}/storage/v1/object/{_bucket()}/{path}"
    status, body = _request(
        url,
        headers={"apikey": _key(), "Authorization": f"Bearer {_key()}"},
    )
    if status == 404:
        return None
    if status >= 400:
        raise SupabaseError(f"Supabase photo read failed ({status}): {body.decode(errors='replace')[:200]}")
    return body


def photo_delete(path: str) -> None:
    _require_configured()
    url = f"{_url()}/storage/v1/object/{_bucket()}/{path}"
    status, body = _request(
        url,
        method="DELETE",
        headers={"apikey": _key(), "Authorization": f"Bearer {_key()}"},
    )
    if status >= 400 and status != 404:
        raise SupabaseError(f"Supabase photo delete failed ({status}): {body.decode(errors='replace')[:200]}")


def new_version() -> int:
    """A number that changes every upload, so browsers never cache a stale photo."""
    return time.time_ns() // 1_000_000
