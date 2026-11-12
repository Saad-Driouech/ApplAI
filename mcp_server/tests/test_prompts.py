"""The scheduled task's playbook (automation/scheduled_run.md) must stay in step with the daily_run prompt."""
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.anyio

PLAYBOOK = Path(__file__).resolve().parents[2] / "automation" / "scheduled_run.md"


async def test_playbook_uses_every_tool_of_daily_run(make_client):
    async with make_client() as client:
        tools = {t.name for t in (await client.list_tools()).tools}
        prompt = await client.get_prompt("daily_run", {})
    text = prompt.messages[0].content.text
    named = {t for t in tools if re.search(rf"\b{t}\b", text)}
    playbook = PLAYBOOK.read_text()
    assert {"run_pipeline_phase", "list_jobs_to_score", "record_scores", "generate_application"} <= named
    assert sorted(t for t in named if f"`{t}`" not in playbook) == []
    assert "limit 30" in playbook and "at most 5 jobs" in playbook
