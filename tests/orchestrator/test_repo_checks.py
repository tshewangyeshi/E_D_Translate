"""The CI checks catch what they claim to: NFR-500 locale isolation and S0.3 requirement IDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.locale import dz
from tools import check_locale, check_req_ids

# Sample IDs are assembled at runtime so the repo-wide ID scan does not see them here.
_FR = "FR" + "-"
_NFR = "NFR" + "-"
REQS = (
    "| ID | Requirement | Status |\n|---|---|---|\n"
    f"| {_FR}140 | Entities byte-identical. | R |\n"
    f"| {_FR}501 … {_FR}503 | *(range only)* | ? |\n"
    f"| {_NFR}304 | Personal data controls. | P |\n"
)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_s03_real_repo_requirements_file_parses() -> None:
    status = check_req_ids.load_requirements()
    assert status[f"{_FR}140"] == "R"
    assert status[f"{_FR}155"] == "P"
    assert status[f"{_FR}505"] == "?"


def test_s03_requirement_check_finds_undefined_and_range_only_ids(tmp_path: Path) -> None:
    reqs = tmp_path / "reqs.md"
    reqs.write_text(REQS, encoding="utf-8")
    assert check_req_ids.load_requirements(reqs) == {
        f"{_FR}140": "R",
        f"{_FR}501": "?",
        f"{_FR}502": "?",
        f"{_FR}503": "?",
        f"{_NFR}304": "P",
    }
    body = "def test_" + "fr140_ok(): ...\ndef test_" + "fr999_bad(): ...\n# " + _FR + "502\n"
    _write(tmp_path, "tests/test_x.py", body)
    cites = {rid for _, _, rid in check_req_ids.iter_citations(tmp_path)}
    assert cites == {f"{_FR}140", f"{_FR}999", f"{_FR}502"}


@pytest.mark.parametrize(
    "line",
    [
        f"TSHEG = '{dz.TSHEG}'",
        "SHAD = '\\u0f0d'",
        "x = '\\x{0F0B}'",
        "html = '&#x0F0B;'",
        "html = '&#3851;'",
    ],
)
def test_nfr500_locale_check_catches_literal_and_escaped_tibetan(tmp_path: Path, line: str) -> None:
    _write(tmp_path, "orchestrator/pipeline/leak.py", line + "\n")
    assert check_locale.violations(tmp_path)


def test_nfr500_locale_homes_are_allowed(tmp_path: Path) -> None:
    _write(tmp_path, "orchestrator/locale/dz.py", f"TSHEG = '{dz.TSHEG}'\n")
    _write(tmp_path, "adapters/widget/src/locale-dz.ts", f"export const TSHEG = '{dz.TSHEG}';\n")
    _write(tmp_path, "orchestrator/pipeline/clean.py", "x = 'plain ascii'\n")
    assert check_locale.violations(tmp_path) == []


def test_nfr500_current_repo_is_clean() -> None:
    assert check_locale.violations() == []


def test_fr160_render_breaks_are_stripped_before_storage() -> None:
    word = dz.TSHEG.join(["a", "b", "c"])
    rendered = dz.insert_breaks(word)
    assert dz.ZWSP in rendered
    assert dz.strip_render_artefacts(rendered) == word
