"""PostgreSQL job queue with SKIP LOCKED claims and lease sweeping (S2.4, FR-155).

Same contract as :class:`orchestrator.queue.jobs.InMemoryQueue`. Every
statement is short; claims lock only the rows they take, so many workers can
run concurrently without blocking each other.
"""

from __future__ import annotations

from typing import Any

from orchestrator.queue.jobs import (
    FAILURE_MEMORY_SECONDS,
    VALIDATION_FAILURE_PREFIX,
    ClaimedJob,
    Job,
    backoff_seconds,
)

_ENQUEUE = """
INSERT INTO translation_job
  (machine_key, segment_key, approved_key, masked_source, gfp, term_ids, site_id,
   model_version, priority, max_attempts)
SELECT %(machine_key)s, %(segment_key)s, %(approved_key)s, %(masked_source)s, %(gfp)s,
       %(term_ids)s, %(site_id)s, %(model_version)s, %(priority)s, %(max_attempts)s
 WHERE NOT EXISTS (
         SELECT 1 FROM translation_version v
          WHERE v.lookup_key = %(machine_key)s AND v.origin = 'mt' AND v.invalidated_at IS NULL)
   AND NOT EXISTS (
         SELECT 1 FROM translation_job f
          WHERE f.machine_key = %(machine_key)s AND f.state = 'failed'
            AND f.last_error LIKE %(validation_prefix)s
            AND f.finished_at > now() - make_interval(secs => %(memory)s))
ON CONFLICT (machine_key) WHERE state IN ('pending', 'running') DO NOTHING
"""

_CLAIM = """
WITH ready AS (
  SELECT id FROM translation_job
   WHERE state = 'pending' AND available_at <= now()
   ORDER BY priority, available_at, id
   FOR UPDATE SKIP LOCKED
   LIMIT %(limit)s)
UPDATE translation_job j
   SET state = 'running', claimed_by = %(worker)s, attempts = j.attempts + 1,
       lease_until = now() + make_interval(secs => %(lease)s)
  FROM ready WHERE j.id = ready.id
RETURNING j.id, j.attempts, j.machine_key, j.segment_key, j.approved_key, j.masked_source,
          j.gfp, j.term_ids, j.site_id, j.model_version, j.priority
"""


class PostgresJobQueue:
    def __init__(self, conn: Any, *, max_depth: int = 10_000, max_attempts: int = 5) -> None:
        self.conn = conn  # psycopg.Connection, autocommit=True
        self.max_depth = max_depth
        self.max_attempts = max_attempts

    def depth(self) -> int:
        (n,) = self.conn.execute(
            "SELECT count(*) FROM translation_job WHERE state = 'pending'"
        ).fetchone()
        return int(n)

    def enqueue(self, job: Job) -> bool:
        if self.depth() >= self.max_depth:
            already = self.conn.execute(
                "SELECT 1 FROM translation_job WHERE machine_key = %s"
                " AND state IN ('pending', 'running')",
                (job.machine_key,),
            ).fetchone()
            return already is not None  # a duplicate of queued work is not "full"
        self.conn.execute(
            _ENQUEUE,
            {
                "machine_key": job.machine_key,
                "segment_key": job.segment_key,
                "approved_key": job.approved_key,
                "masked_source": job.masked_source,
                "gfp": job.gfp,
                "term_ids": list(job.term_ids),
                "site_id": job.site_id,
                "model_version": job.model_version,
                "priority": job.priority,
                "max_attempts": self.max_attempts,
                "validation_prefix": VALIDATION_FAILURE_PREFIX + "%",
                "memory": FAILURE_MEMORY_SECONDS,
            },
        )
        return True

    def claim(self, worker_id: str, limit: int, lease_seconds: float) -> list[ClaimedJob]:
        rows = self.conn.execute(
            _CLAIM, {"limit": limit, "worker": worker_id, "lease": lease_seconds}
        ).fetchall()
        return [
            ClaimedJob(
                id=r[0],
                attempts=r[1],
                job=Job(
                    machine_key=r[2],
                    segment_key=r[3],
                    approved_key=r[4],
                    masked_source=r[5],
                    gfp=r[6],
                    term_ids=tuple(r[7]),
                    site_id=r[8],
                    model_version=r[9],
                    priority=r[10],
                ),
            )
            for r in rows
        ]

    def complete(self, job_id: int, worker_id: str) -> None:
        self.conn.execute(
            "UPDATE translation_job SET state = 'done', finished_at = now(),"
            " claimed_by = NULL, lease_until = NULL"
            " WHERE id = %s AND claimed_by = %s AND state = 'running'",
            (job_id, worker_id),
        )

    def fail(self, job_id: int, worker_id: str, error: str, *, retry: bool) -> None:
        with self.conn.transaction():
            row = self.conn.execute(
                "SELECT attempts, max_attempts FROM translation_job"
                " WHERE id = %s AND claimed_by = %s AND state = 'running' FOR UPDATE",
                (job_id, worker_id),
            ).fetchone()
            if row is None:
                return
            attempts, max_attempts = row
            if retry and attempts < max_attempts:
                self.conn.execute(
                    "UPDATE translation_job SET state = 'pending', claimed_by = NULL,"
                    " lease_until = NULL, last_error = %s,"
                    " available_at = now() + make_interval(secs => %s) WHERE id = %s",
                    (error, backoff_seconds(attempts), job_id),
                )
            else:
                self.conn.execute(
                    "UPDATE translation_job SET state = 'failed', claimed_by = NULL,"
                    " lease_until = NULL, last_error = %s, finished_at = now() WHERE id = %s",
                    (error, job_id),
                )

    def sweep(self) -> int:
        cur = self.conn.execute(
            "UPDATE translation_job"
            "   SET state = CASE WHEN attempts >= max_attempts THEN 'failed' ELSE 'pending' END,"
            "       finished_at = CASE WHEN attempts >= max_attempts THEN now() END,"
            "       available_at = now(), claimed_by = NULL, lease_until = NULL,"
            "       last_error = 'lease expired'"
            " WHERE state = 'running' AND lease_until < now()"
        )
        return int(cur.rowcount)
