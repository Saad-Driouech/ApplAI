"""
Notion integration for job application tracking.

Creates and updates pages in the user's Notion job tracker database.
Each job application maps to one Notion page with properties for
status, score, company, role, country, city, links, and decisions.
"""
from __future__ import annotations

from typing import Any, Optional

from src.logger import audit, get_logger

log = get_logger(__name__)

# Notion property names — update these if your database uses different column names
_PROPS = {
    "company": "Company",           # Title column in Notion (the main row name)
    "title": "Position",            # Job title — Text column
    "country": "Country",
    "city": "City",
    "score": "Score",
    "status": "Status",             # Application lifecycle: Ready for Review → Queued → Applied → …
    "source_url": "Links",
    "cv_path": "CV Path",
    "cover_letter_path": "Cover Letter Path",
    "notes": "Notes",
}

# Maps ISO country codes to full names for display in Notion
_COUNTRY_NAMES = {
    "DE": "Germany",
    "AE": "UAE",
    "SA": "Saudi Arabia",
    "CH": "Switzerland",
    "QA": "Qatar",
    "NL": "Netherlands",
}


def _format_country(code: str) -> str:
    return _COUNTRY_NAMES.get(code.upper(), code)


def _format_city(city: str) -> str:
    if not city:
        return ""
    if "remote" in city.lower():
        return "Remote"
    return city.strip()


class NotionTracker:
    """
    Creates and updates job application records in Notion.

    Args:
        api_token:   Notion integration API token.
        database_id: ID of the job tracker database in Notion.
    """

    def __init__(self, api_token: str, database_id: str):
        self._db_id = database_id
        self._client = self._build_client(api_token)

    @staticmethod
    def _build_client(api_token: str):
        try:
            from notion_client import Client
        except ImportError as exc:
            raise ImportError(
                "notion-client is not installed. Run: pip install notion-client"
            ) from exc
        return Client(auth=api_token)

    def log_job(self, job: dict, application: dict) -> str:
        """
        Create a new page in Notion for this job application.
        Status is set to 'Ready for Review' — updated after Discord decision.

        Returns the Notion page ID.
        """
        props = self._build_properties(job, application)

        page = self._client.pages.create(
            parent={"database_id": self._db_id},
            properties=props,
        )
        page_id = page["id"]

        audit(
            "notion_page_created",
            job_id=job.get("id", "?"),
            app_id=application.get("id", "?"),
            notion_page_id=page_id,
        )
        log.info("Notion page created: %s for job %s", page_id, job.get("title"))
        return page_id

    def record_decision(self, page_id: str, decision: str) -> None:
        """
        Update Status after the user clicks Approve or Reject in Discord.

        approved → Status = "Queued"    (will apply)
        rejected → Status = "Discarded" (won't apply)
        """
        status = "Queued" if decision == "approved" else "Discarded"

        self._client.pages.update(
            page_id=page_id,
            properties={
                _PROPS["status"]: {"select": {"name": status}},
            },
        )
        audit("notion_decision_recorded", page_id=page_id, decision=decision, status=status)
        log.info("Notion page %s updated: status=%s", page_id, status)

    def update_status(self, page_id: str, status: str, notes: Optional[str] = None) -> None:
        """Manually update the Status field (e.g. Applied, Interview, Rejected)."""
        props: dict[str, Any] = {
            _PROPS["status"]: {"select": {"name": status}},
        }
        if notes:
            props[_PROPS["notes"]] = {
                "rich_text": [{"text": {"content": notes[:2000]}}]
            }
        self._client.pages.update(page_id=page_id, properties=props)
        log.debug("Notion page %s updated: status=%s", page_id, status)

    def _build_properties(self, job: dict, application: dict) -> dict[str, Any]:
        props: dict[str, Any] = {
            _PROPS["company"]: {
                "title": [{"text": {"content": job.get("company", "Unknown")}}]
            },
            _PROPS["title"]: {
                "rich_text": [{"text": {"content": job.get("title", "")}}]
            },
            _PROPS["country"]: {
                "rich_text": [{"text": {"content": _format_country(job.get("country", ""))}}]
            },
            _PROPS["status"]: {
                "select": {"name": "Ready for Review"}
            },
        }

        city = _format_city(job.get("city", "") or "")
        if city:
            props[_PROPS["city"]] = {
                "rich_text": [{"text": {"content": city}}]
            }

        score = job.get("score")
        if score is not None:
            props[_PROPS["score"]] = {"number": float(score)}

        source_url = job.get("source_url", "")
        if source_url:
            props[_PROPS["source_url"]] = {"url": source_url}

        cv_path = application.get("cv_path", "")
        if cv_path:
            props[_PROPS["cv_path"]] = {
                "rich_text": [{"text": {"content": str(cv_path)}}]
            }

        cover_letter_path = application.get("cover_letter_path", "")
        if cover_letter_path:
            props[_PROPS["cover_letter_path"]] = {
                "rich_text": [{"text": {"content": str(cover_letter_path)}}]
            }

        return props
