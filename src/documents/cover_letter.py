"""
Cover letter generator: calls the Anthropic API to produce tailored body text,
fills in the LaTeX template with known fields, and compiles to PDF via pdflatex.

Pipeline per job:
  1. Read base .tex template and profile_summary.md from disk (dynamic — always latest)
  2. Call Anthropic API: system = rules + profile, user = job details
  3. Fill template placeholders programmatically (sender, recipient, date, body)
  4. Validate output via latex_safety.validate_tex_file()
  5. Compile with pdflatex (--no-shell-escape)
  6. Return path to the generated PDF

Output: {job_folder}/cover_letter.pdf
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.logger import audit, get_logger
from src.utils.sanitize import sanitize_company_for_path

log = get_logger(__name__)

_CL_SYSTEM_TEMPLATE = """\
You are an expert cover letter writer for the candidate described below.
Never fabricate qualifications or experiences not present in the profile.

## Candidate Profile
{profile}

## Cover Letter Rules
- Write exactly 4 paragraphs plus a brief closing line.
- Paragraph 1: Opening hook, why this company/role excites you.
- Paragraph 2: Academic and technical background with specific metrics.
- Paragraph 3: Professional experience and relevant achievements.
- Paragraph 4: Company fit and what you bring to the team.
- Closing: One sentence thanking them and expressing interest in discussing further.
- Total length: 250-400 words.
- Tone: professional, confident, concise. Write like a real person, not a marketing brochure.
- Use specific metrics and achievements from the profile where relevant.
- The output will be inserted into a LaTeX document.
- Escape LaTeX special characters: use \\% for %, \\& for &, \\# for #, \\$ for $, \\_ for _.
- Do NOT include any salutation (e.g. "Dear..."), subject line, sign-off, or name.
- Return ONLY the body paragraphs separated by blank lines, no markdown, no LaTeX commands.

## Writing Style (STRICT)
- NEVER use em dashes or en dashes. Use commas, periods, or semicolons instead.
- NEVER use "leverage", "spearheaded", "passionate", "thrilled", "excited to", "I am writing to express", "dynamic", "synergy", "cutting-edge", or similar AI cliches.
- Avoid starting sentences with "I am" repeatedly. Vary sentence structure.
- Do not use filler phrases like "I believe", "I am confident that", "I would love to".
- Write in a direct, natural tone. Short sentences are fine. Be specific, not generic.
"""

_CL_USER_TEMPLATE = """\
Write cover letter body paragraphs for this role:

Title: {title}
Company: {company}
Country: {country}
Scoring Analysis:
{reasoning}

Job Description (first 2000 chars):
{description}
"""


def _escape_latex(text: str) -> str:
    """Escape characters that are special in LaTeX, preserving already-escaped ones."""
    # Don't double-escape: only escape chars not preceded by backslash
    for char in ['&', '%', '$', '#', '_']:
        text = re.sub(r'(?<!\\)' + re.escape(char), '\\' + char, text)
    return text


class CoverLetterError(Exception):
    pass


_COUNTRY_NAMES = {
    "DE": "Germany", "AE": "UAE", "SA": "Saudi Arabia",
    "CH": "Switzerland", "QA": "Qatar", "NL": "Netherlands",
    "US": "United States", "GB": "United Kingdom", "FR": "France",
}


class CoverLetterGenerator:
    """
    Generates a tailored PDF cover letter for a specific job.

    Args:
        template_path:     Path to the base .tex cover letter template.
        output_dir:        Base directory where job folders are created.
        profile_path:      Path to profile_summary.md — read fresh on every call.
        candidate_name:    Used in sender info and sign-off.
        candidate_email:   Included in the sender header.
        candidate_address: Street address line (e.g. "Musterstr. 1, 90402 Nuremberg").
        candidate_phone:   Phone number.
        candidate_city:    City the letter is sent from (e.g. "Nuremberg").
        pdflatex_bin:      Path to the pdflatex binary.
    """

    def __init__(
        self,
        template_path: Path,
        output_dir: Path,
        profile_path: Optional[Path],
        candidate_name: str,
        candidate_email: str,
        candidate_address: str = "",
        candidate_phone: str = "",
        candidate_city: str = "",
        pdflatex_bin: str = "pdflatex",
    ):
        self._template = template_path
        self._output_dir = output_dir
        self._profile_path = profile_path
        self._candidate_name = candidate_name
        self._candidate_email = candidate_email
        self._candidate_address = candidate_address
        self._candidate_phone = candidate_phone
        self._candidate_city = candidate_city
        self._pdflatex = pdflatex_bin

        if not template_path.exists():
            raise FileNotFoundError(f"Cover letter template not found: {template_path}")

    def generate(self, job: dict, gemini_reasoning: str = "", output_folder: Path | None = None) -> Path:
        """
        Generate a tailored PDF cover letter for *job*.

        Returns the path to the compiled .pdf file.
        Raises CoverLetterError on failure.
        """
        body_text = self._generate_body(job, gemini_reasoning)
        tex_source = self._fill_template(body_text, job)

        job_folder = output_folder or self._job_folder(job)
        job_folder.mkdir(parents=True, exist_ok=True)

        tex_path = job_folder / "cover_letter.tex"
        tex_path.write_text(tex_source, encoding="utf-8")

        from src.utils.latex_safety import validate_tex_file
        validation = validate_tex_file(str(tex_path))
        if not validation["safe"]:
            violations = validation["violations"]
            log.error(
                "Cover letter failed LaTeX safety check for job %s: %s",
                job.get("id", "?"), violations,
            )
            raise CoverLetterError(f"LaTeX safety violations: {violations}")

        pdf_path = self._compile(tex_path, job_folder)
        audit("cover_letter_generated", job_id=job.get("id", "?"), pdf_path=str(pdf_path))
        log.info("Cover letter generated: %s", pdf_path)
        return pdf_path

    def _generate_body(self, job: dict, reasoning: str) -> str:
        from src import claude_bridge

        data = claude_bridge.validate_input(dict(job))

        profile = (
            self._profile_path.read_text(encoding="utf-8")
            if self._profile_path and self._profile_path.exists()
            else ""
        )
        if not profile:
            log.warning(
                "profile_summary.md not found at %s — cover letter will lack candidate context",
                self._profile_path,
            )

        system = _CL_SYSTEM_TEMPLATE.format(profile=profile)
        user = _CL_USER_TEMPLATE.format(
            title=data["title"],
            company=data["company"],
            country=data["country"],
            reasoning=reasoning[:1000],
            description=(data.get("description") or "")[:2000],
        )

        result = claude_bridge.call_api(system, user, max_tokens=1024)

        if result.get("status") != "success":
            raise CoverLetterError(f"API error: {result.get('error', 'unknown')}")

        output = result.get("output", "").strip()
        if not output:
            raise CoverLetterError("API returned empty cover letter body")

        return output

    def _fill_template(self, body_text: str, job: dict) -> str:
        """Fill the LaTeX template with known fields and LLM-generated body."""
        tex = self._template.read_text(encoding="utf-8")

        company = job.get("company", "Company")
        title = job.get("title", "Position")
        country = job.get("country", "")
        city = job.get("city", "")

        country_name = _COUNTRY_NAMES.get(country, country)
        # Build recipient city line from job city + country
        recipient_city = ""
        if city and city.lower() != "remote":
            recipient_city = f"{city}, {country_name}" if country_name else city
        elif country_name:
            recipient_city = country_name

        # Sender info
        tex = tex.replace("SENDER-NAME", _escape_latex(self._candidate_name))
        tex = tex.replace("SENDER-EMAIL", _escape_latex(self._candidate_email))
        tex = tex.replace("SENDER-ADDRESS", _escape_latex(self._candidate_address))
        tex = tex.replace("SENDER-PHONE", _escape_latex(self._candidate_phone))
        tex = tex.replace("SENDER-CITY", _escape_latex(self._candidate_city))

        # Recipient info
        tex = tex.replace("RECIPIENT-COMPANY", _escape_latex(company))
        tex = tex.replace("RECIPIENT-NAME", "")
        tex = tex.replace("RECIPIENT-STREET", "")
        tex = tex.replace("RECIPIENT-POSTCODE-CITY", _escape_latex(recipient_city))
        tex = tex.replace("RECIPIENT-COUNTRY", "")

        # Date
        tex = tex.replace("LETTER-DATE", datetime.now(timezone.utc).strftime("%d %B %Y"))

        # Subject
        tex = tex.replace("SUBJECT-LINE", f"Application as {_escape_latex(title)}")

        # Salutation
        tex = tex.replace("SALUTATION", "Dear Hiring Team,")

        # Body — split LLM output into paragraphs and fill slots
        paragraphs = [p.strip() for p in body_text.split("\n\n") if p.strip()]

        # Map paragraphs to placeholder slots
        placeholders = [
            "BODY-PARAGRAPH-1",
            "BODY-PARAGRAPH-2",
            "BODY-PARAGRAPH-3",
            "BODY-PARAGRAPH-4",
            "CLOSING-PARAGRAPH",
        ]
        for i, placeholder in enumerate(placeholders):
            if i < len(paragraphs):
                tex = tex.replace(placeholder, paragraphs[i])
            else:
                tex = tex.replace(placeholder, "")

        # Closing
        tex = tex.replace("CLOSING-LINE", "Kind regards,")

        # Clean up empty lines from unfilled optional fields
        tex = re.sub(r'\n\s*\\\\\s*\n', '\n', tex)

        return tex

    def _compile(self, tex_path: Path, output_dir: Path) -> Path:
        """Run pdflatex twice (for cross-references) and return the PDF path."""
        if not shutil.which(self._pdflatex):
            raise CoverLetterError(
                f"pdflatex not found at '{self._pdflatex}'. "
                "Install texlive-latex-base or equivalent."
            )

        cmd = [
            self._pdflatex,
            "--no-shell-escape",
            "-interaction=nonstopmode",
            "-output-directory", str(output_dir),
            str(tex_path),
        ]

        for pass_num in (1, 2):
            log.debug("pdflatex pass %d: %s", pass_num, tex_path.name)
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if proc.returncode != 0:
                output_tail = (proc.stdout or proc.stderr or "")[-3000:]
                log.warning("pdflatex pass %d exited with %d:\n%s", pass_num, proc.returncode, output_tail)

        pdf_path = output_dir / "cover_letter.pdf"
        if not pdf_path.exists():
            raise CoverLetterError("pdflatex failed — cover_letter.pdf not produced")

        return pdf_path

    _COUNTRY_FOLDERS = {
        "DE": "germany", "AE": "uae", "SA": "ksa",
        "CH": "switzerland", "NL": "netherlands", "US": "us",
        "QA": "qatar", "GB": "uk", "FR": "france",
    }

    def _job_folder(self, job: dict) -> Path:
        country_code = job.get("country", "").upper()
        country_dir = self._COUNTRY_FOLDERS.get(country_code, country_code.lower() or "other")
        company = sanitize_company_for_path(job.get("company", "unknown"))
        return self._output_dir / country_dir / company
