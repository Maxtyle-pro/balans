"""Apply versioned SQL using a separate, non-superuser schema owner."""
import argparse
import hashlib
import os
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from dotenv import load_dotenv


def migrate(dsn: str, runtime_role: str) -> None:
    with psycopg.connect(dsn) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(82024002)")
        if conn.execute("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()[0]:
            raise RuntimeError("Миграции требуют отдельного владельца БД без SUPERUSER/BYPASSRLS")
        conn.execute("CREATE TABLE IF NOT EXISTS public.schema_migrations (name text PRIMARY KEY, checksum text NOT NULL)")
        for path in sorted((Path(__file__).resolve().parent.parent / 'migrations').glob('*.sql')):
            source = path.read_text()
            checksum = hashlib.sha256(source.encode()).hexdigest()
            existing = conn.execute("SELECT checksum FROM public.schema_migrations WHERE name=%s", (path.name,)).fetchone()
            if existing:
                if existing[0] != checksum:
                    raise RuntimeError(f"Изменена уже применённая миграция {path.name}")
                continue
            conn.execute(source)
            conn.execute("INSERT INTO public.schema_migrations VALUES (%s,%s)", (path.name, checksum))
        role = sql.Identifier(runtime_role)
        conn.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION balans.billing_access_live(uuid) FROM {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(enabled,generation,trial_started_at,trial_until,trial_quotas,paid_at,paid_until,paid_quotas,checkout_id,checkout_at,checkout_state,auto_renew) ON balans.billing_demo TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.billing_usage() TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.create_currency_account(text,text),balans.create_currency_workspace(text,text) TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.exchange_rates TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.confirm_erasure(uuid),balans.erase_account(uuid),balans.schedule_retention() TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.erasure_requests,balans.file_purge_queue TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(keep_personal_originals,service_consent_version,service_consented_at) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("REVOKE EXECUTE ON FUNCTION balans.bootstrap_before_admin(),balans.create_workspace_before_admin(text) FROM {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.admin_role(),balans.record_activity() TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.admin_login_codes,balans.admin_sessions,balans.admin_otp_used,balans.support_tickets,balans.diagnostic_grants,balans.billing_accounts,balans.service_content,balans.admin_roles TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE ON balans.billing_config,balans.ai_configuration TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT ON balans.admin_audit TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.billing_refund_requests TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.telegram_inbox TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.billing_access(uuid),balans.require_paid_access(uuid) TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.billing_invoices,balans.billing_payments TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT ON balans.billing_audit TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.spending_budgets,balans.budget_alerts,balans.notification_preferences,balans.notification_events,balans.notification_outbox TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(budget_start_day) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.share_drafts TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.media_queues TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.require_open_period(date),balans.ensure_document_set(text,uuid),balans.add_document(uuid,uuid,text,integer,text,uuid,uuid),balans.review_record(uuid,text,text),balans.set_closed_period(date,date,boolean,text),balans.delete_document(uuid,boolean,text),balans.document_policy(boolean,numeric,uuid),balans.document_quota_policy(integer,integer) TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.document_uploads TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.create_fund_transfer(bigint,uuid,numeric,date,text,date),balans.fund_action(uuid,text,integer,numeric,uuid,text),balans.create_fund_claim(uuid,numeric,date,text,text),balans.reconcile_claim(uuid,text,text,uuid) TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.fund_confirmations TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.current_workspace(),balans.owns_workspace(uuid),balans.create_workspace(text),balans.new_invitation(),balans.join_workspace(uuid,boolean),balans.decide_invitation(uuid,boolean),balans.remove_membership(uuid),balans.revoke_invitation(uuid) TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(selected_workspace_id) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.manage_category(text,text,text) TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE ON balans.input_batches TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.create_account(text) TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(default_account_id) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT ON balans.reports TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT, UPDATE ON balans.report_jobs, balans.sheets_connections TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT, UPDATE ON balans.voice_jobs TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(voice_enabled,voice_consent_version,voice_consented_at) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT, UPDATE ON balans.receipt_batches TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT ON balans.receipt_files, balans.receipt_items TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(receipts_enabled,receipts_consent_version,receipts_consented_at) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT, UPDATE ON balans.ai_jobs, balans.category_rules, balans.category_feedback, balans.category_actions TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(ai_enabled,ai_consent_version,ai_consented_at) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.change_category(uuid) TO {}").format(role))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA balans TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT,UPDATE,DELETE ON balans.ui_inputs TO {}").format(role))
        conn.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA balans TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT, UPDATE ON balans.operation_drafts TO {}").format(role))
        conn.execute(sql.SQL("GRANT INSERT ON balans.telegram_updates TO {}").format(role))
        conn.execute(sql.SQL("GRANT UPDATE(timezone) ON balans.user_settings TO {}").format(role))
        conn.execute(sql.SQL("GRANT EXECUTE ON FUNCTION balans.actor_telegram_id(), balans.actor_user_id(), balans.bootstrap(), balans.save_expense(uuid) TO {}").format(role))


if __name__ == '__main__':
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime-role', help='По умолчанию — user из DATABASE_URL')
    args = parser.parse_args()
    runtime_role=args.runtime_role or conninfo_to_dict(os.environ['DATABASE_URL']).get('user')
    if not runtime_role:
        parser.error('Укажите --runtime-role или user в DATABASE_URL')
    migrate(os.environ['MIGRATION_DATABASE_URL'], runtime_role)
    print('Миграции применены.')
