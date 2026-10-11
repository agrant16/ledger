-- migrations/006_constraint_triggers.sql

-- Adds constraint triggers for verifying a new transaction record has at least
-- one record in entries.
CREATE FUNCTION verify_transaction_entries_record()
RETURNS TRIGGER AS $$
BEGIN
IF NOT EXISTS (
   SELECT 1
   FROM entries
   WHERE entries.transaction_id = NEW.id
) THEN
   RAISE EXCEPTION 'Transaction integrity violated: Transaction % must have at least one record in entries', NEW.id
    USING ERRCODE = 'check_violation';
   END IF;

   RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER tbl_transaction_entries_integrity
AFTER INSERT ON entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION verify_transaction_entries_record();
