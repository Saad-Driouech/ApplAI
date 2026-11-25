"""Company watchlist: the public ATS job boards that every scrape reads."""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from applai_mcp.models import BoardCandidate, Company, Watchlist, WatchCompanyResult, WatchedCompany
from applai_mcp.state import state
from applai_mcp.tools.scoring import READ_ONLY

# Changes the watchlist only; reads public job boards to check them
WATCH = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=True)
UNWATCH = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

Ats = Literal["greenhouse", "lever", "ashby", "smartrecruiters", "personio", "recruitee", "workable"]
Slug = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$", description="Board slug from a candidate")]
CareersUrl = Annotated[str, Field(pattern=r"^https://", max_length=2000,
                                  description="The company's careers page, or the link of one of its postings")]

_NOT_FOUND = ("No public job board found. Ask the user for the careers page URL or the link of one posting and try "
              "again. Companies on Workday, SuccessFactors or their own system cannot be watched.")
_CONFIRM = ("Show the user these boards with their job counts and sample titles; guessed boards may belong to a "
            "different company with a similar name. Call watch_company again with the ats and slug they confirm.")


def _watched_step(name: str, job_count: int) -> str:
    return (f"{name}'s board ({job_count} jobs) is watched: every scrape now adds its AI/ML roles as 'new' jobs, "
            "scored with list_jobs_to_score.")


def register(server: MCPServer) -> None:
    @server.tool(annotations=WATCH)
    async def watch_company(
        ctx: Context,
        name: Company,
        careers_url: Optional[CareersUrl] = None,
        ats: Optional[Ats] = None,
        slug: Optional[Slug] = None,
    ) -> WatchCompanyResult:
        """
        Watch a company's public job board (Greenhouse, Lever, Ashby, SmartRecruiters, Personio, Recruitee or
        Workable): every scrape then adds its AI/ML roles to the pipeline. Give the careers page URL when you
        can; with only a name the board is guessed and must be confirmed. Pass ats and slug only for a board
        the user confirmed from an earlier call's candidates.
        """
        if (ats is None) != (slug is None):
            raise ToolError("Pass both ats and slug, or neither")
        s = state(ctx)
        candidates: list[BoardCandidate] = []
        if ats is None:
            candidates = await s.api.detect_board(name, careers_url)
            confident = [c for c in candidates if c.job_count and c.found_by != "guess"]
            if len(confident) != 1:
                found = any(c.job_count for c in candidates)
                return WatchCompanyResult(watched=None, candidates=candidates, next_step=_CONFIRM if found else _NOT_FOUND)
            ats, slug = confident[0].ats, confident[0].slug
        payload = {"name": name, "ats": ats, "slug": slug, "careers_url": careers_url}
        watched = await s.api.watch({k: v for k, v in payload.items() if v})
        return WatchCompanyResult(watched=watched, candidates=candidates,
                                  next_step=_watched_step(watched.company.name, watched.job_count))

    @server.tool(annotations=READ_ONLY)
    async def list_watched_companies(
        ctx: Context,
        include_inactive: Annotated[bool, Field(description="Also list unwatched companies")] = False,
    ) -> Watchlist:
        """
        Companies whose job boards every scrape reads, with the jobs found at the last read (all and AI/ML
        relevant) and the last error if the board could not be read.
        """
        companies = await state(ctx).api.watchlist(include_inactive)
        return Watchlist(count=len(companies), companies=companies)

    @server.tool(annotations=UNWATCH)
    async def unwatch_company(
        ctx: Context,
        company_id: Annotated[int, Field(ge=1, description="id from list_watched_companies")],
    ) -> WatchedCompany:
        """Stop reading a company's job board. Its jobs stay in the pipeline; watch_company re-adds it."""
        return await state(ctx).api.unwatch(company_id)
