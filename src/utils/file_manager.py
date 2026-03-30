"""
File management for the generate → approve/reject workflow.

Documents are generated into a temporary `.pending/{app_id}/` folder.
On approval they are promoted to `{country}/{company}/` with proper naming.
On rejection the pending folder is deleted.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path

from src.logger import get_logger
from src.utils.sanitize import sanitize_company_for_path

log = get_logger(__name__)

COUNTRY_FOLDERS = {
    "DE": "germany", "AE": "uae", "SA": "ksa",
    "CH": "switzerland", "NL": "netherlands", "US": "us",
    "QA": "qatar", "GB": "uk", "FR": "france",
}


def _sanitize_name(name: str) -> str:
    """Turn 'First Last' into 'First_Last', keeping only word characters."""
    return re.sub(r"[^\w]", "_", name.strip()).strip("_")


def pending_dir(working_dir: Path, app_id: str) -> Path:
    return working_dir / ".pending" / app_id


def final_dir(working_dir: Path, country: str, company: str) -> Path:
    code = country.upper()
    folder = COUNTRY_FOLDERS.get(code, code.lower() or "other")
    return working_dir / folder / sanitize_company_for_path(company)


def final_filenames(candidate_name: str, company: str) -> dict[str, str]:
    """Return a dict of target filenames for the four output files."""
    name = _sanitize_name(candidate_name)
    comp = sanitize_company_for_path(company)
    return {
        "cv_tex": f"{name}_{comp}_CV.tex",
        "cv_pdf": f"{name}_{comp}_CV.pdf",
        "cl_tex": f"{name}_{comp}_Cover_Letter.tex",
        "cl_pdf": f"{name}_{comp}_Cover_Letter.pdf",
    }


def promote_to_final(
    pending: Path,
    working_dir: Path,
    country: str,
    company: str,
    candidate_name: str,
) -> dict[str, str]:
    """
    Move .tex and .pdf files from pending to the final country/company folder
    with the proper naming convention.

    Returns a dict with keys cv_path and cover_letter_path (absolute strings).
    """
    dest = final_dir(working_dir, country, company)
    dest.mkdir(parents=True, exist_ok=True)
    names = final_filenames(candidate_name, company)

    file_map = {
        "cv.tex": names["cv_tex"],
        "cv.pdf": names["cv_pdf"],
        "cover_letter.tex": names["cl_tex"],
        "cover_letter.pdf": names["cl_pdf"],
    }

    for src_name, dst_name in file_map.items():
        src = pending / src_name
        if src.exists():
            shutil.copy2(src, dest / dst_name)

    # Clean up pending after successful copy
    cleanup_pending(pending)

    return {
        "cv_path": str(dest / names["cv_pdf"]),
        "cover_letter_path": str(dest / names["cl_pdf"]),
    }


def cleanup_pending(pending: Path) -> None:
    """Delete a pending folder and all its contents."""
    if pending.exists() and pending.is_dir():
        shutil.rmtree(pending)
        log.info("Cleaned up pending folder: %s", pending)


def cleanup_stale_pending(working_dir: Path, max_age_hours: int = 72) -> int:
    """Delete pending folders older than max_age_hours. Returns count deleted."""
    import time
    base = working_dir / ".pending"
    if not base.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    deleted = 0
    for folder in base.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder)
            log.info("Cleaned up stale pending folder: %s", folder.name)
            deleted += 1
    return deleted
