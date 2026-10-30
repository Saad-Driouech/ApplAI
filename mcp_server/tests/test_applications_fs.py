"""Tests for the read-only job_applications/ index."""
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from applai_mcp.applications_fs import ApplicationsIndex, normalize, similarity
from conftest import write


def _by_path(index):
    return {r.path: r for r in index.scan()}


def test_layouts_stages_and_countries(apps_dir):
    records = _by_path(ApplicationsIndex(apps_dir))
    assert set(records) == {"germany/SAP/Forward_Deployed_AI_Engineer", "germany/Lio/Forward_Deployed_Engineer",
                            "germany/Acme", "uae/Vatic/Research"}

    sap = records["germany/SAP/Forward_Deployed_AI_Engineer"]
    assert (sap.country, sap.company, sap.role, sap.layout) == ("DE", "SAP", "Forward Deployed AI Engineer", "nested")
    assert (sap.has_jd, sap.has_cv, sap.stage) == (True, True, "prepared")
    assert sap.notes == ["2026-09-16: apply via referral"]

    acme = records["germany/Acme"]
    assert (acme.layout, acme.role, acme.has_cover_letter, acme.stage) == ("flat", None, True, "prepared")
    assert records["uae/Vatic/Research"].stage == "researching"


def test_prep_files_listed_once_preferring_markdown(apps_dir):
    lio = _by_path(ApplicationsIndex(apps_dir))["germany/Lio/Forward_Deployed_Engineer"]
    assert lio.stage == "interviewing"
    assert lio.prep_files == ["germany/Lio/Forward_Deployed_Engineer/Lio_Call_Prep.md"]


def test_company_level_prep_attaches_and_archives_are_skipped(apps_dir):
    write(apps_dir, "germany/SAP/Interviews/2/Interview_Day_Checklist.md")
    write(apps_dir, "germany/SAP/old/Saad_Driouech_SAP_CV.pdf")
    records = _by_path(ApplicationsIndex(apps_dir))
    assert "germany/SAP/old" not in records
    sap = records["germany/SAP/Forward_Deployed_AI_Engineer"]
    assert sap.prep_files == ["germany/SAP/Interviews/2/Interview_Day_Checklist.md"]
    assert sap.stage == "interviewing"


def test_office_temp_files_and_hidden_folders_ignored(apps_dir):
    write(apps_dir, "germany/Lio/Forward_Deployed_Engineer/~$o_Call_Prep.docx")
    write(apps_dir, "germany/.pending/abc/cv.pdf")
    lio = _by_path(ApplicationsIndex(apps_dir))["germany/Lio/Forward_Deployed_Engineer"]
    assert all("~$" not in p for p in lio.prep_files)


def test_two_letter_folders_count_as_countries(apps_dir):
    write(apps_dir, "fr/Mistral/Saad_Driouech_Mistral_CV.pdf")
    assert _by_path(ApplicationsIndex(apps_dir))["fr/Mistral"].country == "FR"


def test_symlinks_are_not_followed(apps_dir, tmp_path):
    outside = tmp_path / "outside"
    write(outside, "Secret_Interview_Prep.md")
    (apps_dir / "germany" / "Linked").symlink_to(outside)
    (apps_dir / "germany" / "Lio" / "Forward_Deployed_Engineer" / "link").symlink_to(outside)
    records = _by_path(ApplicationsIndex(apps_dir))
    assert "germany/Linked" not in records
    assert not any("Secret" in p for r in records.values() for p in r.prep_files)


def test_missing_root_is_a_tool_error(tmp_path):
    with pytest.raises(ToolError, match="APPLAI_APPLICATIONS_DIR"):
        ApplicationsIndex(tmp_path / "nope").scan()


def test_find_ranks_company_then_role(apps_dir):
    matches = ApplicationsIndex(apps_dir).find("SAP SE", "Forward Deployed AI Engineer (m/f/d)")
    assert [m.application.path for m in matches] == ["germany/SAP/Forward_Deployed_AI_Engineer"]
    assert matches[0].company_similarity == 0.9 and matches[0].title_similarity == 1.0
    assert ApplicationsIndex(apps_dir).find("Totally Different GmbH") == []


class TestReadText:
    def test_reads_inside_root(self, apps_dir):
        assert ApplicationsIndex(apps_dir).read_text("germany/Lio/Forward_Deployed_Engineer/Lio_Call_Prep.md") == "# Prep"

    @pytest.mark.parametrize("rel", ["../outside.txt", "/etc/passwd", "germany/../../outside.txt", "germany"])
    def test_refuses_paths_outside_root_or_not_files(self, apps_dir, tmp_path, rel):
        write(tmp_path, "outside.txt", "secret")
        with pytest.raises(ToolError):
            ApplicationsIndex(apps_dir).read_text(rel)

    def test_refuses_symlink_escaping_root(self, apps_dir, tmp_path):
        write(tmp_path, "outside.txt", "secret")
        (apps_dir / "escape.txt").symlink_to(tmp_path / "outside.txt")
        with pytest.raises(ToolError):
            ApplicationsIndex(apps_dir).read_text("escape.txt")


@pytest.mark.parametrize("a,b,expected", [
    ("AI Engineer (m/w/d)", "ai_engineer", 1.0),
    ("SAP", "SAP SE", 0.9),
    ("", "SAP", 0.0),
])
def test_similarity(a, b, expected):
    assert similarity(a, b) == expected


def test_normalize_matches_applai_dedup_rules():
    assert normalize("  Senior AI-Engineer [all genders] ") == "senior ai engineer"
