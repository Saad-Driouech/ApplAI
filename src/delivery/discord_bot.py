"""
Discord delivery + review gate.

Sends a job application bundle (PDF CV + PDF cover letter) to a Discord
channel and waits for the user to click an Approve or Reject button.

Setup (one-time):
  1. Go to https://discord.com/developers/applications → New Application
  2. Bot tab → Add Bot → copy the Token → set DISCORD_BOT_TOKEN in .env
  3. OAuth2 → URL Generator → scopes: bot + applications.commands
     → permissions: Send Messages, Attach Files, Read Message History
  4. Open the generated URL → invite the bot to your server
  5. Copy the channel ID (right-click channel → Copy Channel ID) → DISCORD_CHANNEL_ID

Decision flow:
  - Bot posts the bundle with two buttons: ✅ Approve / ❌ Reject
  - User clicks a button → bot updates the message and records the decision
  - n8n polls `get_pending_decisions()` or listens via webhook
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from src.logger import audit, get_logger

log = get_logger(__name__)

_EMBED_COLOR_PENDING = 0x5865F2   # Discord blurple
_EMBED_COLOR_APPROVED = 0x57F287  # green
_EMBED_COLOR_REJECTED = 0xED4245  # red

_BUNDLE_EMBED = {
    "title": "📋 New Application Ready for Review",
    "color": _EMBED_COLOR_PENDING,
    "fields": [
        {"name": "Position", "value": "{title}", "inline": True},
        {"name": "Company", "value": "{company}", "inline": True},
        {"name": "Country", "value": "{country}", "inline": True},
        {"name": "Gemini Score", "value": "{score}/10", "inline": True},
        {"name": "Why it fits", "value": "{reasoning}", "inline": False},
    ],
    "footer": {"text": "ApplAI • Click a button to decide"},
}


class DiscordDelivery:
    """
    Delivers application bundles to a Discord channel with interactive buttons.

    Args:
        bot_token: Discord bot token (from Developer Portal → Bot).
        channel_id: ID of the channel to post in.
    """

    def __init__(self, bot_token: str, channel_id: str):
        self._token = bot_token
        self._channel_id = str(channel_id)
        self._base_url = "https://discord.com/api/v10"
        self._headers = {
            "Authorization": f"Bot {bot_token}",
        }
        self._http = self._build_client()

    @staticmethod
    def _build_client():
        try:
            import httpx
            return httpx.Client(timeout=30.0)
        except ImportError as exc:
            raise ImportError("httpx is not installed. Run: pip install httpx") from exc

    def send_bundle(
        self,
        app_id: str,
        job: dict,
        cv_path: Path,
        cover_letter_path: Path,
        score: float,
        reasoning: str,
    ) -> str:
        """
        Post the CV + cover letter to Discord with Approve/Reject buttons.

        Returns the Discord message ID.
        """
        embed = self._build_embed(job, score, reasoning)
        components = self._build_buttons(app_id)

        files = {
            "files[0]": (cv_path.name, cv_path.read_bytes(), "application/pdf"),
            "files[1]": (cover_letter_path.name, cover_letter_path.read_bytes(), "application/pdf"),
        }

        # Discord multipart: JSON payload + file attachments
        payload = {
            "embeds": [embed],
            "components": [components],
            "attachments": [
                {"id": 0, "filename": cv_path.name},
                {"id": 1, "filename": cover_letter_path.name},
            ],
        }

        resp = self._http.post(
            f"{self._base_url}/channels/{self._channel_id}/messages",
            headers=self._headers,
            data={"payload_json": json.dumps(payload)},
            files=files,
        )
        resp.raise_for_status()
        msg_id = str(resp.json()["id"])

        audit(
            "discord_bundle_sent",
            app_id=app_id,
            job_id=job.get("id", "?"),
            discord_msg_id=msg_id,
        )
        log.info("Bundle sent to Discord | app_id=%s msg_id=%s", app_id, msg_id)
        return msg_id

    def update_decision(
        self,
        message_id: str,
        decision: str,          # "approved" | "rejected"
        job_title: str = "",
    ) -> None:
        """
        Edit the original message to reflect the user's decision
        (replaces buttons with a status label).
        Called after the user clicks Approve or Reject.
        """
        color = _EMBED_COLOR_APPROVED if decision == "approved" else _EMBED_COLOR_REJECTED
        label = "✅ Approved — good luck!" if decision == "approved" else "❌ Rejected"

        embed = {
            "title": f"{label}",
            "description": job_title,
            "color": color,
        }

        resp = self._http.patch(
            f"{self._base_url}/channels/{self._channel_id}/messages/{message_id}",
            headers={**self._headers, "Content-Type": "application/json"},
            content=json.dumps({"embeds": [embed], "components": []}),
        )
        resp.raise_for_status()

    def send_text(self, text: str) -> str:
        """Send a plain text message. Returns message ID."""
        resp = self._http.post(
            f"{self._base_url}/channels/{self._channel_id}/messages",
            headers={**self._headers, "Content-Type": "application/json"},
            content=json.dumps({"content": text}),
        )
        resp.raise_for_status()
        return str(resp.json()["id"])

    def handle_interaction(self, interaction: dict) -> Optional[tuple[str, str]]:
        """
        Parse a Discord interaction payload (from your webhook endpoint).

        Returns (app_id, decision) if it's a button click, else None.
        Call this from your n8n webhook handler.

        Example n8n setup:
          Webhook node → receive POST → pass body to this function
          → update DB → update Discord message via update_decision()
        """
        if interaction.get("type") != 3:   # 3 = MESSAGE_COMPONENT
            return None

        custom_id = interaction.get("data", {}).get("custom_id", "")
        # custom_id format: "approve_{app_id}" or "reject_{app_id}"
        if custom_id.startswith("approve_"):
            return custom_id[len("approve_"):], "approved"
        if custom_id.startswith("reject_"):
            return custom_id[len("reject_"):], "rejected"
        return None

    def close(self) -> None:
        self._http.close()

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _build_embed(job: dict, score: float, reasoning: str) -> dict:
        return {
            "title": "📋 New Application Ready for Review",
            "color": _EMBED_COLOR_PENDING,
            "fields": [
                {"name": "Position", "value": job.get("title", "—"), "inline": True},
                {"name": "Company",  "value": job.get("company", "—"), "inline": True},
                {"name": "Country",  "value": job.get("country", "—"), "inline": True},
                {"name": "Score",    "value": f"{score:.1f} / 10", "inline": True},
                {
                    "name": "Why it fits",
                    "value": (reasoning[:500] + "…") if len(reasoning) > 500 else reasoning or "—",
                    "inline": False,
                },
            ],
            "footer": {"text": "ApplAI • Click Approve or Reject below"},
        }

    @staticmethod
    def _build_buttons(app_id: str) -> dict:
        return {
            "type": 1,   # ACTION_ROW
            "components": [
                {
                    "type": 2,              # BUTTON
                    "style": 3,             # SUCCESS (green)
                    "label": "Approve ✅",
                    "custom_id": f"approve_{app_id}",
                },
                {
                    "type": 2,
                    "style": 4,             # DANGER (red)
                    "label": "Reject ❌",
                    "custom_id": f"reject_{app_id}",
                },
            ],
        }
