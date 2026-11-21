"""
Claude-written application drafts.

A draft renders and compiles the documents into .pending/<draft_id>/ without touching the
database, so Claude can check page counts and fix errors first. Committing a draft makes it
a 'ready' application with the same id; uncommitted drafts expire with other stale pending
folders.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.logger import audit
from src.utils.file_manager import cleanup_pending, pending_dir
from src.utils.latex_safety import page_count

DRAFT_FILE = "draft.json"
DRAFT_ID_PATTERN = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


class DraftNotFound(LookupError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_draft(working_dir: Path, cv_gen, cl_gen, job: dict, cv_tex: str, cover_letter_body: str,
                 meta: dict[str, Any]) -> dict[str, Any]:
    """
    Render both documents for *job* into a new pending folder and store *meta* with them.
    Returns the draft record. Raises the generators' errors; nothing is left behind on failure.
    """
    draft_id = str(uuid.uuid4())
    folder = pending_dir(working_dir, draft_id)
    folder.mkdir(parents=True)
    try:
        cv_pdf = cv_gen.render(cv_tex, job, output_folder=folder)
        cl_pdf = cl_gen.render(cover_letter_body, job, output_folder=folder)
    except Exception:
        cleanup_pending(folder)
        raise

    record = {
        **meta,
        "draft_id": draft_id,
        "cv_path": str(cv_pdf),
        "cover_letter_path": str(cl_pdf),
        "cv_pages": page_count(cv_pdf),
        "cover_letter_pages": page_count(cl_pdf),
        "created_at": _now(),
        "committed_at": None,
    }
    _write(folder, record)
    audit("draft_created", draft_id=draft_id, job_id=meta.get("job_id") or "new")
    return record


def load_draft(working_dir: Path, draft_id: str) -> dict[str, Any]:
    """The stored draft record; raises DraftNotFound for unknown or malformed ids."""
    if not re.fullmatch(DRAFT_ID_PATTERN, draft_id):
        raise DraftNotFound(f"Draft {draft_id} not found")
    try:
        return json.loads((pending_dir(working_dir, draft_id) / DRAFT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise DraftNotFound(f"Draft {draft_id} not found") from None


def mark_committed(working_dir: Path, record: dict[str, Any]) -> None:
    record["committed_at"] = _now()
    _write(pending_dir(working_dir, record["draft_id"]), record)


def _write(folder: Path, record: dict[str, Any]) -> None:
    (folder / DRAFT_FILE).write_text(json.dumps(record, indent=2), encoding="utf-8")
