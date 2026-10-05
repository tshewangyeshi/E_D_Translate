-- 0004: audit trail (S3.3, spec §2.6). FR-620.
--
-- Who changed what the service will say, and when. Termbase publishes, review
-- approvals, offline seed imports, tier-rule changes and enrolment changes each
-- leave one row here.
--
-- What the database enforces, and what it does not:
--
--   * UPDATE, DELETE and TRUNCATE are refused by trigger, for every role.
--   * On INSERT, `id` and `at` are set here, whatever the caller supplied, so
--     a record cannot be backdated or slotted in between two others.
--   * The table's OWNER can still disable the triggers, rewrite rows and
--     enable them again without leaving a trace. Today the service migrates
--     with its own role, so it is the owner. Closing that needs a separate
--     owner role for migrations and a runtime role with INSERT and SELECT
--     only: a deployment decision, recorded in TODOS.md.
--
-- `detail` holds identifiers, versions and counts. Never segment text, and
-- never anything that identifies a citizen (NFR-303): the application refuses
-- values that do not look like identifiers before they reach this table.

CREATE TABLE audit_event (
  id       BIGSERIAL PRIMARY KEY,
  actor    TEXT NOT NULL CHECK (btrim(actor) <> ''),     -- reviewer ref or operator
  action   TEXT NOT NULL CHECK (action IN
             ('termbase.publish', 'review.approve', 'seed.import',
              'tier_rule.change', 'site.enrolment_change')),
  subject  TEXT NOT NULL CHECK (btrim(subject) <> ''),   -- what was changed
  detail   JSONB NOT NULL DEFAULT '{}'::jsonb,
  at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "What happened to this segment / term / site", newest first; also serves
-- the newest-row-per-subject read the site audit makes at start.
CREATE INDEX audit_event_subject ON audit_event (subject, id DESC);
CREATE INDEX audit_event_action ON audit_event (action, id DESC);

CREATE FUNCTION audit_event_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_event is append-only (FR-620)';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_event_no_rewrite
  BEFORE UPDATE OR DELETE ON audit_event
  FOR EACH ROW EXECUTE FUNCTION audit_event_append_only();

CREATE TRIGGER audit_event_no_truncate
  BEFORE TRUNCATE ON audit_event
  FOR EACH STATEMENT EXECUTE FUNCTION audit_event_append_only();

CREATE FUNCTION audit_event_stamp() RETURNS trigger AS $$
BEGIN
  NEW.id := nextval(pg_get_serial_sequence('audit_event', 'id'));
  NEW.at := now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_event_stamp
  BEFORE INSERT ON audit_event
  FOR EACH ROW EXECUTE FUNCTION audit_event_stamp();
