CREATE OR REPLACE FUNCTION verify_transaction_entries_record()
RETURNS TRIGGER AS $$
BEGIN
IF NOT EXISTS (
   SELECT 1
   FROM entries
   WHERE entries.id = NEW.id
) THEN
   RAISE EXCEPTION 'Transaction integrity violated: Transaction %s must have at least one record in transactions', NEW.id;
   END IF;

   RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE CONSTRAINT TRIGGER tbl_transaction_entries_integrity
AFTER INSERT ON entries
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION verify_transaction_entries_record();
