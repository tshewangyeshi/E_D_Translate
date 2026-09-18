"""Translation-memory contract: InMemoryTM and PostgresTM must behave identically.

Requirements: FR-410, FR-411, FR-412, FR-143, FR-153, FR-154, NFR-305.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from orchestrator.store.models import Origin, ReviewState

SEG = "a" * 64
SEG2 = "b" * 64
GFP = "0" * 64
GFP_TERM = "1" * 64


def _mt(
    tm: Any, key: str, target: str, *, seg: str = SEG, gfp: str = GFP, terms: tuple[str, ...] = ()
) -> Any:
    return tm.store_machine(
        segment_key=seg,
        masked_source="Pay ⟦CUR:1⟧",
        machine_key=key,
        masked_target=target,
        gfp=gfp,
        model_version="nllb-test-1",
        term_ids=terms,
        tag_integrity=True,
    )


def _approve(
    tm: Any, key: str, target: str, *, seg: str = SEG, gfp: str = GFP, terms: tuple[str, ...] = ()
) -> Any:
    return tm.approve(
        segment_key=seg,
        masked_source="Pay ⟦CUR:1⟧",
        approved_key=key,
        masked_target=target,
        gfp=gfp,
        author="reviewer-1",
        term_ids=terms,
        site_id="portal",
    )


def test_fr411_lookup_returns_latest_machine_and_current_approved(tm: Any) -> None:
    _mt(tm, "m1" + "x" * 62, "old ⟦CUR:1⟧")
    newer = _mt(tm, "m1" + "x" * 62, "new ⟦CUR:1⟧")
    ok = _approve(tm, "a1" + "x" * 62, "approved ⟦CUR:1⟧")
    approved, machine = tm.lookup(["a1" + "x" * 62, "zz" * 32], ["m1" + "x" * 62])
    assert approved["a1" + "x" * 62].version_id == ok.version_id
    assert approved["a1" + "x" * 62].origin is Origin.HUMAN
    assert machine["m1" + "x" * 62].version_id == newer.version_id
    assert "zz" * 32 not in approved


def test_fr412_approval_creates_a_new_version_and_keeps_history(tm: Any) -> None:
    first = _approve(tm, "a" * 64, "first ⟦CUR:1⟧")
    second = _approve(tm, "a" * 64, "second ⟦CUR:1⟧")
    history = tm.history(SEG)
    assert [v.masked_target for v in history] == ["first ⟦CUR:1⟧", "second ⟦CUR:1⟧"]
    item = tm.review_item(SEG)
    assert item.current_version == second.version_id != first.version_id
    assert item.state is ReviewState.APPROVED
    assert item.site_id == "portal"


def test_fr143_only_masked_text_is_stored(tm: Any) -> None:
    _mt(tm, "m" * 64, "Pay ⟦CUR:1⟧ today")
    stored = tm.history(SEG)[0]
    assert "⟦CUR:1⟧" in stored.masked_target
    assert not any(ch.isdigit() for ch in stored.masked_target.replace("CUR:1", ""))


def test_fr153_term_invalidation_hits_exactly_the_affected_versions(tm: Any) -> None:
    _mt(tm, "m" * 64, "with term ⟦T:2⟧", gfp=GFP_TERM, terms=("T-0005",))
    _mt(tm, "n" * 64, "no term", seg=SEG2)
    _approve(tm, "a" * 64, "approved ⟦T:2⟧", gfp=GFP_TERM, terms=("T-0005",))
    result = tm.invalidate_terms(["T-0005"])
    assert result.machine_keys == ("m" * 64,)
    assert result.recheck_segments == (SEG,)
    approved, machine = tm.lookup(["a" * 64], ["m" * 64, "n" * 64])
    assert approved == {}  # needs_recheck is never served
    assert set(machine) == {"n" * 64}
    assert tm.review_item(SEG).state is ReviewState.NEEDS_RECHECK


def test_fr153_unrelated_term_changes_nothing(tm: Any) -> None:
    _mt(tm, "m" * 64, "with term ⟦T:2⟧", gfp=GFP_TERM, terms=("T-0005",))
    result = tm.invalidate_terms(["T-9999"])
    assert result.machine_keys == () and result.recheck_segments == ()


def test_fr154_pipeline_migration_rekeys_or_rechecks_approvals(tm: Any) -> None:
    _approve(tm, "a" * 64, "keep ⟦CUR:1⟧")
    _approve(tm, "c" * 64, "changed", seg=SEG2)

    def rekey(seg: str, masked_source: str, gfp: str) -> str | None:
        return "k" * 64 if seg == SEG else None

    report = tm.migrate_pipeline(rekey)
    assert (report.rekeyed, report.needs_recheck) == (1, 1)
    approved, _ = tm.lookup(["k" * 64, "c" * 64], [])
    assert set(approved) == {"k" * 64}
    assert tm.review_item(SEG2).state is ReviewState.NEEDS_RECHECK


def test_nfr305_retention_expires_old_machine_rows_only(tm: Any) -> None:
    _mt(tm, "m" * 64, "machine")
    _approve(tm, "a" * 64, "human")
    expired = tm.expire_machine(datetime.now(UTC) + timedelta(seconds=5))
    assert expired == 1
    approved, machine = tm.lookup(["a" * 64], ["m" * 64])
    assert machine == {} and set(approved) == {"a" * 64}


@pytest.mark.integration
def test_fr412_database_refuses_to_rewrite_a_translation_version(pg_conn: Any) -> None:
    import psycopg

    from orchestrator.store.postgres_tm import PostgresTM

    tm = PostgresTM(pg_conn)
    stored = _mt(tm, "m" * 64, "original")
    with pytest.raises(psycopg.errors.RaiseException):
        pg_conn.execute(
            "UPDATE translation_version SET masked_target = 'tampered' WHERE id = %s",
            (stored.version_id,),
        )
    with pytest.raises(psycopg.errors.RaiseException):
        pg_conn.execute("DELETE FROM translation_version WHERE id = %s", (stored.version_id,))
