"""
Seed the company watchlist from the application folders and a list of suggested companies.

  python -m src.scrapers.seed_watchlist                # detect boards and write a review file; changes nothing
  python -m src.scrapers.seed_watchlist --apply FILE   # watch every entry marked "watch": true in FILE

Boards guessed from a name can belong to another company, so a person reviews the file (job counts and
sample titles help) and flips "watch" before applying it. Guesses start unwatched when a company has
more than one candidate board.
"""
from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

import src.database as db
from src.config import load_paths
from src.scrapers import ats
from src.scrapers.single_url import _download
from src.utils.file_manager import COUNTRY_FOLDERS

SUGGESTIONS = Path(__file__).with_name("watchlist_suggestions.txt")


def _key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def folder_companies(working_dir: Path, folders: list[str]) -> list[dict]:
    """Companies from <working_dir>/<country>/<Company>/ folders (underscores read as spaces)."""
    found = []
    for folder in folders:
        root = working_dir / folder
        if not root.is_dir():
            continue
        for path in sorted(root.iterdir()):
            if path.is_dir() and not path.name.startswith("."):
                found.append({"name": path.name.replace("_", " ").strip(), "careers_url": None,
                              "source": f"folder:{folder}"})
    return found


def suggested_companies(path: Path = SUGGESTIONS) -> list[dict]:
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, url = (part.strip() for part in line.partition("|"))
        found.append({"name": name, "careers_url": url or None, "source": "suggestion"})
    return found


def _detect(get, company: dict) -> list[dict]:
    found = ats.detect_ats(get, company["name"], company["careers_url"], download=_download)
    candidates = [c for c in found if c.job_count]
    failed = [c for c in found if c.error]
    rows = [{**company, "watch": False, "ats": c.ats, "slug": c.slug, "found_by": c.found_by,
             "note": f"board could not be read ({c.error}); run the seed again"} for c in failed]
    if not candidates:
        return rows or [{**company, "watch": False, "ats": None, "slug": None, "note": "no public board found"}]
    confident = [c for c in candidates if c.found_by != "guess"]
    for c in candidates:
        watch = c in confident[:1] if confident else len(candidates) == 1
        rows.append({**company, "watch": watch, "ats": c.ats, "slug": c.slug, "found_by": c.found_by,
                     "job_count": c.job_count, "sample_titles": c.sample_titles})
    return rows


def unique_companies(companies: list[dict]) -> list[dict]:
    """One entry per company name, keeping a careers URL from any duplicate."""
    merged: dict[str, dict] = {}
    for company in companies:
        key = _key(company["name"])
        if key in merged:
            merged[key]["careers_url"] = merged[key]["careers_url"] or company["careers_url"]
        elif key:
            merged[key] = dict(company)
    return list(merged.values())


def build_report(companies: list[dict], watched: set[tuple[str, str]]) -> list[dict]:
    with ats.board_client() as get, ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda c: _detect(get, c), unique_companies(companies)))
    rows = [row for result in results for row in result]
    for row in rows:
        if (row["ats"], row["slug"]) in watched:
            row.update(watch=False, note="already watched")
    return rows


def apply_report(db_path: Path, rows: list[dict]) -> tuple[int, int]:
    added = reactivated = 0
    with db.get_conn(db_path) as conn:
        for row in rows:
            if not (row.get("watch") and row.get("ats") and row.get("slug")):
                continue
            ats.check_slug(row["ats"], row["slug"])
            _, new = db.add_watched_company(conn, row["name"], row["ats"], row["slug"], row.get("careers_url"))
            added, reactivated = added + new, reactivated + (not new)
    return added, reactivated


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", type=Path, metavar="FILE", help="Watch the entries marked \"watch\": true")
    parser.add_argument("--out", type=Path, help="Review file to write (default: next to the database)")
    parser.add_argument("--folders", nargs="*", default=sorted(set(COUNTRY_FOLDERS.values()) | {"remote"}),
                        help="Country folders under the working directory to read company names from")
    parser.add_argument("--no-suggestions", action="store_true", help="Only use the application folders")
    args = parser.parse_args(argv)

    paths = load_paths()
    db.init_db(paths.db_path).close()
    if args.apply:
        added, reactivated = apply_report(paths.db_path, json.loads(args.apply.read_text(encoding="utf-8")))
        print(f"Watching {added} new boards ({reactivated} already on the watchlist)")
        return

    companies = folder_companies(paths.working_dir, args.folders)
    if not args.no_suggestions:
        companies += suggested_companies()
    with db.get_conn(paths.db_path) as conn:
        watched = {(r["ats"], r["slug"]) for r in db.list_watched_companies(conn)}
    rows = build_report(companies, watched)

    out = args.out or paths.db_path.parent / "watchlist_seed.json"
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in rows:
        board = (f"{row['ats']}/{row['slug']} ({row['found_by']}, {row['job_count']} jobs)" if row.get("job_count")
                 else row["note"])
        titles = "; ".join(row.get("sample_titles", []))
        print(f"{'WATCH' if row['watch'] else '     '}  {row['name'][:28]:28}  {board:52}  {titles[:90]}")
    print(f"\n{sum(r['watch'] for r in rows)} boards marked to watch, {len(rows)} rows. Review {out}, then run "
          f"--apply {out}")


if __name__ == "__main__":
    main()
