"""End-to-end tool tests through the SDK's in-memory client."""
import anyio
import httpx2
import pytest
from mcp import Client
from mcp_types import ElicitResult

from applai_mcp.server import create_server
from conftest import (
    COMMIT, COMMITTED, DRAFT_ID, JD, body, call, call_error, candidate, draft, job, resolve_response, watched,
)

pytestmark = pytest.mark.anyio

READ_ONLY_TOOLS = {"get_scoring_context", "check_duplicate", "list_applications", "get_stats", "get_pipeline_run",
                   "get_application", "get_generation_context", "list_watched_companies"}
GATED_TOOLS = {"generate_application", "run_pipeline_phase"}
STORE_TOOLS = {"list_jobs_to_score", "record_scores", "draft_application", "watch_company", "unwatch_company"}


async def test_tool_catalogue_and_annotations(make_client):
    async with make_client() as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == READ_ONLY_TOOLS | GATED_TOOLS | STORE_TOOLS
    for name in READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is True
    for name in tools.keys() - READ_ONLY_TOOLS:
        assert tools[name].annotations.read_only_hint is False
        assert tools[name].annotations.destructive_hint is False
    for name in GATED_TOOLS:
        assert "confirmation_token" in tools[name].input_schema["properties"]
        assert "approval" not in tools[name].input_schema["properties"]  # filled by the resolver
    assert all(t.output_schema for t in tools.values())


class TestScoringContext:
    async def test_posting_text_with_rubric_and_existing_folders(self, make_client, fake_api):
        async with make_client() as client:
            out = await call(client, "get_scoring_context", {"jd_text": JD, "company": "Acme", "country": "de"})
        assert body(fake_api.calls("POST /jobs/resolve")[0]) == {"description": JD, "company": "Acme", "country": "DE"}
        assert out["posting"]["description"] == JD and out["job"] is None
        assert out["context"]["threshold"] == 6.0 and out["context"]["rubric"].startswith("## Scoring Rubric")
        assert [m["application"]["path"] for m in out["existing_applications"]] == ["germany/Acme"]
        assert "ask before writing" in out["next_step"]

    async def test_pipeline_job_uses_clean_description(self, make_client, fake_api):
        fake_api.routes["GET /jobs/j1"] = {**job(company="SAP"), "description": JD}
        async with make_client() as client:
            out = await call(client, "get_scoring_context", {"job_id": "j1"})
        assert fake_api.calls("GET /jobs/j1")[0].url.params["clean"] == "true"
        assert out["job"]["description"] == JD and out["posting"] is None
        assert out["existing_applications"][0]["application"]["company"] == "SAP"

    @pytest.mark.parametrize("args", [{}, {"jd_text": JD, "url": "https://jobs.example.com/1"},
                                      {"jd_text": JD, "job_id": "j1"}])
    async def test_requires_exactly_one_source(self, make_client, args):
        async with make_client() as client:
            assert "exactly one" in await call_error(client, "get_scoring_context", args)

    async def test_schema_rejects_non_https_url(self, make_client, fake_api):
        async with make_client() as client:
            await call_error(client, "get_scoring_context", {"url": "http://jobs.example.com/1"})
        assert fake_api.requests == []


class TestBatchScoring:
    async def test_list_runs_prefilter_first(self, make_client, fake_api):
        fake_api.routes["POST /jobs/prefilter"] = {"expired": 1, "skipped": 7, "remaining": 2}
        fake_api.routes["GET /jobs/to-score"] = {"count": 1, "jobs": [
            {"id": "j1", "title": "AI Engineer", "company": "Acme", "location": "Berlin, DE", "description": JD}]}
        async with make_client() as client:
            out = await call(client, "list_jobs_to_score", {"limit": 10})
        assert [r.url.path for r in fake_api.requests[:1]] == ["/jobs/prefilter"]
        assert fake_api.calls("GET /jobs/to-score")[0].url.params["limit"] == "10"
        assert out["prefilter"]["skipped"] == 7 and out["jobs"][0]["id"] == "j1"
        assert out["context"]["profile"] == "Profile: LLM agents"

    async def test_record_scores_counts_outcomes(self, make_client, fake_api):
        fake_api.routes["POST /jobs/scores"] = {"results": [
            {"job_id": "j1", "status": "queued"}, {"job_id": "j2", "status": "skipped", "skip_reason": "low_score"},
            {"job_id": "j3", "error": "not found"}]}
        scores = [{"job_id": f"j{i}", "score": 7.0, "reasoning": "fit"} for i in (1, 2, 3)]
        async with make_client() as client:
            out = await call(client, "record_scores", {"scores": scores})
        assert (out["queued"], out["skipped"], out["errors"]) == (1, 1, 1)
        sent = body(fake_api.calls("POST /jobs/scores")[0])["scores"][0]
        assert sent == {"job_id": "j1", "score": 7.0, "reasoning": "fit", "is_job_posting": True, "scored_by": "claude"}

    @pytest.mark.parametrize("scores", [[], [{"job_id": "j1", "score": 11, "reasoning": "x"}],
                                        [{"job_id": f"j{i}", "score": 5, "reasoning": "x"} for i in range(31)]])
    async def test_record_scores_validated(self, make_client, fake_api, scores):
        async with make_client() as client:
            await call_error(client, "record_scores", {"scores": scores})
        assert fake_api.requests == []


class TestWatchlist:
    async def test_board_found_on_the_careers_page_is_watched_directly(self, make_client, fake_api):
        fake_api.routes["POST /watchlist/detect"] = {"candidates": [candidate()]}
        fake_api.routes["POST /watchlist"] = {"company": watched(careers_url="https://helsing.ai/careers"),
                                              "added": True, "job_count": 176, "sample_titles": ["AI Research Engineer"]}
        async with make_client() as client:
            out = await call(client, "watch_company", {"name": "Helsing", "careers_url": "https://helsing.ai/careers"})
        assert body(fake_api.calls("POST /watchlist/detect")[0]) == {"name": "Helsing", "careers_url": "https://helsing.ai/careers"}
        assert body(fake_api.calls("POST /watchlist")[0]) == {
            "name": "Helsing", "ats": "greenhouse", "slug": "helsing", "careers_url": "https://helsing.ai/careers"}
        assert out["watched"]["company"]["id"] == 1 and "176 jobs" in out["next_step"]

    async def test_guessed_boards_need_confirmation(self, make_client, fake_api):
        fake_api.routes["POST /watchlist/detect"] = {"candidates": [
            candidate(ats="personio", slug="amazon", found_by="guess", job_count=3)]}
        async with make_client() as client:
            out = await call(client, "watch_company", {"name": "Amazon"})
        assert out["watched"] is None and out["candidates"][0]["slug"] == "amazon"
        assert "different company" in out["next_step"]
        assert fake_api.calls("POST /watchlist") == []

    async def test_no_board_found(self, make_client, fake_api):
        fake_api.routes["POST /watchlist/detect"] = {"candidates": []}
        async with make_client() as client:
            out = await call(client, "watch_company", {"name": "SAP"})
        assert out["watched"] is None and "careers page URL" in out["next_step"]

    async def test_confirmed_board_skips_detection(self, make_client, fake_api):
        fake_api.routes["POST /watchlist"] = {"company": watched(), "added": False, "job_count": 176, "sample_titles": []}
        async with make_client() as client:
            await call(client, "watch_company", {"name": "Helsing", "ats": "greenhouse", "slug": "helsing"})
            assert "both ats and slug" in await call_error(client, "watch_company", {"name": "Helsing", "ats": "lever"})
            await call_error(client, "watch_company", {"name": "X", "ats": "taleo", "slug": "x"})
            await call_error(client, "watch_company", {"name": "X", "ats": "personio", "slug": "evil.com/#"})
        assert fake_api.calls("POST /watchlist/detect") == [] and len(fake_api.calls("POST /watchlist")) == 1

    async def test_list_and_unwatch(self, make_client, fake_api):
        fake_api.routes["GET /watchlist"] = {"count": 1, "companies": [watched(last_job_count=176, last_relevant_count=8)]}
        fake_api.routes["DELETE /watchlist/1"] = watched(active=False)
        async with make_client() as client:
            listed = await call(client, "list_watched_companies", {"include_inactive": True})
            removed = await call(client, "unwatch_company", {"company_id": 1})
        assert listed["count"] == 1 and listed["companies"][0]["last_relevant_count"] == 8
        assert fake_api.calls("GET /watchlist")[0].url.params["include_inactive"] == "true"
        assert removed["active"] is False


class TestCheckDuplicate:
    async def test_pipeline_and_folder_matches(self, make_client, fake_api):
        fake_api.routes["GET /jobs/duplicates"] = {"dedup_key": "sap|x", "matches": [job(company="SAP")]}
        async with make_client() as client:
            out = await call(client, "check_duplicate", {"company": "SAP", "title": "Forward Deployed AI Engineer"})
        assert out["is_duplicate"] is True
        assert "pipeline has 1" in out["summary"] and "similar role" in out["summary"]

    async def test_same_company_other_role_is_not_a_duplicate(self, make_client):
        async with make_client() as client:
            out = await call(client, "check_duplicate", {"company": "SAP", "title": "Data Analyst"})
        assert out["is_duplicate"] is False and "different" in out["summary"]


class TestListApplications:
    async def test_merges_approved_job_with_flat_folder(self, make_client, fake_api):
        approved = job(status="approved", application_id="a1", decision_at="2026-10-21T09:00:00+00:00")

        def jobs(request):
            matches = [approved] if request.url.params["status"] == "approved" else []
            return {"count": len(matches), "jobs": matches}
        fake_api.routes["GET /jobs"] = jobs
        async with make_client() as client:
            out = await call(client, "list_applications", {"country": "de"})

        assert {r.url.params["status"] for r in fake_api.calls("GET /jobs")} == {"ready", "delivered", "approved",
                                                                                "rejected"}
        acme = next(r for r in out["applications"] if r["company"] == "Acme")
        assert (acme["source"], acme["job_id"], acme["path"], acme["status"]) == ("both", "j1", "germany/Acme",
                                                                                   "approved")
        assert all(r["country"] == "DE" for r in out["applications"])
        assert out["count"] == 3

    async def test_status_filter_skips_folders(self, make_client, fake_api):
        fake_api.routes["GET /jobs"] = {"count": 1, "jobs": [job()]}
        async with make_client() as client:
            out = await call(client, "list_applications", {"status": "queued"})
        assert [r["source"] for r in out["applications"]] == ["pipeline"]

    async def test_stage_filter_skips_pipeline(self, make_client, fake_api):
        async with make_client() as client:
            out = await call(client, "list_applications", {"stage": "interviewing"})
        assert [r["path"] for r in out["applications"]] == ["germany/Lio/Forward_Deployed_Engineer"]
        assert fake_api.calls("GET /jobs") == []

    async def test_limit_reports_truncation(self, make_client):
        async with make_client() as client:
            out = await call(client, "list_applications", {"source": "filesystem", "limit": 2})
        assert out["count"] == 2 and out["truncated"] is True


async def test_get_stats_combines_sources(make_client):
    async with make_client() as client:
        out = await call(client, "get_stats")
    assert out["pipeline_status_counts"]["new"] == 4
    assert out["approval_rate"] == 0.5 and out["recommended_threshold"] == 6.5
    assert out["filesystem_by_stage"] == {"prepared": 2, "interviewing": 1, "researching": 1}
    assert out["filesystem_by_country"][0] == {"country": "DE", "applications": 3, "interviewing": 1}
    assert out["active_last_7_days"] == 4


class TestApiErrors:
    @pytest.mark.parametrize("status,detail,expected", [
        (401, "Invalid or missing API key", "APPLAI_API_KEY"),
        (409, {"message": "This job already exists", "existing_job_id": "j9"}, "existing job_id: j9"),
        (422, [{"loc": ["body", "url"], "msg": "bad url"}], "url: bad url"),
        (502, "Scoring failed: quota", "ApplAI error 502: Scoring failed: quota"),
    ])
    async def test_http_errors_become_actionable_tool_errors(self, make_client, fake_api, status, detail, expected):
        fake_api.routes["POST /jobs/resolve"] = (status, {"detail": detail})
        async with make_client() as client:
            assert expected in await call_error(client, "get_scoring_context", {"jd_text": JD})

    @pytest.mark.parametrize("exc,expected", [
        (httpx2.ConnectError("refused"), "docker compose up"),
        (httpx2.ReadTimeout("slow"), "did not answer within"),
    ])
    async def test_transport_errors(self, make_client, fake_api, exc, expected):
        def fail(request):
            raise exc
        fake_api.routes["POST /jobs/resolve"] = fail
        async with make_client() as client:
            assert expected in await call_error(client, "get_scoring_context", {"jd_text": JD})


class TestApiDownFallsBackToFolders:
    @pytest.fixture(autouse=True)
    def api_down(self, fake_api):
        def refuse(request):
            raise httpx2.ConnectError("refused")
        for route in ("GET /stats", "GET /feedback", "GET /jobs", "GET /jobs/duplicates"):
            fake_api.routes[route] = refuse

    async def test_list_applications(self, make_client):
        async with make_client() as client:
            out = await call(client, "list_applications")
        assert out["count"] == 4 and len(out["warnings"]) == 1
        assert "docker compose up" in out["warnings"][0]

    async def test_check_duplicate(self, make_client):
        async with make_client() as client:
            out = await call(client, "check_duplicate", {"company": "SAP", "title": "Forward Deployed AI Engineer"})
        assert out["is_duplicate"] is True and out["pipeline_matches"] == [] and len(out["warnings"]) == 1

    async def test_get_stats(self, make_client):
        async with make_client() as client:
            out = await call(client, "get_stats")
        assert out["pipeline_status_counts"] is None and out["approval_rate"] is None
        assert out["filesystem_by_stage"]["prepared"] == 2 and len(out["warnings"]) == 1


CV_TEX = "\\documentclass{article}\\begin{document}" + "Tailored CV. " * 20 + "\\end{document}"
BODY = "First paragraph about the role. " * 10 + "\n\n" + "Second paragraph. " * 5


@pytest.fixture
def draft_routes(fake_api):
    fake_api.routes[f"GET /drafts/{DRAFT_ID}"] = draft()
    fake_api.routes[COMMIT] = COMMITTED


async def test_generation_context(make_client, fake_api):
    fake_api.routes["GET /generation/context"] = {
        "job": {**job(), "description": JD}, "cv_template": "1page", "cv_template_tex": "\\documentclass...",
        "cv_rules": "## Rules", "cover_letter_rules": "## Cover Letter Rules", "profile": "Profile"}
    async with make_client() as client:
        out = await call(client, "get_generation_context", {"job_id": "j1", "cv_template": "1page"})
    assert dict(fake_api.calls("GET /generation/context")[0].url.params) == {"job_id": "j1", "cv_template": "1page"}
    assert out["cv_rules"] == "## Rules" and out["job"]["id"] == "j1" and "draft_application" in out["next_step"]


class TestDraftApplication:
    async def test_existing_job(self, make_client, fake_api):
        fake_api.routes["POST /drafts"] = draft()
        async with make_client() as client:
            out = await call(client, "draft_application", {"job_id": "j1", "cv_tex": CV_TEX, "cover_letter_body": BODY})
        assert body(fake_api.calls("POST /drafts")[0]) == {
            "job_id": "j1", "cv_tex": CV_TEX, "cover_letter_body": BODY, "cv_template": None, "score": None}
        assert out["draft"]["draft_id"] == DRAFT_ID and "generate_application" in out["next_step"]

    async def test_new_posting_with_score(self, make_client, fake_api):
        fake_api.routes["POST /drafts"] = draft(job_id=None)
        args = {"jd_text": JD, "title": "AI Engineer", "company": "Acme", "country": "de", "cv_tex": CV_TEX,
                "cover_letter_body": BODY, "score": {"score": 7.5, "reasoning": "fit", "scored_by": "claude-opus"}}
        async with make_client() as client:
            await call(client, "draft_application", args)
        sent = body(fake_api.calls("POST /drafts")[0])
        assert sent["job"] == {"description": JD, "title": "AI Engineer", "company": "Acme", "country": "DE"}
        assert sent["score"] == {"score": 7.5, "reasoning": "fit", "is_job_posting": True, "scored_by": "claude-opus"}

    async def test_warnings_ask_for_a_fix(self, make_client, fake_api):
        fake_api.routes["POST /drafts"] = draft(cv_pages=2, warnings=["CV is 2 pages; it must fit on one page"])
        async with make_client() as client:
            out = await call(client, "draft_application", {"job_id": "j1", "cv_tex": CV_TEX, "cover_letter_body": BODY})
        assert out["next_step"].startswith("Fix these problems") and "2 pages" in out["next_step"]

    async def test_render_error_is_actionable(self, make_client, fake_api):
        detail = "Rendering failed: pdflatex exited with 1:\n" + "x" * 900 + "\n! Undefined control sequence."
        fake_api.routes["POST /drafts"] = (422, {"detail": detail})
        async with make_client() as client:
            error = await call_error(client, "draft_application",
                                     {"job_id": "j1", "cv_tex": CV_TEX, "cover_letter_body": BODY})
        assert "Undefined control sequence" in error

    @pytest.mark.parametrize("args", [{"job_id": "j1", "jd_text": JD}, {}])
    async def test_requires_exactly_one_source(self, make_client, args):
        async with make_client() as client:
            assert "exactly one" in await call_error(
                client, "draft_application", {**args, "cv_tex": CV_TEX, "cover_letter_body": BODY})

    @pytest.mark.parametrize("args", [{"cv_template": "../../etc/passwd"}, {"cv_tex": "too short"}])
    async def test_schema_rejects_bad_input(self, make_client, fake_api, args):
        async with make_client() as client:
            await call_error(client, "draft_application",
                             {"job_id": "j1", "cv_tex": CV_TEX, "cover_letter_body": BODY, **args})
        assert fake_api.requests == []


@pytest.mark.usefixtures("draft_routes")
class TestGenerateApplicationTokenFlow:
    async def test_preview_then_confirm(self, make_client, fake_api):
        async with make_client() as client:
            first = await call(client, "generate_application", {"draft_id": DRAFT_ID})
            assert first["status"] == "confirmation_required"
            details = first["preview"]["details"]
            assert details["Score"] == "8.0/10" and details["Pages"] == "CV 1 (limit 1), cover letter 1"
            assert "germany/Acme" in details["Existing folders"]
            assert fake_api.calls(COMMIT) == []

            done = await call(client, "generate_application",
                              {"draft_id": DRAFT_ID, "confirmation_token": first["confirmation_token"]})
        assert done["status"] == "done" and done["result"]["application_id"] == DRAFT_ID
        assert done["message"] == ("Application for job j1 sent to Discord for your approval. "
                                   "Approving it moves the files into job_applications/germany/Aleph_Alpha/.")
        assert "Post it to Discord" in first["preview"]["side_effects"][-1]
        assert len(fake_api.calls(COMMIT)) == 1
        assert len(fake_api.calls(f"GET /drafts/{DRAFT_ID}")) == 1  # the preview is not recomputed on confirm

    async def test_delivery_error_is_reported(self, make_client, fake_api):
        fake_api.routes[COMMIT] = {**COMMITTED, "status": "ready",
                                   "delivery_error": "Another delivery is running; the next 'deliver' run sends it"}
        async with make_client() as client:
            token = (await call(client, "generate_application", {"draft_id": DRAFT_ID}))["confirmation_token"]
            done = await call(client, "generate_application", {"draft_id": DRAFT_ID, "confirmation_token": token})
        assert "not sent yet: Another delivery is running" in done["message"]
        assert "run_pipeline_phase('deliver')" in done["message"]

    async def test_new_job_and_warnings_shown_in_preview(self, make_client, fake_api):
        fake_api.routes[f"GET /drafts/{DRAFT_ID}"] = draft(job_id=None, cv_pages=2, warnings=["CV is 2 pages"])
        async with make_client() as client:
            preview = (await call(client, "generate_application", {"draft_id": DRAFT_ID}))["preview"]
        assert preview["details"]["Warnings"] == "CV is 2 pages"
        assert "source 'mcp'" in preview["side_effects"][0]

    async def test_token_cannot_be_reused_or_moved_to_another_draft(self, make_client, fake_api):
        other = DRAFT_ID.replace("0b6c", "1b6c")
        async with make_client() as client:
            token = (await call(client, "generate_application", {"draft_id": DRAFT_ID}))["confirmation_token"]
            assert "does not match" in await call_error(
                client, "generate_application", {"draft_id": other, "confirmation_token": token})
            await call(client, "generate_application", {"draft_id": DRAFT_ID, "confirmation_token": token})
            assert "already used" in await call_error(
                client, "generate_application", {"draft_id": DRAFT_ID, "confirmation_token": token})
        assert len(fake_api.calls(COMMIT)) == 1

    async def test_committed_draft_rejected_at_preview(self, make_client, fake_api):
        fake_api.routes[f"GET /drafts/{DRAFT_ID}"] = draft(committed=True)
        async with make_client() as client:
            assert "already used" in await call_error(client, "generate_application", {"draft_id": DRAFT_ID})

    async def test_schema_rejects_malformed_draft_id(self, make_client, fake_api):
        async with make_client() as client:
            await call_error(client, "generate_application", {"draft_id": "../../etc"})
        assert fake_api.requests == []


@pytest.mark.usefixtures("draft_routes")
class TestGenerateApplicationElicitation:
    @pytest.mark.parametrize("response,status,committed", [
        (ElicitResult(action="accept", content={"approve": True}), "done", 1),
        (ElicitResult(action="accept", content={"approve": False}), "declined", 0),
        (ElicitResult(action="decline"), "declined", 0),
        (ElicitResult(action="cancel"), "cancelled", 0),
    ])
    async def test_user_decides(self, make_client, fake_api, response, status, committed):
        prompts = []

        async def answer(ctx, params):
            prompts.append(params.message)
            return response

        async with make_client(elicitation_callback=answer) as client:
            out = await call(client, "generate_application", {"draft_id": DRAFT_ID})
        assert out["status"] == status and out["confirmation_token"] is None
        assert "Generate an application for AI Engineer at Acme" in prompts[0]
        assert len(fake_api.calls(COMMIT)) == committed

    async def test_tokens_are_ignored_when_the_client_can_elicit(self, make_client, fake_api):
        async def decline(ctx, params):
            return ElicitResult(action="decline")

        async with make_client(elicitation_callback=decline) as client:
            out = await call(client, "generate_application", {"draft_id": DRAFT_ID, "confirmation_token": "1.2.3"})
        assert out["status"] == "declined"
        assert fake_api.calls(COMMIT) == []

    async def test_token_mode_overrides_elicitation(self, settings, fake_api):
        async def never(ctx, params):
            raise AssertionError("token mode must not elicit")

        server = create_server(settings.model_copy(update={"confirmation_mode": "token"}),
                               transport=httpx2.MockTransport(fake_api.handler))
        async with Client(server, elicitation_callback=never) as client:
            first = await call(client, "generate_application", {"draft_id": DRAFT_ID})
            assert first["status"] == "confirmation_required"
            done = await call(client, "generate_application",
                              {"draft_id": DRAFT_ID, "confirmation_token": first["confirmation_token"]})
        assert done["status"] == "done" and len(fake_api.calls(COMMIT)) == 1


async def _wait_for_run(client, run_id):
    with anyio.fail_after(2):
        while True:
            run = (await call(client, "get_pipeline_run", {"run_id": run_id}))["runs"][0]
            if run["status"] != "running":
                return run
            await anyio.sleep(0.01)


class TestPipelinePhases:
    async def test_confirmed_phase_runs_in_background(self, make_client, fake_api):
        fake_api.routes["POST /score"] = {"scored": 3, "queued": 1}
        async with make_client() as client:
            preview = await call(client, "run_pipeline_phase", {"phase": "score"})
            assert preview["preview"]["details"] == {"Jobs currently 'new'": 4}
            started = await call(client, "run_pipeline_phase",
                                 {"phase": "score", "confirmation_token": preview["confirmation_token"]})
            assert started["status"] == "done" and started["result"]["status"] == "running"
            run = await _wait_for_run(client, started["result"]["run_id"])
            assert (run["status"], run["result"]) == ("succeeded", {"scored": 3, "queued": 1})
            assert len((await call(client, "get_pipeline_run"))["runs"]) == 1

    async def test_busy_phase_fails_with_server_message(self, make_client, fake_api):
        fake_api.routes["POST /deliver"] = (409, {"detail": "Pipeline phase 'deliver' is already running"})
        async with make_client() as client:
            token = (await call(client, "run_pipeline_phase", {"phase": "deliver"}))["confirmation_token"]
            started = await call(client, "run_pipeline_phase", {"phase": "deliver", "confirmation_token": token})
            run = await _wait_for_run(client, started["result"]["run_id"])
        assert run["status"] == "failed" and "already running" in run["error"]

    async def test_second_start_while_running_is_refused(self, make_client, fake_api):
        release = anyio.Event()

        async def slow(request):
            await release.wait()
            return httpx2.Response(200, json={"new_jobs": 0})
        fake_api.routes["POST /scrape"] = slow
        async with make_client() as client:
            for attempt in range(2):
                token = (await call(client, "run_pipeline_phase", {"phase": "scrape"}))["confirmation_token"]
                args = {"phase": "scrape", "confirmation_token": token}
                if attempt == 0:
                    run_id = (await call(client, "run_pipeline_phase", args))["result"]["run_id"]
                else:
                    assert run_id in await call_error(client, "run_pipeline_phase", args)
            release.set()
            assert (await _wait_for_run(client, run_id))["status"] == "succeeded"

    async def test_unknown_phase_and_run(self, make_client):
        async with make_client() as client:
            await call_error(client, "run_pipeline_phase", {"phase": "drop_tables"})
            assert "Unknown run_id" in await call_error(client, "get_pipeline_run", {"run_id": "nope"})
