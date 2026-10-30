"""
Read-only index of job_applications/.

Layouts: nested <country>/<Company>/<Role_Slug>/ (JD.txt, CV, prep files)
         flat   <country>/<Company>/ (CV + cover letter; also where ApplAI promotes approved bundles)
Subfolders of a company that are not roles (Interviews/, event folders...)
contribute their prep files to every record of that company; archive folders
are skipped. Symlinks inside the root are never followed, and reads are
confined to the root.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from applai_mcp.models import FsApplication, FsMatch

# Mirrors ApplAI's src/utils/file_manager.py COUNTRY_FOLDERS, plus folders created by hand
COUNTRY_FOLDERS = {
    "germany": "DE", "uae": "AE", "ksa": "SA", "switzerland": "CH", "netherlands": "NL",
    "us": "US", "qatar": "QA", "uk": "GB", "france": "FR", "italy": "IT", "remote": "REMOTE",
}
_PREP_PATTERN = re.compile(r"prep|interview|_qa\b|negotiation|call_notes", re.IGNORECASE)
_DOC_SUFFIXES = {".md", ".pdf", ".tex", ".docx", ".txt"}
_NOTE_PATTERN = re.compile(r"^\s*NOTE\s*\((\d{4}-\d{2}-\d{2})\)\s*:?\s*(.+)$")
_MAX_NOTES = 5
_MAX_DEPTH = 4
_ARCHIVE_DIRS = {"old", "archive", "archived", "backup"}
# One entry per prep document: prefer the format an agent can read
_PREP_FORMAT_RANK = {".md": 0, ".txt": 1, ".tex": 2, ".pdf": 3, ".docx": 4}


def normalize(text: str) -> str:
    """Same normalization as ApplAI's dedup key: lowercase, drop gender suffixes and punctuation."""
    text = text.lower().replace("_", " ")
    text = re.sub(r"\s*[\(\[]\s*(?:m/f/d|f/m/d|m/w/d|w/m/d|f/m/x|all genders?|diverse)\s*[\)\]]\s*", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # "SAP" vs "SAP SE": a whole-word prefix/containment is a strong company match
    if re.search(rf"\b{re.escape(a)}\b", b) or re.search(rf"\b{re.escape(b)}\b", a):
        return 0.9
    return round(SequenceMatcher(None, a, b).ratio(), 3)


def _visible(path: Path) -> bool:
    if path.name.startswith((".", "~$")) or path.is_symlink():
        return False
    return not (path.is_dir() and path.name.lower() in _ARCHIVE_DIRS)


def _files(directory: Path, depth: int = _MAX_DEPTH) -> Iterator[Path]:
    for child in sorted(directory.iterdir()):
        if not _visible(child):
            continue
        if child.is_file():
            yield child
        elif child.is_dir() and depth > 0:
            yield from _files(child, depth - 1)


def _is_cv(path: Path) -> bool:
    return "_cv" in path.stem.lower() and path.suffix in {".pdf", ".tex", ".docx"}


def _is_cover_letter(path: Path) -> bool:
    return "cover_letter" in path.stem.lower()


def _is_prep(path: Path) -> bool:
    return path.suffix.lower() in _DOC_SUFFIXES and bool(_PREP_PATTERN.search(path.stem)) and not _is_cv(path)


def _is_role_dir(path: Path) -> bool:
    return any(f.name == "JD.txt" or _is_cv(f) for f in path.iterdir() if f.is_file() and _visible(f))


def _notes(jd: Path) -> list[str]:
    notes = []
    with jd.open(encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if i > 300 or len(notes) >= _MAX_NOTES:
                break
            if m := _NOTE_PATTERN.match(line):
                notes.append(f"{m.group(1)}: {m.group(2).strip()[:300]}")
    return notes


def _display(name: str) -> str:
    return name.replace("_", " ").strip()


class ApplicationsIndex:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _record(self, country: str, company_dir: Path, files: list[Path], role_dir: Path | None,
                extra_prep: list[Path]) -> FsApplication:
        best: dict[tuple[Path, str], Path] = {}
        for f in sorted(files + extra_prep, key=lambda f: _PREP_FORMAT_RANK.get(f.suffix.lower(), 9)):
            if _is_prep(f):
                best.setdefault((f.parent, f.stem), f)
        prep = sorted(self._rel(f) for f in best.values())
        has_cv = any(_is_cv(f) for f in files)
        jd = next((f for f in files if f.name == "JD.txt" and f.parent == (role_dir or company_dir)), None)
        stage = "interviewing" if prep else "prepared" if has_cv else "researching"
        mtimes = [f.stat().st_mtime for f in files + extra_prep] or [(role_dir or company_dir).stat().st_mtime]
        return FsApplication(
            country=country,
            company=_display(company_dir.name),
            role=_display(role_dir.name) if role_dir else None,
            layout="nested" if role_dir else "flat",
            path=self._rel(role_dir or company_dir),
            has_jd=jd is not None,
            has_cv=has_cv,
            has_cover_letter=any(_is_cover_letter(f) for f in files),
            prep_files=prep,
            notes=_notes(jd) if jd else [],
            last_modified=datetime.fromtimestamp(max(mtimes), timezone.utc),
            stage=stage,
        )

    def _company_records(self, country: str, company_dir: Path) -> list[FsApplication]:
        direct = [f for f in company_dir.iterdir() if f.is_file() and _visible(f)]
        subdirs = [d for d in sorted(company_dir.iterdir()) if d.is_dir() and _visible(d)]
        roles = [d for d in subdirs if _is_role_dir(d)]
        extra_prep = [f for d in subdirs if d not in roles for f in _files(d) if _is_prep(f)]
        extra_prep += [f for f in direct if _is_prep(f)]

        records = [self._record(country, company_dir, list(_files(r)), r, extra_prep) for r in roles]
        if any(_is_cv(f) or _is_cover_letter(f) for f in direct) or not roles:
            if direct or extra_prep:
                records.append(self._record(country, company_dir, direct, None, extra_prep))
        return records

    def scan(self) -> list[FsApplication]:
        if not self.root.is_dir():
            raise ToolError(f"Applications folder not found: {self.root} (set APPLAI_APPLICATIONS_DIR)")
        records: list[FsApplication] = []
        for country_dir in sorted(self.root.iterdir()):
            if not (country_dir.is_dir() and _visible(country_dir)):
                continue
            name = country_dir.name.lower()
            country = COUNTRY_FOLDERS.get(name) or (name.upper() if re.fullmatch(r"[a-z]{2}", name) else None)
            if country is None:
                continue  # resumes/, certificates/, logs/...
            for company_dir in sorted(country_dir.iterdir()):
                if company_dir.is_dir() and _visible(company_dir):
                    records.extend(self._company_records(country, company_dir))
        return records

    def find(self, company: str, title: str | None = None, min_company_similarity: float = 0.85) -> list[FsMatch]:
        """Folders whose company matches, best match first, with role similarity when a title is given."""
        matches = []
        for app in self.scan():
            company_sim = similarity(company, app.company)
            if company_sim < min_company_similarity:
                continue
            title_sim = similarity(title, app.role) if title and app.role else None
            matches.append(FsMatch(application=app, company_similarity=company_sim, title_similarity=title_sim))
        return sorted(matches, key=lambda m: (m.company_similarity, m.title_similarity or 0), reverse=True)

    def read_text(self, rel_path: str, max_chars: int = 20_000) -> str:
        """Read a text file inside the root; anything resolving outside it is refused."""
        path = (self.root / rel_path).resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise ToolError(f"Not a file inside the applications folder: {rel_path}")
        return path.read_text(encoding="utf-8", errors="replace")[:max_chars]
