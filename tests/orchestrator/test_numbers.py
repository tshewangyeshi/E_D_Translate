"""The model may translate amounts, dates, percentages and counts (FR-144, proposed).

Requirements: FR-140, FR-141, FR-142, FR-144.

Decided by the product owner on 2026-10-05, after the staging experiment kept
every value in 9 of 9 sentences while writing them the Dzongkha way. The rule
that makes it safe: a number may change script, never value. Identifiers --
citizen IDs, phone numbers, references, emails, URLs -- stay masked and come
back byte-identical, whatever this setting says.
"""

from __future__ import annotations

import pytest

from orchestrator.locale.dz import to_tibetan_digits
from orchestrator.pipeline.protect import (
    TRANSLATABLE_KINDS,
    EntityCheckError,
    mask,
    number_values,
    restore,
)
from orchestrator.pipeline.segment import Entity, Segment, parse, tokenize
from orchestrator.pipeline.validate import validate_output
from orchestrator.testing.mock_nmt import Mode
from orchestrator.testing.rig import ORIGIN, body, make_client, make_rig
from orchestrator.wiring import ConfigError, Settings

SENTENCE = "Pay Nu. 1,500 by 30 June 2026 and quote CID 00012345678, or call 17123456."


def _masked(text: str = SENTENCE) -> tuple[Segment, dict[int, object]]:
    return mask(parse(text), TRANSLATABLE_KINDS)  # type: ignore[return-value]


def _out(wire: str) -> Segment:
    return Segment(tokenize(wire, allow_entities=True))


# --- what reaches the model ---


def test_fr144_amounts_and_dates_go_to_the_model_identifiers_do_not() -> None:
    seg, entities = _masked()
    kinds = sorted(e.kind for e in entities.values())  # type: ignore[attr-defined]
    assert kinds == ["CID", "PHONE"]
    wire = seg.to_wire()
    assert "Nu. 1,500" in wire and "30 June 2026" in wire
    assert "00012345678" not in wire and "17123456" not in wire


@pytest.mark.parametrize(
    "text",
    [
        "Write to applications@portal.gov.example",
        "See https://portal.gov.example/services/renewal",
        "Quote DEMO-HR/2025/0457",
        "Call +975 9 000003",
    ],
)
def test_fr144_identifiers_are_always_masked(text: str) -> None:
    seg, entities = _masked(text)
    assert len(entities) == 1
    assert not any(ch.isdigit() for t in seg.tokens if not isinstance(t, Entity) for ch in t.value)  # type: ignore[union-attr]


def test_fr144_only_the_four_kinds_can_be_left_to_the_model() -> None:
    with pytest.raises(ValueError):
        mask(parse("CID 00012345678"), frozenset({"CID"}))


# --- values, any script ---


def test_fr144_a_number_has_one_value_in_any_script() -> None:
    assert number_values("Nu. 1,500 by 30 June 2026, 12.5%") == number_values(
        to_tibetan_digits("1500 30 2026 12.5")
    )
    assert number_values("30/06/2026") == number_values(to_tibetan_digits("30 6 2026"))


def test_fr144_digits_inside_a_name_are_not_values() -> None:
    """Seen on the pilot portal: "the G2C portal" came back as "the
    government-to-citizen services portal", correctly, with no 2 in it."""
    assert number_values("submit online on the G2C portal, B2B") == number_values("")
    assert number_values("photographs (45mm x 35mm)") == number_values("45 35")
    seg, entities = _masked("Submit on the G2C portal by 30 June 2026.")
    restore(_out(to_tibetan_digits("DZ 2026 6 30")), seg, entities)  # type: ignore[arg-type]


def test_fr144_a_time_on_the_hour_is_its_hour() -> None:
    """Seen on the pilot portal: "9:00 am to 12:00 pm" came back as "9 to 12"."""
    assert number_values("9:00 am to 12:00 pm, 02:00pm") == number_values("9 12 2")
    assert number_values("9:30") == number_values("9 30")
    assert number_values("9:00") != number_values("9 30")


def test_fr144_a_fraction_cannot_be_compared_so_it_is_refused() -> None:
    with pytest.raises(ValueError):
        number_values("half: ½")


# --- the check after the model ---


def _source() -> tuple[Segment, dict[int, object]]:
    return _masked("Pay Nu. 500 by 30 June 2026, CID 00012345678.")


def test_fr144_the_same_values_in_tibetan_digits_are_served() -> None:
    seg, entities = _source()
    cid = next(i for i, e in entities.items() if e.kind == "CID")  # type: ignore[attr-defined]
    model = to_tibetan_digits("DZ 2026 6 30 DZ 500 DZ ") + f"⟦CID:{cid}⟧"
    out = restore(_out(model), seg, entities)  # type: ignore[arg-type]
    assert "00012345678" in out.to_wire()  # the identifier, byte-identical


@pytest.mark.parametrize(
    "model",
    [
        "DZ 2026 6 30 DZ 600 DZ ",  # an amount changed
        "DZ 2026 6 DZ 500 DZ ",  # a date lost its day
        "DZ 2026 6 30 DZ 500 DZ 7 ",  # a number invented
        "DZ 2026 6 30 DZ 5000 DZ ",  # merged or mistyped
    ],
)
def test_fr144_a_changed_lost_or_invented_number_falls_back(model: str) -> None:
    seg, entities = _source()
    cid = next(i for i, e in entities.items() if e.kind == "CID")  # type: ignore[attr-defined]
    with pytest.raises(EntityCheckError) as raised:
        restore(_out(to_tibetan_digits(model) + f"⟦CID:{cid}⟧"), seg, entities)  # type: ignore[arg-type]
    assert raised.value.reason == "leak"


def test_fr144_a_named_month_may_come_back_as_its_number_and_only_that() -> None:
    seg, entities = _masked("Apply by 30 June 2026.")
    restore(_out(to_tibetan_digits("DZ 2026 6 30")), seg, entities)  # type: ignore[arg-type]
    restore(_out("DZ 2026 month-word 30"), seg, entities)  # type: ignore[arg-type]
    with pytest.raises(EntityCheckError):
        restore(_out(to_tibetan_digits("DZ 2026 7 30")), seg, entities)  # type: ignore[arg-type]
    with pytest.raises(EntityCheckError):
        restore(_out(to_tibetan_digits("DZ 2026 6 6 30")), seg, entities)  # type: ignore[arg-type]


def test_fr144_may_the_verb_is_not_a_month() -> None:
    seg, entities = _masked("You may pay Nu. 500.")
    with pytest.raises(EntityCheckError):
        restore(_out(to_tibetan_digits("DZ 500 5")), seg, entities)  # type: ignore[arg-type]


def test_fr141_a_masked_identifier_is_still_required_exactly_once() -> None:
    seg, entities = _source()
    with pytest.raises(EntityCheckError) as raised:
        restore(_out(to_tibetan_digits("DZ 2026 6 30 DZ 500")), seg, entities)  # type: ignore[arg-type]
    assert raised.value.reason == "missing"


def test_fr142_an_email_or_url_written_by_the_model_is_still_a_leak() -> None:
    seg, entities = _masked("Pay Nu. 500 today.")
    with pytest.raises(EntityCheckError):
        restore(_out("DZ 500 www.example.bt"), seg, entities)  # type: ignore[arg-type]


def test_fr142_with_everything_masked_any_numeral_is_still_a_leak() -> None:
    """The protected setting behaves exactly as before."""
    seg, entities = mask(parse("Pay Nu. 500 today."))
    with pytest.raises(EntityCheckError):
        restore(_out("DZ ⟦CUR:1⟧ " + to_tibetan_digits("5")), seg, entities)


def test_fr144_the_worker_applies_the_same_rule_to_masked_jobs() -> None:
    seg, _ = _source()
    cid = next(t.id for t in seg.tokens if isinstance(t, Entity))
    validate_output(_out(to_tibetan_digits("DZ 2026 6 30 DZ 500 ") + f"⟦CID:{cid}⟧"), seg)
    with pytest.raises(EntityCheckError):
        validate_output(_out(to_tibetan_digits("DZ 2026 6 30 DZ 501 ") + f"⟦CID:{cid}⟧"), seg)


# --- through the API, and the setting ---


def test_fr144_a_fee_sentence_translates_with_the_model_setting() -> None:
    rig = make_rig(translate_kinds=TRANSLATABLE_KINDS)
    response = make_client(rig).post(
        "/v1/translate", json=body("Pay Nu. 500 by 30 June 2026."), headers={"Origin": ORIGIN}
    )
    (segment,) = response.json()["segments"]
    assert segment["status"] == "translated"
    assert "Nu. 500" in segment["text"]  # the mock copies the English, numbers and all


def test_fr144_an_invented_number_keeps_the_page_english() -> None:
    rig = make_rig(translate_kinds=TRANSLATABLE_KINDS, modes={Mode.INVENT_NUMBER: 1.0})
    response = make_client(rig).post(
        "/v1/translate", json=body("Pay Nu. 500 today."), headers={"Origin": ORIGIN}
    )
    (segment,) = response.json()["segments"]
    assert segment["status"] == "entity_check_failed"
    assert segment["text"] == "Pay Nu. 500 today."


def _env(**extra: str) -> dict[str, str]:
    return {
        "DZWEB_PG_DSN": "postgresql://x@127.0.0.1:1/x",
        "DZWEB_REDIS_URL": "redis://127.0.0.1:1/0",
        "DZWEB_TERMBASE": "termbase.json",
        "DZWEB_SITES": "sites.json",
        **extra,
    }


def test_fr144_the_model_setting_is_the_default_and_protected_remains_available() -> None:
    assert Settings.from_env(_env()).numbers == "model"
    assert Settings.from_env(_env(DZWEB_NUMBERS="protected")).numbers == "protected"
    with pytest.raises(ConfigError, match="DZWEB_NUMBERS"):
        Settings.from_env(_env(DZWEB_NUMBERS="sometimes"))
