-- Restore the original six-column enumeration from 0002.
-- Downgrading re-opens the gap on version, aggregate_type, correlation_id,
-- causation_id, actor_id and trace_id. Only downgrade to reach an earlier
-- revision, never as a way of loosening the guard.

CREATE OR REPLACE FUNCTION jobforge_events_publish_only() RETURNS TRIGGER AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'events is append-only; DELETE is not permitted'
      USING ERRCODE = 'raise_exception';
  END IF;
  IF NEW.id             IS DISTINCT FROM OLD.id
     OR NEW.type        IS DISTINCT FROM OLD.type
     OR NEW.payload     IS DISTINCT FROM OLD.payload
     OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
     OR NEW.aggregate_id IS DISTINCT FROM OLD.aggregate_id
     OR NEW.actor_type  IS DISTINCT FROM OLD.actor_type
  THEN
    RAISE EXCEPTION 'events rows are immutable except for publication state'
      USING ERRCODE = 'raise_exception';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
