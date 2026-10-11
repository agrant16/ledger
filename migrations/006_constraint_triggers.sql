CREATE OR REPLACE FUNCTION verify_transactions_has_record()
RETURNS TRIGGER AS $$
BEGIN
IF NOT EXISTS (
   SELECT 1
   FROM transactions
   WHERE transactions.id = NEW.id
) THEN
   RAISE EXCEPTION 'Transaction integrity violated: Transaction %s must have at least one record in transactions', NEW.id;
   END IF;

   RETURN NEW;
END;
$$ LANGUAGE plpgsql

CREATE CONSTRAINT TRIGGER tbl_transaction_has_record_integrity
AFTER INSERT OR UPDATE ON transactions
DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW
EXECUTE FUNCTION verify_transaction_has_record();
