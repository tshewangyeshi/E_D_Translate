-- 0002: background translation queue (spec §2.10, ER-3). FR-155, FR-156, NFR-412.
-- Jobs carry MASKED text only (FR-143).

CREATE TABLE translation_job (
  id             BIGSERIAL PRIMARY KEY,
  machine_key    CHAR(64) NOT NULL,
  segment_key    CHAR(64) NOT NULL,
  approved_key   CHAR(64) NOT NULL,
  masked_source  TEXT NOT NULL,
  gfp            CHAR(64) NOT NULL,
  term_ids       TEXT[] NOT NULL DEFAULT '{}',
  site_id        TEXT NOT NULL,
  model_version  TEXT NOT NULL,
  priority       SMALLINT NOT NULL DEFAULT 100,   -- lower runs sooner
  state          TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending', 'running', 'done', 'failed')),
  attempts       INT NOT NULL DEFAULT 0,
  max_attempts   INT NOT NULL DEFAULT 5,
  available_at   TIMESTAMPTZ NOT NULL DEFAULT now(),  -- retry backoff
  claimed_by     TEXT,
  lease_until    TIMESTAMPTZ,
  last_error     TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at    TIMESTAMPTZ
);

-- At most one ACTIVE job per key; finished jobs don't block re-enqueueing
-- after an invalidation (ER-3).
CREATE UNIQUE INDEX translation_job_active_key
  ON translation_job (machine_key) WHERE state IN ('pending', 'running');

-- Claim order for workers.
CREATE INDEX translation_job_ready
  ON translation_job (priority, available_at, id) WHERE state = 'pending';

-- Sweeper: expired leases of crashed workers.
CREATE INDEX translation_job_leases
  ON translation_job (lease_until) WHERE state = 'running';

-- Negative cache: recent validation failures are not re-queued on every page view.
CREATE INDEX translation_job_recent_failures
  ON translation_job (machine_key, finished_at) WHERE state = 'failed';
