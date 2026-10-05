-- 0006: indexes for review flagging and queue health (S3.1, S2.3). FR-511, FR-611.
--
-- Separate from 0005 so the exclusive lock 0005 takes on review_item and
-- translation_job is released before any index is built.
SET LOCAL lock_timeout = '5s';

-- The cap is a counting query on the write path of every flagged translation.
CREATE INDEX review_item_request_recent ON review_item (site_id, created_at)
  WHERE raised_by = 'request';

-- Reviewer queue: oldest pending first, per site (S7.1 reads this). Owed
-- items are released from the same index.
CREATE INDEX review_item_open ON review_item (site_id, state, created_at)
  WHERE state IN ('pending_review', 'owed');

-- Age of the oldest waiting job, read by every health check and metrics scrape.
CREATE INDEX translation_job_pending_created ON translation_job (created_at)
  WHERE state = 'pending';

-- Per-site daily ceiling on error reports (FR-432), counted on every report
-- that passes the limiter.
CREATE INDEX error_report_site_recent ON error_report (site_id, created_at);
