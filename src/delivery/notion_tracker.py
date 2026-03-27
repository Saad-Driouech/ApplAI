"""
Notion integration for job application tracking.

Creates and updates pages in the user's Notion job tracker database.
Each job application maps to one Notion page with properties for
status, score, company, role, country, links, and decisions.
"""
from __future__ import annotations

from typing import Any, Optional

from src.logger import audit, get_logger

log = get_logger(__name__)

# Notion property names — update these if your database uses different column names
_PROPS = {
    "title": "Position",           # Title property (required)
    "company": "Company",
    "country": "Country",
    "score": "Score",
    "status": "Status",
    "source_url": "Links",
    "cv_path": "CV Path",
    "cover_letter_path": "Cover Letter Path",
    "decision": "Decision",
    "notes": "Notes",
}

_STATUS_OPTIONS = {
    "queued": "Queued",
    "generating": "Generating",
    "ready": "Ready for Review",
    "approved": "Approved",
    "submitted": "Submitted",
    "rejected": "Rejected",
    "skipped": "Skipped",
}


class NotionTracker:
    """
    Creates and updates job application records in Notion.

    Args:
        api_token: Notion integration API token.
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

    def update_status(
        self,
        page_id: str,
        status: str,
        notes: Optional[str] = None,
    ) -> None:
        """Update the status (and optionally notes) on an existing Notion page."""
        notion_status = _STATUS_OPTIONS.get(status, status.capitalize())
        props: dict[str, Any] = {
            _PROPS["status"]: {
                "select": {"name": notion_status}
            },
        }
        if notes:
            props[_PROPS["notes"]] = {
                "rich_text": [{"text": {"content": notes[:2000]}}]
            }

        self._client.pages.update(page_id=page_id, properties=props)
        log.debug("Notion page %s updated: status=%s", page_id, notion_status)

    def record_decision(
        self,
        page_id: str,
        decision: str,          # "approved" | "rejected"
    ) -> None:
        """Record the user's Discord approval/rejection decision."""
        notion_decision = "Approved ✅" if decision == "approved" else "Rejected ❌"
        status = "Approved" if decision == "approved" else "Rejected"

        self._client.pages.update(
            page_id=page_id,
            properties={
                _PROPS["decision"]: {
                    "select": {"name": notion_decision}
                },
                _PROPS["status"]: {
                    "select": {"name": status}
                },
            },
        )
        audit("notion_decision_recorded", page_id=page_id, decision=decision)

    def _build_properties(self, job: dict, application: dict) -> dict[str, Any]:
        props: dict[str, Any] = {
            # Company is the Title column (required)
            _PROPS["company"]: {
                "title": [{"text": {"content": job.get("company", "Unknown")}}]
            },
            _PROPS["title"]: {
                "rich_text": [{"text": {"content": job.get("title", "")}}]
            },
            _PROPS["country"]: {
                "rich_text": [{"text": {"content": job.get("country", "")}}]
            },
            _PROPS["status"]: {
                "select": {"name": "Ready for Review"}
            },
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
