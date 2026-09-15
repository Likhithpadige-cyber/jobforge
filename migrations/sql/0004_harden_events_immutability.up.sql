-- =====================================================================
-- Harden jobforge_events_publish_only().
--
-- The original guard enumerated six columns. The events table has
-- fourteen, so version, aggregate_type, correlation_id, causation_id,
-- actor_id and trace_id were silently mutable. Two of those matter a great
-- deal:
--
--   correlation_id  ties an event to its pipeline run. Rewriting it
--                   detaches an action from the journey it belonged to,
--                   which is precisely how an audit trail stops being one.
--   actor_id        records WHO acted. Rewriting it re-attributes an
--                   agent's action to a human, or vice versa.
--
-- Rather than extend the enumeration - which drifts the moment a column is
-- added - the guard now compares the whole row minus the two legitimately
-- mutable columns. Any column added by a future migration is protected
-- automatically, with no code change and no chance of someone forgetting.
--
-- The exception names the offending columns, because "events rows are
-- immutable" without saying which field moved is an unpleasant debug.
--
-- The trigger itself is unchanged and still points at this function name,
-- so replacing the body is sufficient.
-- =====================================================================

CREATE OR REPLACE FUNCTION jobforge_events_publish_only() RETURNS TRIGGER AS $$
DECLARE
  old_immutable JSONB;
  new_immutable JSONB;
  changed       TEXT;
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'events is append-only; DELETE is not permitted'
      USING ERRCODE = 'raise_exception';
  END IF;

  -- Everything except publication state must be byte-identical.
  old_immutable := to_jsonb(OLD) - 'published' - 'published_at';
  new_immutable := to_jsonb(NEW) - 'published' - 'published_at';

  IF new_immutable IS DISTINCT FROM old_immutable THEN
    SELECT string_agg(key, ', ' ORDER BY key) INTO changed
    FROM jsonb_each(old_immutable) AS o(key, value)
    WHERE o.value IS DISTINCT FROM (new_immutable -> o.key);

    RAISE EXCEPTION
      'events rows are immutable except for publication state; '
      'attempted to modify: %', COALESCE(changed, '(column set changed)')
      USING ERRCODE = 'raise_exception';
  END IF;

  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
