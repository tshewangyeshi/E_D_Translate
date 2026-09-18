"""Validate model output against a MASKED source (FR-122, FR-140..142, FR-401).

Guarded directory (docs/CLAUDE.md).

The background worker only ever holds masked text (jobs never carry entity
values, FR-143), so it cannot restore real values. This check proves the
output is restorable for ANY request that maps to the same masked source:

  * every entity and glossary placeholder exactly once, kinds unchanged
  * no numeral, email or URL in model-authored text (leak scan)
  * no two entities newly touching (merged numbers)
  * inline-tag markers exactly as in the source

The live request path applies the same rules while restoring real values,
so both paths enforce one policy.
"""

from __future__ import annotations

from orchestrator.pipeline.glossary import TERM_KIND, Term, restore_terms
from orchestrator.pipeline.protect import ENTITY_KINDS, MaskedEntity, restore
from orchestrator.pipeline.segment import Entity, Segment
from orchestrator.pipeline.tags import check_tags

_OPAQUE = "<value>"  # stands in for a real value; the result is discarded, never shown or stored


def validate_output(output: Segment, source: Segment) -> None:
    """Raise SegmentError (EntityCheckError / GlossaryCheckError / tag failure) or return."""
    markers = [m for m in source.structure() if isinstance(m, Entity)]
    entities = {m.id: MaskedEntity(m.kind, _OPAQUE) for m in markers if m.kind in ENTITY_KINDS}
    terms = {
        m.id: Term(
            term_id=f"job-{m.id}", term_version=1, source="x", target=_OPAQUE, case_sensitive=False
        )
        for m in markers
        if m.kind == TERM_KIND
    }
    restored = restore(output, source, entities)
    restored = restore_terms(restored, source, terms)
    check_tags(restored, source)
