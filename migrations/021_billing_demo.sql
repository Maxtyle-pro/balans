SET search_path=balans,pg_catalog;
CREATE TABLE billing_demo (
 user_id uuid PRIMARY KEY REFERENCES users ON DELETE CASCADE,
 allowed boolean NOT NULL DEFAULT false, enabled boolean NOT NULL DEFAULT false,
 generation uuid NOT NULL DEFAULT gen_random_uuid(), stars integer NOT NULL DEFAULT 100 CHECK(stars BETWEEN 1 AND 10000),
 trial_started_at timestamptz, trial_until timestamptz, trial_quotas jsonb,
 paid_at timestamptz, paid_until timestamptz, paid_quotas jsonb,
 checkout_id uuid, checkout_at timestamptz, checkout_state text CHECK(checkout_state IN ('pending','paid','cancelled')),
 auto_renew boolean NOT NULL DEFAULT true
);
ALTER TABLE billing_demo ENABLE ROW LEVEL SECURITY;
ALTER TABLE billing_demo FORCE ROW LEVEL SECURITY;
CREATE POLICY own ON billing_demo USING(user_id=actor_user_id()) WITH CHECK(user_id=actor_user_id());
-- Simulation entitlements are private to an explicitly provisioned tester's
-- personal budget. No fake rows enter the real invoice/payment ledger.
ALTER FUNCTION billing_access(uuid) RENAME TO billing_access_live;
CREATE FUNCTION billing_access(space uuid DEFAULT current_workspace()) RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=balans,pg_temp AS $$
DECLARE access jsonb;demo billing_demo;w workspaces;status text;q jsonb;started timestamptz;ending timestamptz;
BEGIN
 access:=billing_access_live(space);
 SELECT * INTO w FROM workspaces WHERE id=space;
 IF w.kind<>'personal' OR w.owner_user_id<>actor_user_id() OR access->>'status'='suspended' THEN RETURN access;END IF;
 SELECT * INTO demo FROM billing_demo WHERE user_id=actor_user_id() AND allowed AND enabled;
 IF NOT FOUND THEN RETURN access;END IF;
 IF demo.paid_until>now() THEN status:='active';q:=demo.paid_quotas;started:=demo.paid_at;ending:=demo.paid_until;
 ELSIF demo.trial_until>now() THEN status:='trial';q:=demo.trial_quotas;started:=demo.trial_started_at;ending:=demo.trial_until;
 ELSIF demo.trial_started_at IS NULL AND demo.paid_at IS NULL THEN status:='not_started';
 ELSE status:='expired';ending:=greatest(demo.trial_until,demo.paid_until);END IF;
 RETURN jsonb_build_object('status',status,'until',ending,'sponsor',actor_user_id(),'quotas',q,
  'period_start',started,'period_end',ending,'demo',true);
END $$;
REVOKE ALL ON FUNCTION billing_access(uuid) FROM PUBLIC;
