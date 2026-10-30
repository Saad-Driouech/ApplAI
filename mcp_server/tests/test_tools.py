"""End-to-end tool tests through the SDK's in-memory client."""
import anyio
import httpx2
import pytest
from mcp_types import ElicitResult

from conftest import JD, body, call, call_error, job, score_response

pytestmark = pytest.mark.anyio


async def test_tool_catalogue_and_annotations(make_client):
    async with make_client() as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"score_job", "check_duplicate", "list_applications", "get_stats",
                          "generate_application", "run_pipeline_phase", "get_pipeline_run"}
    for name in ("score_job", "check_duplicate", "list_applications", "get_stats", "get_pipeline_run"):
        assert tools[name].annotations.read_only_hint is True
    for name in ("generate_application", "run_pipeline_phase"):
        assert tools[name].annotations.read_only_hint is False
        assert "confirmation_token" in tools[name].input_schema["properties"]
        assert "approval" not in tools[name].input_schema["properties"]  # filled by the resolver
    assert all(t.output_schema for t in tools.values())


class TestScoreJob:
    async def test_scores_text_and_reports_existing_folders(self, make_client, fake_api):
        async with make_client() as client:
            out = await call(client, "score_job", {"jd_text": JD, "company": "Acme", "country": "de"})
        assert body(fake_api.calls("POST /jobs/score")[0]) == {"description": JD, "company": "Acme", "country": "DE"}
        assert out["result"]["score"] == 7.5
        assert [m["application"]["path"] for m in out["existing_applications"]] == ["germany/Acme"]

    @pytest.mark.parametrize("args", [{}, {"jd_text": JD, "url": "https://jobs.example.com/1"}])
    async def test_requires_exactly_one_source(self, make_client, args):
        async with make_client() as client:
            assert "exactly one" in await call_error(client, "score_job", args)

    async def test_schema_rejects_non_https_url(self, make_client, fake_api):
        async with make_client() as client:
            await call_error(client, "score_job", {"url": "http://jobs.example.com/1"})
        assert fake_api.requests == []


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
        fake_api.routes["POST /jobs/score"] = (status, {"detail": detail})
        async with make_client() as client:
            assert expected in await call_error(client, "score_job", {"jd_text": JD})

    @pytest.mark.parametrize("exc,expected", [
        (httpx2.ConnectError("refused"), "docker compose up"),
        (httpx2.ReadTimeout("slow"), "did not answer within"),
    ])
    async def test_transport_errors(self, make_client, fake_api, exc, expected):
        def fail(request):
            raise exc
        fake_api.routes["POST /jobs/score"] = fail
        async with make_client() as client:
            assert expected in await call_error(client, "score_job", {"jd_text": JD})


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


GENERATED = {"job_id": "j1", "application_id": "a1", "status": "ready", "score": 8.0,
             "cv_path": "/w/.pending/a1/cv.pdf", "cover_letter_path": "/w/.pending/a1/cover_letter.pdf"}


class TestGenerateApplicationTokenFlow:
    async def test_preview_then_confirm(self, make_client, fake_api):
        fake_api.routes["POST /jobs/generate"] = GENERATED
        args = {"jd_text": JD, "title": "AI Engineer", "company": "Acme", "country": "DE"}
        async with make_client() as client:
            first = await call(client, "generate_application", args)
            assert first["status"] == "confirmation_required"
            assert first["preview"]["details"]["Score"].startswith("7.5/10")
            assert "germany/Acme" in first["preview"]["details"]["Existing folders"]
            assert fake_api.calls("POST /jobs/generate") == []

            done = await call(client, "generate_application", {**args, "confirmation_token": first["confirmation_token"]})
        assert done["status"] == "done" and done["result"]["application_id"] == "a1"
        assert body(fake_api.calls("POST /jobs/generate")[0]) == {
            "cv_template": None, "job": {"description": JD, "title": "AI Engineer", "company": "Acme", "country": "DE"}}
        assert len(fake_api.calls("POST /jobs/score")) == 1  # the preview is not recomputed on confirm

    async def test_token_cannot_be_reused_or_moved_to_other_arguments(self, make_client, fake_api):
        fake_api.routes["GET /jobs/j1"] = job()
        fake_api.routes["POST /jobs/generate"] = GENERATED
        async with make_client() as client:
            token = (await call(client, "generate_application", {"job_id": "j1"}))["confirmation_token"]
            assert "does not match" in await call_error(
                client, "generate_application", {"job_id": "j2", "confirmation_token": token})
            await call(client, "generate_application", {"job_id": "j1", "confirmation_token": token})
            assert "already used" in await call_error(
                client, "generate_application", {"job_id": "j1", "confirmation_token": token})
        assert len(fake_api.calls("POST /jobs/generate")) == 1

    async def test_preview_rejects_pipeline_duplicate(self, make_client, fake_api):
        fake_api.routes["POST /jobs/score"] = score_response(duplicates=[job(id="j7", status="delivered")])
        async with make_client() as client:
            error = await call_error(client, "generate_application",
                                     {"jd_text": JD, "title": "AI Engineer", "company": "Acme", "country": "DE"})
        assert "job_id j7" in error
        assert fake_api.calls("POST /jobs/generate") == []

    async def test_preview_rejects_job_with_documents(self, make_client, fake_api):
        fake_api.routes["GET /jobs/j1"] = job(status="delivered")
        async with make_client() as client:
            assert "already has documents" in await call_error(client, "generate_application", {"job_id": "j1"})

    async def test_preview_requires_country(self, make_client, fake_api):
        fake_api.routes["POST /jobs/score"] = score_response(
            job={"title": "AI Engineer", "company": "Acme", "country": "", "city": "", "source_url": "",
                 "description_chars": 10})
        async with make_client() as client:
            assert "country" in await call_error(client, "generate_application", {"jd_text": JD})

    @pytest.mark.parametrize("args", [{}, {"job_id": "j1", "jd_text": JD}])
    async def test_requires_exactly_one_source(self, make_client, args):
        async with make_client() as client:
            assert "exactly one" in await call_error(client, "generate_application", args)

    async def test_schema_rejects_template_paths(self, make_client, fake_api):
        async with make_client() as client:
            await call_error(client, "generate_application", {"job_id": "j1", "cv_template": "../../etc/passwd"})
        assert fake_api.requests == []


class TestGenerateApplicationElicitation:
    @pytest.mark.parametrize("response,status,generated", [
        (ElicitResult(action="accept", content={"approve": True}), "done", 1),
        (ElicitResult(action="accept", content={"approve": False}), "declined", 0),
        (ElicitResult(action="decline"), "declined", 0),
        (ElicitResult(action="cancel"), "cancelled", 0),
    ])
    async def test_user_decides(self, make_client, fake_api, response, status, generated):
        fake_api.routes["GET /jobs/j1"] = job()
        fake_api.routes["POST /jobs/generate"] = GENERATED
        prompts = []

        async def answer(ctx, params):
            prompts.append(params.message)
            return response

        async with make_client(elicitation_callback=answer) as client:
            out = await call(client, "generate_application", {"job_id": "j1"})
        assert out["status"] == status and out["confirmation_token"] is None
        assert "Generate an application for AI Engineer at Acme" in prompts[0]
        assert len(fake_api.calls("POST /jobs/generate")) == generated

    async def test_tokens_are_ignored_when_the_client_can_elicit(self, make_client, fake_api):
        fake_api.routes["GET /jobs/j1"] = job()

        async def decline(ctx, params):
            return ElicitResult(action="decline")

        async with make_client(elicitation_callback=decline) as client:
            out = await call(client, "generate_application", {"job_id": "j1", "confirmation_token": "1.2.3"})
        assert out["status"] == "declined"
        assert fake_api.calls("POST /jobs/generate") == []


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
