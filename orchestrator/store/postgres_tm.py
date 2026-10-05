"""PostgreSQL translation memory. Same contract as :class:`orchestrator.store.tm.InMemoryTM`.

Requirements: FR-410, FR-411, FR-412, FR-143, FR-153, FR-154, FR-511, NFR-305.
Uses psycopg 3; each method is one transaction; ``lookup`` is ONE query (ER-21).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime
from typing import Any, cast

from orchestrator.store.models import (
    FlagOutcome,
    Invalidation,
    MigrationReport,
    Origin,
    RaisedBy,
    ReviewItem,
    ReviewState,
    Stored,
    TranslationVersion,
)
from orchestrator.store.tm import RekeyFn

_LOOKUP = """
SELECT 'a' AS kind, r.approved_key AS key, v.id, v.origin, v.masked_target, TRUE
  FROM review_item r JOIN translation_version v ON v.id = r.current_version
 WHERE r.state = 'approved' AND r.approved_key = ANY(%(approved)s)
UNION ALL
(SELECT DISTINCT ON (v.lookup_key) 'm', v.lookup_key, v.id, v.origin, v.masked_target,
        EXISTS (SELECT 1 FROM review_item r WHERE r.segment_key = v.segment_key)
   FROM translation_version v
  WHERE v.origin = 'mt' AND v.invalidated_at IS NULL AND v.lookup_key = ANY(%(machine)s)
  ORDER BY v.lookup_key, v.id DESC)
"""

# One statement: re-point an open item, or open a new one -- pending, or owed
# when the site is past its cap. Two requests racing at the cap boundary can
# both open a pending item: the cap is a flood limit, not a quota, and one
# over costs a reviewer nothing.
_FLAG = """
WITH existing AS (
  SELECT 1 FROM review_item WHERE segment_key = %(segment_key)s
), repoint AS (
  UPDATE review_item SET current_version = %(version_id)s, updated_at = now()
   WHERE segment_key = %(segment_key)s AND state IN ('pending_review', 'owed')
     AND current_version < %(version_id)s
  RETURNING 1
), opened AS (
  SELECT count(*) AS n FROM review_item
   WHERE site_id = %(site_id)s AND raised_by = 'request' AND state <> 'owed'
     AND created_at >= %(since)s
), inserted AS (
  INSERT INTO review_item (segment_key, state, current_version, site_id, raised_by)
  SELECT %(segment_key)s,
         CASE WHEN %(cap)s::int IS NOT NULL AND (SELECT n FROM opened) >= %(cap)s::int
              THEN 'owed' ELSE 'pending_review' END,
         %(version_id)s, %(site_id)s, %(raised_by)s
   WHERE NOT EXISTS (SELECT 1 FROM existing)
  ON CONFLICT (segment_key) DO NOTHING
  RETURNING state
)
SELECT (SELECT state FROM inserted), (SELECT count(*) FROM repoint)
"""

# Open items whose version is no longer served ask a reviewer about nothing.
_CLOSE_INVALIDATED = """
DELETE FROM review_item r USING translation_version v
 WHERE v.id = r.current_version AND r.state IN ('pending_review', 'owed')
   AND v.invalidated_at IS NOT NULL
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

    def _tx(self) -> AbstractContextManager[Any]:
        """A transaction, or none when the caller already opened one.

        Nested ``transaction()`` blocks become savepoints, each two more round
        trips. Inside ``TranslationStore.atomic`` the outer transaction already
        makes the work all-or-nothing, so joining it is enough.
        """
        from psycopg.pq import TransactionStatus

        if self.conn.info.transaction_status != TransactionStatus.IDLE:
            return nullcontext()
        return cast(AbstractContextManager[Any], self.conn.transaction())

    def lookup(
        self, approved_keys: Sequence[str], machine_keys: Sequence[str]
    ) -> tuple[dict[str, Stored], dict[str, Stored]]:
        self.lookup_calls += 1
        rows = self.conn.execute(
            _LOOKUP, {"approved": list(approved_keys), "machine": list(machine_keys)}
        ).fetchall()
        approved: dict[str, Stored] = {}
        machine: dict[str, Stored] = {}
        for kind, key, vid, origin, target, reviewed in rows:
            (approved if kind == "a" else machine)[key] = Stored(
                vid, Origin(origin), target, bool(reviewed)
            )
        return approved, machine

    def history(self, segment_key: str) -> list[TranslationVersion]:
        rows = self.conn.execute(
            f"SELECT {_VERSION_COLS} FROM translation_version WHERE segment_key = %s ORDER BY id",  # noqa: S608
            (segment_key,),
        ).fetchall()
        return [_version(r) for r in rows]

    def review_item(self, segment_key: str) -> ReviewItem | None:
        row = self.conn.execute(
            "SELECT segment_key, approved_key, state, current_version, site_id, updated_at,"
            " created_at, raised_by FROM review_item WHERE segment_key = %s",
            (segment_key,),
        ).fetchone()
        if row is None:
            return None
        return ReviewItem(
            row[0], row[1], ReviewState(row[2]), row[3], row[4], row[5], row[6], RaisedBy(row[7])
        )

    def pending_review(self, site_id: str | None = None) -> int:
        return self._count("pending_review", site_id)

    def owed(self, site_id: str | None = None) -> int:
        return self._count("owed", site_id)

    def _count(self, state: str, site_id: str | None) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM review_item WHERE state = %(state)s"
            " AND (%(site)s::text IS NULL OR site_id = %(site)s)",
            {"state": state, "site": site_id},
        ).fetchone()
        return int(row[0]) if row else 0

    def release_owed(self, site_id: str, limit: int) -> int:
        cur = self.conn.execute(
            "UPDATE review_item SET state = 'pending_review', updated_at = now()"
            " WHERE segment_key IN ("
            "   SELECT segment_key FROM review_item WHERE state = 'owed' AND site_id = %s"
            "    ORDER BY created_at, segment_key LIMIT %s FOR UPDATE SKIP LOCKED)",
            (site_id, max(0, limit)),
        )
        return int(cur.rowcount)

    def flag_for_review(
        self,
        *,
        segment_key: str,
        version_id: int,
        site_id: str,
        raised_by: RaisedBy,
        daily_cap: int | None,
        since: datetime,
    ) -> FlagOutcome:
        row = self.conn.execute(
            _FLAG,
            {
                "segment_key": segment_key,
                "version_id": version_id,
                "site_id": site_id,
                "raised_by": raised_by.value,
                "cap": daily_cap,
                "since": since,
            },
        ).fetchone()
        state = row[0] if row else None
        if state == "owed":
            return FlagOutcome.OWED
        return FlagOutcome.CREATED if state == "pending_review" else FlagOutcome.EXISTS

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
        with self._tx():
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
            self.conn.execute(_CLOSE_INVALIDATED)
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

    def count_machine_before(self, before: datetime) -> int:
        row = self.conn.execute(
            "SELECT count(*) FROM translation_version"
            " WHERE origin = 'mt' AND invalidated_at IS NULL AND created_at < %s",
            (before,),
        ).fetchone()
        return int(row[0]) if row else 0

    def expire_machine(self, before: datetime) -> int:
        with self.conn.transaction():
            cur = self.conn.execute(
                "UPDATE translation_version SET invalidated_at = now()"
                " WHERE origin = 'mt' AND invalidated_at IS NULL AND created_at < %s",
                (before,),
            )
            expired = int(cur.rowcount)
            self.conn.execute(_CLOSE_INVALIDATED)
        return expired
