"""Tag-structure check for widget output (FR-122, ER-2). Guarded directory (docs/CLAUDE.md).

The widget writes each translated slot back into the SAME text node, so the
inline-tag markers (open, close, void) in a translation must match the source
exactly: same markers, same order. Anything else means the block stays English.

Proxy, CMS and ``/v1/translate/html`` output can do better than English: it
writes new markup rather than reusing the page's nodes, so a translation whose
tags did not survive is kept, and the segment's formatting is applied to the
whole of it (``collapse_formatting``, FR-123). The widget never collapses: it
cannot restructure host nodes (FR-210).
"""

from __future__ import annotations

from orchestrator.pipeline.segment import (
    Close,
    Entity,
    Marker,
    Open,
    Segment,
    SegmentError,
    Text,
    Token,
    Void,
    validate,
)


def tag_structure(segment: Segment) -> tuple[Marker, ...]:
    return tuple(m for m in segment.structure() if isinstance(m, (Open, Close, Void)))


def check_tags(output: Segment, source: Segment) -> None:
    """Raise ``SegmentError("tag_mismatch")`` unless the tag markers match exactly."""
    validate(output.tokens)
    if tag_structure(output) != tag_structure(source):
        raise SegmentError("tag_mismatch", "tag markers differ from the source")


def collapse_formatting(output: Segment, source: Segment) -> Segment:
    """The translation with the source's formatting applied to the whole (FR-123).

    Whatever tags the model wrote are dropped; its text and placeholders are
    kept in order. Every inline span of the source then wraps the whole text,
    nested in source order, so ``Click <a>here</a> to <b>apply</b>`` becomes
    ``<a><b>translated sentence</b></a>``. Void elements (a line break, an
    image) follow the text, so none is lost. The result always passes
    ``validate``: balanced, properly nested, every id once.
    """
    content: list[Token] = []
    for token in output.tokens:
        if isinstance(token, Text):
            if content and isinstance(content[-1], Text):
                content[-1] = Text(content[-1].value + token.value)
            else:
                content.append(token)
        elif isinstance(token, Entity):
            content.append(token)
    structure = tag_structure(source)
    spans = [m.id for m in structure if isinstance(m, Open)]
    voids = [m for m in structure if isinstance(m, Void)]
    tokens = (
        *(Open(i) for i in spans),
        *content,
        *(Close(i) for i in reversed(spans)),
        *voids,
    )
    validate(tokens)
    return Segment(tokens)
