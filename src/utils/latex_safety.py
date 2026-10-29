"""
LaTeX safety: validation, escaping, and sandboxed compilation.

Two independent layers: validate_tex_file() rejects file-I/O and code-execution
primitives; compile_tex() runs pdflatex confined to its folder (paranoid
kpathsea access) with no secrets in its environment. Always compile through
compile_tex().
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

from src.logger import get_logger

log = get_logger(__name__)

# Control words end at a non-letter; keeps \includegraphics distinct from \include
_CMD_END = r"(?![A-Za-z])"

DANGEROUS_PATTERNS = [
    (r"\\write18", "shell escape command execution"),
    (r"\\immediate" + _CMD_END, "immediate write (can execute commands)"),
    (
        r"\\(input|include|InputIfFileExists|IfFileExists|verbatiminput"
        r"|lstinputlisting|includepdf|openin|openout|read|readline"
        r"|newread|newwrite|closein|closeout)" + _CMD_END,
        "file I/O primitive",
    ),
    (r"\\pdf(filedump|filesize|filemoddate|mdfivesum)" + _CMD_END, "file inspection primitive"),
    (r"\\(directlua|luaexec)" + _CMD_END, "Lua code execution"),
    (r"\\catcode" + _CMD_END, "category code manipulation"),
    (r"\\csname" + _CMD_END, "dynamic command construction"),
    (r"\^\^", "TeX character-code escape (can hide commands)"),
    (r"\\special\s*\{[^}]*\|", "pipe in special command"),
    (r"\\url\{[^}]*\|", "pipe in url command"),
]

_COMPILED_PATTERNS = [(re.compile(p), desc) for p, desc in DANGEROUS_PATTERNS]


class UnsafeLatexError(ValueError):
    """The .tex source contains a forbidden primitive and must not be compiled."""


class LatexCompileError(RuntimeError):
    """pdflatex is unavailable or did not produce a PDF."""


def validate_tex_file(filepath: str) -> dict:
    """
    Check a .tex file for dangerous LaTeX primitives.

    Returns:
        {"safe": bool, "violations": list[str]}
        If safe=False, do NOT compile the file.
    """
    try:
        content = Path(filepath).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return {"safe": False, "violations": [f"File not found: {filepath}"]}

    violations = [
        f"{description}: matched '{pattern.pattern}'"
        for pattern, description in _COMPILED_PATTERNS
        if pattern.search(content)
    ]
    return {"safe": len(violations) == 0, "violations": violations}


_LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "%": r"\%",
    "_": r"\_",
    "^": r"\textasciicircum{}",
    "~": r"\textasciitilde{}",
}


def escape_latex(text: str) -> str:
    """Escape plain text for insertion into a LaTeX document (single pass, no double-escaping)."""
    return "".join(_LATEX_SPECIALS.get(c, c) for c in text)


def _sandbox_env() -> dict[str, str]:
    """Minimal environment for pdflatex: no secrets, paranoid kpathsea file access."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "openin_any": "p",
        "openout_any": "p",
        "shell_escape": "f",
    }
    for name in ("HOME", "LANG", "LC_ALL"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def compile_tex(tex_path: Path, pdflatex_bin: str = "pdflatex", timeout: int = 60) -> Path:
    """
    Validate, then compile *tex_path* to PDF inside its own folder.

    Raises UnsafeLatexError, LatexCompileError, or subprocess.TimeoutExpired.
    """
    check = validate_tex_file(str(tex_path))
    if not check["safe"]:
        raise UnsafeLatexError(f"LaTeX safety violations: {check['violations']}")

    if not shutil.which(pdflatex_bin):
        raise LatexCompileError(
            f"pdflatex not found at '{pdflatex_bin}'. Install texlive-latex-base or equivalent."
        )

    workdir = tex_path.parent
    cmd = [pdflatex_bin, "-no-shell-escape", "-interaction=nonstopmode", tex_path.name]
    env = _sandbox_env()

    # Two passes for cross-references; LaTeX errors on the final pass are fatal
    for pass_num in (1, 2):
        log.debug("pdflatex pass %d: %s", pass_num, tex_path.name)
        proc = subprocess.run(
            cmd, cwd=workdir, env=env, capture_output=True, text=True, timeout=timeout,
        )
    if proc.returncode != 0:
        output_tail = (proc.stdout or proc.stderr or "")[-1500:]
        raise LatexCompileError(f"pdflatex exited with {proc.returncode}:\n{output_tail}")

    pdf_path = tex_path.with_suffix(".pdf")
    if not pdf_path.exists():
        raise LatexCompileError(f"pdflatex failed — {pdf_path.name} not produced")
    return pdf_path
