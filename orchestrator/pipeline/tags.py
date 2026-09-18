"""Tag-structure check for widget output (FR-122, ER-2). Guarded directory (docs/CLAUDE.md).

The widget writes each translated slot back into the SAME text node, so the
inline-tag markers (open, close, void) in a translation must match the source
exactly: same markers, same order. Anything else means the block stays English.

This is the strict, format-independent core of S1.5. Formatting collapse for
proxy/CMS output (FR-123) comes with S1.5 after the Sprint 0 go/no-go.
"""

from __future__ import annotations

from orchestrator.pipeline.segment import Close, Marker, Open, Segment, SegmentError, Void, validate


def tag_structure(segment: Segment) -> tuple[Marker, ...]:
    return tuple(m for m in segment.structure() if isinstance(m, (Open, Close, Void)))


def check_tags(output: Segment, source: Segment) -> None:
    """Raise ``SegmentError("tag_mismatch")`` unless the tag markers match exactly."""
    validate(output.tokens)
    if tag_structure(output) != tag_structure(source):
        raise SegmentError("tag_mismatch", "tag markers differ from the source")
