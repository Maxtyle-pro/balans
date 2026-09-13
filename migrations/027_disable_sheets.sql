SET search_path=balans,pg_catalog;
ALTER TABLE sheets_connections NO FORCE ROW LEVEL SECURITY;
UPDATE sheets_connections SET state='revoked',challenge='' WHERE state IN ('active','pending');
ALTER TABLE sheets_connections FORCE ROW LEVEL SECURITY;
