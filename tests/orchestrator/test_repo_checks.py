"""The CI checks catch what they claim to: NFR-500 locale isolation and S0.3 requirement IDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.locale import dz
from tools import check_invisible, check_locale, check_req_ids, check_requirements

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


@pytest.mark.parametrize("cp", [0x200B, 0x200C, 0xFEFF, 0x00, 0x07])
def test_fr160_invisible_character_check_catches_literals(tmp_path: Path, cp: int) -> None:
    _write(tmp_path, "orchestrator/x.py", "value = 'a" + chr(cp) + "b'\n")
    assert check_invisible.violations(tmp_path)


def test_fr160_escaped_invisible_characters_are_fine(tmp_path: Path) -> None:
    _write(tmp_path, "orchestrator/x.py", "ZWSP = '" + chr(92) + "u200b'\n")
    assert check_invisible.violations(tmp_path) == []


def test_fr160_current_repo_has_no_invisible_characters() -> None:
    assert check_invisible.violations() == []


# --- dependency pins: requirements.txt must not drift from pyproject.toml ---


def test_pins_accept_a_well_formed_file() -> None:
    pins, errors = check_requirements.read_pins("# a comment\n\nfastapi==0.141.1\nredis==8.1.0\n")
    assert errors == []
    assert pins == {"fastapi": "0.141.1", "redis": "8.1.0"}


@pytest.mark.parametrize(
    "line",
    [
        "ruff>=0.6",  # a range says what is permitted, not what is installed
        "fastapi",  # unpinned
        "-r other.txt",  # include
        "--index-url https://example.invalid/simple",  # index option
        "-e .",  # editable
        "pkg @ https://example.invalid/pkg.whl",  # direct URL
    ],
)
def test_pins_reject_anything_that_is_not_an_exact_pin(line: str) -> None:
    _, errors = check_requirements.read_pins(line + "\n")
    assert errors, line


def test_pins_reject_a_duplicate_package() -> None:
    _, errors = check_requirements.read_pins("pytest==9.1.1\npytest==9.0.0\n")
    assert any("pinned again" in e for e in errors)


def test_pins_normalise_package_names_per_pep503() -> None:
    """Types-Redis, types_redis and types.redis are one package, not three."""
    pins, errors = check_requirements.read_pins("Types-Redis==1.0\ntypes_redis==1.0\n")
    assert errors and pins == {"types-redis": "1.0"}


def test_pins_catch_a_dependency_declared_but_never_pinned() -> None:
    errors = check_requirements.check_declared({"fastapi": "0.141.1"}, declared=["redis>=5.0"])
    assert any("missing from requirements.txt" in e for e in errors)


def test_pins_catch_a_pin_that_violates_the_declared_range() -> None:
    errors = check_requirements.check_declared({"fastapi": "0.99.0"}, declared=["fastapi>=0.115"])
    assert any("does not satisfy" in e for e in errors)


def test_pins_accept_a_pin_inside_the_declared_range() -> None:
    assert (
        check_requirements.check_declared({"fastapi": "0.141.1"}, declared=["fastapi>=0.115"]) == []
    )


def test_pins_in_the_current_repo_match_pyproject_and_the_environment() -> None:
    pins, errors = check_requirements.read_pins(
        check_requirements.REQUIREMENTS.read_text(encoding="utf-8")
    )
    assert errors == []
    assert check_requirements.check_declared(pins) == []
    assert check_requirements.check_environment(pins) == []
