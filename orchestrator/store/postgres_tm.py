"""PostgreSQL translation memory. Same contract as :class:`orchestrator.store.tm.InMemoryTM`.

Requirements: FR-410, FR-411, FR-412, FR-143, FR-153, FR-154, NFR-305.
Uses psycopg 3; each method is one transaction; ``lookup`` is ONE query (ER-21).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from orchestrator.store.models import (
    Invalidation,
    MigrationReport,
    Origin,
    ReviewItem,
    ReviewState,
    Stored,
    TranslationVersion,
)
from orchestrator.store.tm import RekeyFn

_LOOKUP = """
SELECT 'a' AS kind, r.approved_key AS key, v.id, v.origin, v.masked_target
  FROM review_item r JOIN translation_version v ON v.id = r.current_version
 WHERE r.state = 'approved' AND r.approved_key = ANY(%(approved)s)
UNION ALL
(SELECT DISTINCT ON (v.lookup_key) 'm', v.lookup_key, v.id, v.origin, v.masked_target
   FROM translation_version v
  WHERE v.origin = 'mt' AND v.invalidated_at IS NULL AND v.lookup_key = ANY(%(machine)s)
  ORDER BY v.lookup_key, v.id DESC)
"""

_VERSION_COLS = (
    "id, segment_key, lookup_key, origin, masked_target, gfp, model_version, author,"
    " tag_integrity, created_at, invalidated_at"
)


def _version(row: Sequence[Any]) -> TranslationVersion:
    return TranslationVersion(
        id=row[0],
        segment_key=row[1],
        lookup_key=row[2],
        origin=Origin(row[3]),
        masked_target=row[4],
        gfp=row[5],
        model_version=row[6],
        author=row[7],
        tag_integrity=row[8],
        created_at=row[9],
        invalidated_at=row[10],
    )


class PostgresTM:
    def __init__(self, conn: Any) -> None:  # psycopg.Connection, autocommit=True
        self.conn = conn
        self.lookup_calls = 0

    def lookup(
        self, approved_keys: Sequence[str], machine_keys: Sequence[str]
    ) -> tuple[dict[str, Stored], dict[str, Stored]]:
        self.lookup_calls += 1
        rows = self.conn.execute(
            _LOOKUP, {"approved": list(approved_keys), "machine": list(machine_keys)}
        ).fetchall()
        approved: dict[str, Stored] = {}
        machine: dict[str, Stored] = {}
        for kind, key, vid, origin, target in rows:
            (approved if kind == "a" else machine)[key] = Stored(vid, Origin(origin), target)
        return approved, machine

    def history(self, segment_key: str) -> list[TranslationVersion]:
        rows = self.conn.execute(
            f"SELECT {_VERSION_COLS} FROM translation_version WHERE segment_key = %s ORDER BY id",  # noqa: S608
            (segment_key,),
        ).fetchall()
        return [_version(r) for r in rows]

    def review_item(self, segment_key: str) -> ReviewItem | None:
        row = self.conn.execute(
            "SELECT segment_key, approved_key, state, current_version, site_id, updated_at"
            " FROM review_item WHERE segment_key = %s",
            (segment_key,),
        ).fetchone()
        if row is None:
            return None
        return ReviewItem(row[0], row[1], ReviewState(row[2]), row[3], row[4], row[5])

    def _ensure_segment(self, segment_key: str, masked_source: str) -> None:
        self.conn.execute(
            "INSERT INTO segment (segment_key, masked_source) VALUES (%s, %s)"
            " ON CONFLICT (segment_key) DO NOTHING",
            (segment_key, masked_source),
        )

    def _hits(self, term_ids: Iterable[str], segment_key: str, gfp: str) -> None:
        for term in term_ids:
            self.conn.execute(
                "INSERT INTO glossary_hit (term_id, segment_key, gfp) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING",
                (term, segment_key, gfp),
            )

    def store_machine(
        self,
        *,
        segment_key: str,
        masked_source: str,
        machine_key: str,
        masked_target: str,
        gfp: str,
        model_version: str,
        term_ids: Iterable[str],
        tag_integrity: bool | None,
    ) -> Stored:
        with self.conn.transaction():
            self._ensure_segment(segment_key, masked_source)
            (vid,) = self.conn.execute(
                "INSERT INTO translation_version (segment_key, lookup_key, origin,"
                " masked_target, gfp, model_version, tag_integrity)"
                " VALUES (%s, %s, 'mt', %s, %s, %s, %s) RETURNING id",
                (segment_key, machine_key, masked_target, gfp, model_version, tag_integrity),
            ).fetchone()
            self._hits(term_ids, segment_key, gfp)
        return Stored(vid, Origin.MT, masked_target)

    def approve(
        self,
        *,
        segment_key: str,
        masked_source: str,
        approved_key: str,
        masked_target: str,
        gfp: str,
        author: str,
        term_ids: Iterable[str],
        site_id: str | None = None,
    ) -> Stored:
        with self.conn.transaction():
            self._ensure_segment(segment_key, masked_source)
            (vid,) = self.conn.execute(
                "INSERT INTO translation_version"
                " (segment_key, lookup_key, origin, masked_target, gfp, author, tag_integrity)"
                " VALUES (%s, %s, 'human', %s, %s, %s, TRUE) RETURNING id",
                (segment_key, approved_key, masked_target, gfp, author),
            ).fetchone()
            self.conn.execute(
                "INSERT INTO review_item"
                " (segment_key, approved_key, state, current_version, site_id)"
                " VALUES (%s, %s, 'approved', %s, %s)"
                " ON CONFLICT (segment_key) DO UPDATE SET approved_key = EXCLUDED.approved_key,"
                "  state = 'approved', current_version = EXCLUDED.current_version,"
                "  site_id = COALESCE(EXCLUDED.site_id, review_item.site_id), updated_at = now()",
                (segment_key, approved_key, vid, site_id),
            )
            self._hits(term_ids, segment_key, gfp)
        return Stored(vid, Origin.HUMAN, masked_target)

    def invalidate_terms(self, term_ids: Iterable[str]) -> Invalidation:
        terms = list(term_ids)
        with self.conn.transaction():
            machine = self.conn.execute(
                "UPDATE translation_version v SET invalidated_at = now()"
                "  FROM glossary_hit h"
                " WHERE h.term_id = ANY(%s) AND h.segment_key = v.segment_key AND h.gfp = v.gfp"
                "   AND v.origin = 'mt' AND v.invalidated_at IS NULL"
                " RETURNING v.lookup_key",
                (terms,),
            ).fetchall()
            approved = self.conn.execute(
                "UPDATE review_item r SET state = 'needs_recheck', updated_at = now()"
                "  FROM translation_version v, glossary_hit h"
                " WHERE r.state = 'approved' AND v.id = r.current_version"
                "   AND h.term_id = ANY(%s) AND h.segment_key = r.segment_key AND h.gfp = v.gfp"
                " RETURNING r.approved_key, r.segment_key",
                (terms,),
            ).fetchall()
        return Invalidation(
            tuple(sorted({r[0] for r in machine})),
            tuple(sorted({r[0] for r in approved})),
            tuple(sorted({r[1] for r in approved})),
        )

    def migrate_pipeline(self, rekey: RekeyFn) -> MigrationReport:
        rekeyed = rechecked = 0
        with self.conn.transaction():
            rows = self.conn.execute(
                "SELECT r.segment_key, r.approved_key, s.masked_source, v.gfp"
                "  FROM review_item r JOIN segment s USING (segment_key)"
                "  JOIN translation_version v ON v.id = r.current_version"
                " WHERE r.state = 'approved' FOR UPDATE OF r"
            ).fetchall()
            for seg, old_key, masked_source, gfp in rows:
                new_key = rekey(seg, masked_source, gfp)
                if new_key is None:
                    self.conn.execute(
                        "UPDATE review_item SET state = 'needs_recheck', updated_at = now()"
                        " WHERE segment_key = %s",
                        (seg,),
                    )
                    rechecked += 1
                elif new_key != old_key:
                    self.conn.execute(
                        "UPDATE review_item SET approved_key = %s, updated_at = now()"
                        " WHERE segment_key = %s",
                        (new_key, seg),
                    )
                    rekeyed += 1
        return MigrationReport(rekeyed, rechecked)

    def expire_machine(self, before: datetime) -> int:
        cur = self.conn.execute(
            "UPDATE translation_version SET invalidated_at = now()"
            " WHERE origin = 'mt' AND invalidated_at IS NULL AND created_at < %s",
            (before,),
        )
        return int(cur.rowcount)
