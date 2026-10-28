"""
CV generator: calls the Anthropic API to tailor the LaTeX CV template,
then compiles it to PDF via pdflatex.

Pipeline per job:
  1. Read base .tex template and profile_summary.md from disk (dynamic — always latest)
  2. Call Anthropic API: system = rules + profile + CV template, user = job details
  3. Validate + compile in a sandbox via latex_safety.compile_tex()
  4. Return path to the generated PDF
"""
from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Optional

from src.logger import audit, get_logger
from src.utils.latex_safety import LatexCompileError, UnsafeLatexError, compile_tex

log = get_logger(__name__)

_CV_SYSTEM_TEMPLATE = """\
You are a professional CV editor. Tailor the LaTeX CV for a target job.

## Rules
- Keep the LaTeX structure intact; only modify text content.
- Do NOT add \\write18, \\input, \\include, \\openin, \\openout, or any file I/O commands.
- The CV MUST fit on exactly ONE page. This is a hard constraint.
- Keep ALL sections. Do not remove any section. Instead make each bullet point shorter and more concise.
- Each bullet point should be one line maximum. Cut filler words, keep only the metric or outcome.
- Emphasise experience and skills most relevant to the job description.
- Reorder bullet points within each role to put the most relevant ones first.
- Return ONLY the modified .tex source, no explanations, no markdown fences.
- NEVER fabricate experiences, skills, projects, or achievements.
- Only use information present in the CV source below.

## Writing Style (STRICT)
- NEVER use em dashes or en dashes. Use commas, periods, or semicolons instead.
- NEVER use "leverage", "spearheaded", "passionate", "drove", "synergy", "cutting-edge", "orchestrated", or similar AI cliches.
- Use direct, plain language. "Built X that did Y" is better than "Spearheaded the development of X".
- Keep bullet points factual: action verb + what + measurable result. No filler.

## Candidate Profile & Strategy
{profile}

## Base CV (LaTeX source)
{cv_tex}
"""

_CV_USER_TEMPLATE = """\
Tailor this CV for the following role. The result MUST fit on one page — \
keep all sections but write concise, single-line bullet points.

Title: {title}
Company: {company}
Country: {country}
Scoring Analysis:
{reasoning}

Job Description (first 2000 chars):
{description}
"""


class CVGenerationError(Exception):
    pass


class CVGenerator:
    """
    Generates a tailored PDF CV for a specific job via the Anthropic API.

    Args:
        template_path: Path to the base .tex CV file.
        output_dir:    Directory where per-job folders will be created.
        profile_path:  Path to profile_summary.md — read fresh on every call.
        pdflatex_bin:  Path to the pdflatex binary.
    """

    def __init__(
        self,
        template_path: Path,
        output_dir: Path,
        profile_path: Optional[Path] = None,
        pdflatex_bin: str = "pdflatex",
    ):
        self._template = template_path
        self._output_dir = output_dir
        self._profile_path = profile_path
        self._pdflatex = pdflatex_bin

        if not template_path.exists():
            raise FileNotFoundError(f"CV template not found: {template_path}")

    def generate(self, job: dict, gemini_reasoning: str = "", output_folder: Path | None = None) -> Path:
        """
        Generate a tailored PDF CV for *job*.

        Returns the path to the compiled .pdf file.
        Raises CVGenerationError on any failure.
        """
        job_folder = output_folder or self._job_folder(job)
        job_folder.mkdir(parents=True, exist_ok=True)

        tex_source = self._tailor_with_api(job, gemini_reasoning)
        tex_path = job_folder / "cv.tex"
        tex_path.write_text(tex_source, encoding="utf-8")
        self._copy_template_assets(job_folder)

        try:
            pdf_path = compile_tex(tex_path, pdflatex_bin=self._pdflatex)
        except UnsafeLatexError as exc:
            log.error("API output failed LaTeX safety check for job %s: %s", job.get("id", "?"), exc)
            raise CVGenerationError(str(exc)) from exc
        except LatexCompileError as exc:
            raise CVGenerationError(str(exc)) from exc
        audit("cv_generated", job_id=job.get("id", "?"), pdf_path=str(pdf_path))
        log.info("CV generated: %s", pdf_path)
        return pdf_path

    def _tailor_with_api(self, job: dict, reasoning: str) -> str:
        from src import claude_bridge

        data = claude_bridge.validate_input(dict(job))

        # Read both files fresh each call so updates are picked up automatically
        cv_tex = self._template.read_text(encoding="utf-8")
        profile = (
            self._profile_path.read_text(encoding="utf-8")
            if self._profile_path and self._profile_path.exists()
            else ""
        )

        system = _CV_SYSTEM_TEMPLATE.format(profile=profile, cv_tex=cv_tex)
        user = _CV_USER_TEMPLATE.format(
            title=data["title"],
            company=data["company"],
            country=data["country"],
            reasoning=(reasoning or "")[:1000],
            description=(data.get("description") or "")[:2000],
        )

        result = claude_bridge.call_api(system, user)

        if result.get("status") != "success":
            raise CVGenerationError(f"API error: {result.get('error', 'unknown')}")

        output = result.get("output", "").strip()
        if not output:
            raise CVGenerationError("API returned empty output")

        return output

    def _copy_template_assets(self, output_dir: Path) -> None:
        """
        Copy image assets from the template directory so relative \\includegraphics
        paths resolve — the compile sandbox only reads from the job folder.
        """
        template_dir = self._template.parent
        for pattern in ("*.png", "*.jpg"):
            for img in template_dir.glob(pattern):
                shutil.copy2(img, output_dir / img.name)

    _COUNTRY_FOLDERS = {
        "DE": "germany", "AE": "uae", "SA": "ksa",
        "CH": "switzerland", "NL": "netherlands", "US": "us",
        "QA": "qatar", "GB": "uk", "FR": "france",
    }

    def _job_folder(self, job: dict) -> Path:
        from src.utils.sanitize import sanitize_company_for_path
        country_code = job.get("country", "").upper()
        country_dir = self._COUNTRY_FOLDERS.get(country_code, country_code.lower() or "other")
        company = sanitize_company_for_path(job.get("company", "unknown"))
        return self._output_dir / country_dir / company
