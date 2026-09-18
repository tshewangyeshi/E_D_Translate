"""Cache and translation-memory keys (spec §2.6). Requirements: FR-143, FR-150, FR-153, FR-154.

Everything is keyed on the MASKED segment, so real entity values never enter
a key, Redis or PostgreSQL, and "Pay Nu. 500" / "Pay Nu. 600" share an entry.

    segment_key  = sha256(NFC(collapse_ws(masked wire)))
    approved_key = sha256(segment_key | src | tgt | pipeline_version | gfp)
    machine_key  = sha256(approved_key inputs | model_version)

Approved (human) translations are not keyed by model version, so a model
upgrade keeps human work; machine translations are, so it invalidates them.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass

from orchestrator.pipeline.segment import Segment


def _sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def segment_key(masked: Segment) -> str:
    """Canonical id of a masked segment. Safe to normalise: entities are already masked."""
    normalised = unicodedata.normalize("NFC", " ".join(masked.to_wire().split()))
    return _sha(normalised)


@dataclass(frozen=True)
class Versions:
    source_lang: str
    target_lang: str
    pipeline_version: str
    model_version: str


@dataclass(frozen=True)
class SegmentKeys:
    segment_key: str
    approved_key: str
    machine_key: str
    gfp: str


def keys_for(masked: Segment, gfp: str, versions: Versions) -> SegmentKeys:
    seg = segment_key(masked)
    approved = _sha(seg, versions.source_lang, versions.target_lang, versions.pipeline_version, gfp)
    machine = _sha(
        seg,
        versions.source_lang,
        versions.target_lang,
        versions.pipeline_version,
        gfp,
        versions.model_version,
    )
    return SegmentKeys(seg, approved, machine, gfp)


def approved_key_for(segment: str, gfp: str, versions: Versions) -> str:
    """Approved key for a stored segment_key (used when re-keying after a version change)."""
    return _sha(segment, versions.source_lang, versions.target_lang, versions.pipeline_version, gfp)
