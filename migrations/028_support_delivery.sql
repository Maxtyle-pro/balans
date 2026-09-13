SET search_path TO balans, public;
ALTER TABLE support_tickets ADD COLUMN delivered_at timestamptz DEFAULT now(),
 ADD COLUMN delivery_after timestamptz NOT NULL DEFAULT now();
ALTER TABLE support_tickets ALTER COLUMN delivered_at DROP DEFAULT;
CREATE POLICY delivery ON support_tickets USING(current_setting('balans.support_worker',true)='on') WITH CHECK(current_setting('balans.support_worker',true)='on');
