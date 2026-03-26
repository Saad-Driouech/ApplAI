r"""
LaTeX Safety Validator
Scans generated .tex files for dangerous primitives before compilation.
LaTeX is Turing-complete — \write18 with --shell-escape = arbitrary code execution.

ALWAYS compile with: pdflatex -no-shell-escape
NEVER compile with: pdflatex --shell-escape
"""
import re
import subprocess
from pathlib import Path


DANGEROUS_PATTERNS = [
    (r'\\write18', "shell escape command execution"),
    (r'\\immediate\s*\\write', "immediate write (can execute commands)"),
    (r'\\input\{/', "absolute path file inclusion"),
    (r'\\include\{/', "absolute path file inclusion"),
    (r'\\openin', "file read primitive"),
    (r'\\openout', "file write primitive"),
    (r'\\catcode', "category code manipulation"),
    (r'\\csname\s+.*\\endcsname', "dynamic command construction"),
    (r'\\verbatiminput', "verbatim file inclusion"),
    (r'\\newwrite', "file handle creation"),
    (r'\\closeout', "file handle close"),
    (r'\\special\s*\{.*\|', "pipe in special command"),
    (r'\\url\{[^}]*\|', "pipe in url command"),
]


def validate_tex_file(filepath: str) -> dict:
    """
    Check a .tex file for dangerous LaTeX primitives.

    Returns:
        {"safe": bool, "violations": list[str]}
        If safe=False, do NOT compile the file.
    """
    try:
        content = Path(filepath).read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return {"safe": False, "violations": [f"File not found: {filepath}"]}

    violations = []
    for pattern, description in DANGEROUS_PATTERNS:
        if re.search(pattern, content):
            violations.append(f"{description}: matched '{pattern}'")

    return {"safe": len(violations) == 0, "violations": violations}


def safe_compile(tex_path: str, output_dir: str = None) -> dict:
    """
    Compile a .tex file to PDF with safety guarantees.
    Validates the file first, then compiles with --no-shell-escape.

    Returns:
        {"status": "success"|"blocked"|"error", ...}
    """
    check = validate_tex_file(tex_path)
    if not check["safe"]:
        return {
            "status": "blocked",
            "reason": f"Dangerous LaTeX detected: {check['violations']}",
        }

    out_dir = output_dir or str(Path(tex_path).parent)
    cmd = [
        "pdflatex",
        "-interaction=nonstopmode",
        "-no-shell-escape",
        "-output-directory", out_dir,
        tex_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            shell=False,
        )

        if result.returncode == 0:
            pdf_path = str(Path(tex_path).with_suffix('.pdf'))
            return {"status": "success", "pdf_path": pdf_path}
        else:
            return {
                "status": "error",
                "reason": "LaTeX compilation failed",
                "stderr": result.stderr[:500],
            }
    except subprocess.TimeoutExpired:
        return {"status": "error", "reason": "Compilation timed out (60s)"}
