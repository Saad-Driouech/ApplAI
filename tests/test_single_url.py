"""Tests for single job-posting URL fetching (src/scrapers/single_url.py)."""
import functools
import json
import socket

import httpx
import pytest

import src.scrapers.single_url as single_url
from src.scrapers.single_url import JobFetchError, fetch_job_posting

DESC = "Build retrieval-augmented LLM systems in Python for enterprise customers. " * 5
HOSTS = {"jobs.example.com": "93.184.216.34", "internal.example.com": "10.0.0.5",
         "localhost": "127.0.0.1", "meta.example.com": "169.254.169.254"}


def _job_ld(**overrides) -> str:
    node = {
        "@type": "JobPosting", "title": "AI Engineer", "description": f"<p>{DESC}</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
        "jobLocation": [{"address": {"addressLocality": "Berlin", "addressCountry": {"name": "de"}}}],
        "datePosted": "2026-10-01",
    }
    node.update(overrides)
    return f'<script type="application/ld+json">{json.dumps({"@graph": [{"@type": "WebPage"}, node]})}</script>'


@pytest.fixture
def serve(monkeypatch):
    """Route requests to a handler and resolve hosts from HOSTS."""
    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host not in HOSTS:
            raise socket.gaierror("unknown host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (HOSTS[host], port))]

    monkeypatch.setattr(single_url.socket, "getaddrinfo", fake_getaddrinfo)

    def install(handler):
        client = functools.partial(httpx.Client, transport=httpx.MockTransport(handler))
        monkeypatch.setattr(single_url.httpx, "Client", client)
    return install


def _html(body: str, status: int = 200, **headers):
    return lambda request: httpx.Response(status, text=f"<html><body>{body}</body></html>", headers=headers)


class TestUrlValidation:
    @pytest.mark.parametrize("url", [
        "http://jobs.example.com/1",
        "ftp://jobs.example.com/1",
        "https://user:pw@jobs.example.com/1",  # pragma: allowlist secret
        "https://localhost/admin",
        "https://internal.example.com/job",
        "https://meta.example.com/latest/meta-data",
        "https://unknown.invalid/job",
    ])
    def test_rejected_before_any_request(self, serve, url):
        serve(lambda request: pytest.fail("request should not be sent"))
        with pytest.raises(JobFetchError):
            fetch_job_posting(url)

    def test_redirect_to_private_host_is_rejected(self, serve):
        serve(_html("", status=302, location="https://internal.example.com/secret"))
        with pytest.raises(JobFetchError, match="non-public"):
            fetch_job_posting("https://jobs.example.com/1")

    def test_redirect_loop_is_capped(self, serve):
        serve(_html("", status=302, location="/again"))
        with pytest.raises(JobFetchError, match="redirects"):
            fetch_job_posting("https://jobs.example.com/1")

    def test_relative_redirect_is_followed(self, serve):
        def handler(request):
            if request.url.path == "/old":
                return httpx.Response(301, headers={"location": "/new"})
            return httpx.Response(200, text=_job_ld())
        serve(handler)
        assert fetch_job_posting("https://jobs.example.com/old")["source_url"] == "https://jobs.example.com/new"

    def test_oversized_page_is_rejected(self, serve, monkeypatch):
        monkeypatch.setattr(single_url, "MAX_BYTES", 1000)
        serve(_html("x" * 2000))
        with pytest.raises(JobFetchError, match="limit"):
            fetch_job_posting("https://jobs.example.com/1")

    def test_http_error_is_reported(self, serve):
        serve(_html("", status=404))
        with pytest.raises(JobFetchError, match="404"):
            fetch_job_posting("https://jobs.example.com/1")


class TestParsing:
    def test_json_ld_job_posting(self, serve):
        serve(_html(_job_ld()))
        job = fetch_job_posting("https://jobs.example.com/1")
        assert (job["title"], job["company"], job["city"], job["country"]) == ("AI Engineer", "Acme", "Berlin", "DE")
        assert job["description"].startswith("Build retrieval") and "<p>" not in job["description"]
        assert job["source_url"] == "https://jobs.example.com/1" and len(job["external_id"]) == 32

    def test_non_iso_country_is_dropped(self, serve):
        serve(_html(_job_ld(jobLocation={"address": {"addressCountry": "Germany"}})))
        assert fetch_job_posting("https://jobs.example.com/1")["country"] == ""

    def test_falls_back_to_page_text(self, serve):
        page = (f'<head><meta property="og:title" content="ML Engineer at Acme"></head>'
                f'<nav>Home Jobs</nav><main><h1>ML Engineer</h1><p>{DESC}</p></main><script>var x=1;</script>')
        serve(lambda request: httpx.Response(200, text=f"<html>{page}</html>"))
        job = fetch_job_posting("https://jobs.example.com/1")
        assert job["title"] == "ML Engineer at Acme" and job["company"] == ""
        assert "Home Jobs" not in job["description"] and "var x" not in job["description"]

    def test_login_wall_asks_for_pasted_text(self, serve):
        serve(_html("<main>Sign in to see this job</main>"))
        with pytest.raises(JobFetchError, match="paste the job description"):
            fetch_job_posting("https://jobs.example.com/1")


_LINKEDIN_CARD = (
    '<section class="top-card-layout"><h2 class="top-card-layout__title topcard__title">Senior AI Engineer (w/m/d)</h2>'
    '<a class="topcard__org-name-link topcard__flavor--black-link" href="#">Eraneos</a>'
    '<span class="topcard__flavor topcard__flavor--bullet">Greater Munich Metropolitan Area</span></section>'
    f'<div class="description__text description__text--rich"><div class="show-more-less-html__markup">'
    f'<p>{DESC}</p></div></div>'
)


class TestLinkedIn:
    @pytest.mark.parametrize("url,job_id", [
        ("https://www.linkedin.com/jobs/view/4448028473/", "4448028473"),
        ("https://de.linkedin.com/jobs/view/senior-ai-engineer-at-eraneos-4448028473?trk=public", "4448028473"),
        ("https://www.linkedin.com/comm/jobs/view/4448028473/?trackingId=abc%3D%3D&refId=x", "4448028473"),
        ("https://www.linkedin.com/jobs/search/?currentJobId=4448028473&keywords=ai", "4448028473"),
        ("https://www.linkedin.com/in/someone/", None),
        ("https://evil.example/www.linkedin.com/jobs/view/4448028473/", None),
        ("https://linkedin.com.evil.example/jobs/view/4448028473/", None),
    ])
    def test_job_id(self, url, job_id):
        assert single_url.linkedin_job_id(url) == job_id

    def test_alert_link_is_read_from_the_guest_endpoint(self, serve, monkeypatch):
        monkeypatch.setitem(HOSTS, "www.linkedin.com", "13.107.42.14")
        seen = []

        def handler(request):
            seen.append(str(request.url))
            return httpx.Response(200, text=f"<html><body>{_LINKEDIN_CARD}</body></html>")

        serve(handler)
        job = fetch_job_posting("https://www.linkedin.com/comm/jobs/view/4448028473/?trackingId=abc")

        assert seen == ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4448028473"]
        assert (job["title"], job["company"], job["country"]) == ("Senior AI Engineer (w/m/d)", "Eraneos", "DE")
        assert job["city"] == "Greater Munich Metropolitan Area" and job["description"].startswith("Build retrieval")
        assert job["external_id"] == "linkedin:4448028473"
        assert job["source_url"] == "https://www.linkedin.com/jobs/view/4448028473/"

    @pytest.mark.parametrize("response", [
        httpx.Response(429, text=""),
        httpx.Response(200, text="<html><body><main>Sign in to view</main></body></html>"),
    ])
    def test_rate_limit_or_login_wall_asks_for_the_employer_posting(self, serve, monkeypatch, response):
        monkeypatch.setitem(HOSTS, "www.linkedin.com", "13.107.42.14")
        calls = []
        serve(lambda request: calls.append(request) or response)
        with pytest.raises(JobFetchError, match="employer's own posting"):
            fetch_job_posting("https://www.linkedin.com/jobs/view/4448028473/")
        assert len(calls) == 1
