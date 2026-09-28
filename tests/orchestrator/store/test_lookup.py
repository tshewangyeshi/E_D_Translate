"""Tier-gated lookup, keys and cache behaviour (S1.7).

Requirements: FR-510, FR-411, FR-143, FR-150, FR-151, FR-153, FR-154, NFR-304, NFR-410.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from orchestrator.pipeline import version as version_mod
from orchestrator.pipeline.protect import mask, restore
from orchestrator.pipeline.segment import parse
from orchestrator.store.cache import (
    APPROVED_NS,
    InMemoryCache,
    InMemorySeenCounter,
    RedisCache,
    RedisSeenCounter,
    ResilientCache,
    ResilientSeenCounter,
    encode,
)
from orchestrator.store.keys import Versions, keys_for, segment_key
from orchestrator.store.lookup import LookupItem, TranslationStore, gate_tier
from orchestrator.store.models import Origin, Stored
from orchestrator.store.tm import InMemoryTM

V1 = Versions("en", "dz", "p-test", "nllb-1")
V2_MODEL = Versions("en", "dz", "p-test", "nllb-2")
GFP = "0" * 64


def _keys(text: str, versions: Versions = V1, gfp: str = GFP) -> Any:
    masked, _ = mask(parse(text))
    return keys_for(masked, gfp, versions), masked


def _store(**kw: Any) -> tuple[TranslationStore, InMemoryTM, InMemoryCache]:
    tm, cache = InMemoryTM(), InMemoryCache()
    return TranslationStore(tm, ResilientCache(cache), InMemorySeenCounter(), **kw), tm, cache


def _machine(store: TranslationStore, keys: Any, masked: Any, target: str) -> Stored:
    return store.record_machine(
        keys=keys,
        masked_source=masked.to_wire(),
        masked_target=target,
        model_version="nllb-1",
        term_ids=(),
        tag_integrity=True,
    )


# --- keys (FR-143, FR-150) ---


def test_fr143_entity_values_share_one_entry_and_restore_separately() -> None:
    k500, m500 = _keys("Pay Nu. 500 today")
    k600, _ = _keys("Pay Nu. 600 today")
    assert k500 == k600
    _, map500 = mask(parse("Pay Nu. 500 today"))
    _, map600 = mask(parse("Pay Nu. 600 today"))
    out = parse("Today ⟦CUR:1⟧ pay", allow_entities=True)
    assert restore(out, m500, map500).plain_text() == "Today Nu. 500 pay"
    assert restore(out, m500, map600).plain_text() == "Today Nu. 600 pay"


def test_fr143_whitespace_variants_share_a_key_but_keep_their_bytes() -> None:
    a, ma = _keys("Pay   Nu. 500   today")
    b, _ = _keys("Pay Nu. 500 today")
    assert a.segment_key == b.segment_key
    _, entities = mask(parse("Pay   Nu. 500   today"))
    assert restore(ma, ma, entities).plain_text() == "Pay   Nu. 500   today"


def test_fr150_model_version_is_in_machine_keys_but_not_approved_keys() -> None:
    k1, _ = _keys("Apply online", V1)
    k2, _ = _keys("Apply online", V2_MODEL)
    assert k1.approved_key == k2.approved_key
    assert k1.machine_key != k2.machine_key


def test_fr153_glossary_fingerprint_changes_both_keys() -> None:
    k1, _ = _keys("Pay the fee", gfp="1" * 64)
    k2, _ = _keys("Pay the fee", gfp="2" * 64)
    assert k1.approved_key != k2.approved_key and k1.machine_key != k2.machine_key


# --- pipeline version (FR-154) ---


def test_fr154_pipeline_version_is_derived_and_stable() -> None:
    assert version_mod.compute_pipeline_version() == version_mod.compute_pipeline_version()
    assert version_mod.pipeline_version().startswith("p-")


def test_fr154_changing_a_masking_pattern_changes_the_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    before = version_mod.compute_pipeline_version()
    patterns = tuple(
        (kind, re.compile(rx.pattern + "|zzz") if kind == "PCT" else rx)
        for kind, rx in version_mod.PATTERNS
    )
    monkeypatch.setattr(version_mod, "PATTERNS", patterns)
    assert version_mod.compute_pipeline_version() != before


def test_fr154_changing_golden_output_changes_the_version(tmp_path: Path) -> None:
    corpus = json.loads(version_mod.GOLDEN.read_text(encoding="utf-8"))
    corpus["segments"].append("One more pinned behaviour: 42.")
    alt = tmp_path / "corpus.json"
    alt.write_text(json.dumps(corpus), encoding="utf-8")
    assert version_mod.compute_pipeline_version(alt) != version_mod.compute_pipeline_version()


# --- tier gate (FR-510, ER-1) ---


def test_fr510_tier1_never_served_cached_mt() -> None:
    store, _, _ = _store()
    keys, masked = _keys("Applications close on 30 June 2026")
    _machine(store, keys, masked, "mt ⟦DATE:1⟧")  # filled by a Tier 2 page
    tier2, tier1 = store.lookup([LookupItem(keys, 2), LookupItem(keys, 1)])
    assert tier2 is not None and tier2.stored.origin is Origin.MT
    assert tier1 is None  # Tier 1 gets tier_blocked upstream, never MT


def test_fr510_tier1_is_served_an_approved_translation() -> None:
    store, _, _ = _store()
    keys, masked = _keys("Applications close on 30 June 2026")
    store.approve(
        keys=keys,
        masked_source=masked.to_wire(),
        masked_target="ok ⟦DATE:1⟧",
        author="r1",
        term_ids=(),
    )
    (hit,) = store.lookup([LookupItem(keys, 1)])
    assert hit is not None and hit.stored.origin is Origin.HUMAN


def test_fr510_machine_value_in_the_approved_namespace_is_ignored() -> None:
    store, _, cache = _store()
    keys, _ = _keys("Eligibility rules apply")
    cache.data[APPROVED_NS + keys.approved_key] = encode(Stored(99, Origin.MT, "poisoned"))
    assert store.lookup([LookupItem(keys, 1)]) == [None]
    assert store.lookup([LookupItem(keys, 2)]) == [None]


@pytest.mark.parametrize("bad", [0, 4, None, "2", 2.0, True, -2])
def test_fr510_unknown_tier_is_treated_as_tier1(bad: object) -> None:
    assert gate_tier(bad) == 1
    assert gate_tier(2) == 2 and gate_tier(3) == 3


# --- lookup order and batching (FR-411, ER-21) ---


def test_fr411_approved_wins_over_machine_for_tier2() -> None:
    store, _, _ = _store()
    keys, masked = _keys("Visit the office")
    _machine(store, keys, masked, "machine")
    store.approve(
        keys=keys, masked_source=masked.to_wire(), masked_target="human", author="r1", term_ids=()
    )
    (hit,) = store.lookup([LookupItem(keys, 2)])
    assert hit is not None and hit.stored.masked_target == "human"


def test_fr411_one_batched_tm_read_per_request_and_cache_backfill() -> None:
    store, tm, cache = _store()
    items = []
    for n in range(20):
        keys, masked = _keys(f"Sentence number {n} here")
        tm.store_machine(
            segment_key=keys.segment_key,
            masked_source=masked.to_wire(),
            machine_key=keys.machine_key,
            masked_target=f"t{n}",
            gfp=GFP,
            model_version="nllb-1",
            term_ids=(),
            tag_integrity=True,
        )
        items.append(LookupItem(keys, 2))
    first = store.lookup(items)
    assert all(h is not None and h.source == "tm" for h in first)
    assert tm.lookup_calls == 1
    second = store.lookup(items)
    assert all(h is not None and h.source == "cache" for h in second)
    assert tm.lookup_calls == 1  # fully served from the backfilled cache


def test_fr150_model_upgrade_misses_machine_but_keeps_approved() -> None:
    store, _, _ = _store()
    keys1, masked = _keys("Apply online", V1)
    _machine(store, keys1, masked, "machine v1")
    other1, other_masked = _keys("Approved sentence", V1)
    store.approve(
        keys=other1,
        masked_source=other_masked.to_wire(),
        masked_target="human",
        author="r1",
        term_ids=(),
    )
    keys2, _ = _keys("Apply online", V2_MODEL)
    other2, _ = _keys("Approved sentence", V2_MODEL)
    assert store.lookup([LookupItem(keys2, 2), LookupItem(other2, 2)])[0] is None
    assert store.lookup([LookupItem(other2, 2)])[0] is not None


def test_fr421_approval_supersedes_a_cached_machine_translation_immediately() -> None:
    store, _, _ = _store()
    keys, masked = _keys("Visit the office")
    _machine(store, keys, masked, "machine")
    assert store.lookup([LookupItem(keys, 2)])[0].stored.origin is Origin.MT  # type: ignore[union-attr]
    store.approve(
        keys=keys, masked_source=masked.to_wire(), masked_target="human", author="r1", term_ids=()
    )
    (hit,) = store.lookup([LookupItem(keys, 2)])
    assert hit is not None and hit.stored.masked_target == "human"


# --- glossary invalidation through the store (FR-153) ---


def test_fr153_term_publish_purges_cache_and_stops_serving_approved() -> None:
    store, _, cache = _store()
    keys, masked = _keys("Pay the fee", gfp="1" * 64)
    store.approve(
        keys=keys,
        masked_source=masked.to_wire(),
        masked_target="⟦T:1⟧ human",
        author="r1",
        term_ids=("T-0005",),
    )
    assert store.lookup([LookupItem(keys, 1)])[0] is not None
    assert APPROVED_NS + keys.approved_key in cache.data
    result = store.invalidate_terms(["T-0005"])
    assert result.recheck_segments == (keys.segment_key,)
    assert APPROVED_NS + keys.approved_key not in cache.data
    assert store.lookup([LookupItem(keys, 1)]) == [None]


# --- Redis down: degrade, never fail (FR-151, NFR-410) ---


class _BrokenCache:
    def get_many(self, keys: Sequence[str]) -> dict[str, Stored]:
        raise ConnectionError("redis down")

    def set(self, key: str, value: Stored, ttl_seconds: int | None) -> None:
        raise ConnectionError("redis down")

    def delete_many(self, keys: Sequence[str]) -> None:
        raise ConnectionError("redis down")


def test_fr151_redis_down_degrades_to_tm_without_error() -> None:
    tm = InMemoryTM()
    resilient = ResilientCache(_BrokenCache())
    store = TranslationStore(tm, resilient, InMemorySeenCounter())
    keys, masked = _keys("Visit the office")
    stored = _machine(store, keys, masked, "machine")  # cache write fails silently
    (hit,) = store.lookup([LookupItem(keys, 2)])
    assert hit is not None and hit.stored == stored and hit.source == "tm"
    assert resilient.failures >= 2
    store.invalidate_terms(["T-0005"])  # purge failure is also tolerated


# --- NFR-304: persist only after N distinct clients ---


def test_nfr304_tier2_text_is_persisted_only_after_n_distinct_clients() -> None:
    store, _, _ = _store()
    keys, _ = _keys("Welcome back to your applications")
    assert not store.should_persist(keys, "client-a")
    assert not store.should_persist(keys, "client-a")  # same client again
    assert not store.should_persist(keys, "client-b")
    assert store.should_persist(keys, "client-c")


class _BrokenSeen:
    def observe(self, segment_key: str, client_hash: str) -> int:
        raise ConnectionError("redis down")


def test_nfr304_counter_failure_means_do_not_persist() -> None:
    seen = ResilientSeenCounter(_BrokenSeen())
    store = TranslationStore(InMemoryTM(), InMemoryCache(), seen)
    keys, _ = _keys("Welcome back")
    assert not any(store.should_persist(keys, f"c{n}") for n in range(10))
    assert seen.failures == 10


def test_fr143_segment_key_contains_no_entity_value() -> None:
    masked, _ = mask(parse("CID 00012345678 and Nu. 500"))
    assert "00012345678" not in masked.to_wire()
    assert len(segment_key(masked)) == 64


# --- Redis adapter (integration) ---


@pytest.mark.integration
def test_fr151_redis_cache_round_trip(redis_client: Any) -> None:
    cache = RedisCache(redis_client)
    value = Stored(7, Origin.HUMAN, "masked ⟦CUR:1⟧ target")
    cache.set("dzweb:a:k1", value, 60)
    assert cache.get_many(["dzweb:a:k1", "dzweb:a:missing"]) == {"dzweb:a:k1": value}
    cache.delete_many(["dzweb:a:k1"])
    assert cache.get_many(["dzweb:a:k1"]) == {}


@pytest.mark.integration
def test_nfr304_redis_seen_counter_counts_distinct_clients(redis_client: Any) -> None:
    seen = RedisSeenCounter(redis_client, ttl_seconds=60)
    assert [seen.observe("s1", c) for c in ("a", "a", "b")] == [1, 1, 2]
