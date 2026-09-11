"""Local monitoring JSON; no financial descriptions, credentials or user identifiers."""
import json,os
from datetime import datetime,timezone
from pathlib import Path
from dotenv import load_dotenv
from balans.service import Service


def main():
    load_dotenv();s=Service(os.environ['DATABASE_URL'])
    try:
        bot=int(os.environ['BOT_TOKEN'].split(':',1)[0]);s.check();result=s.queue_health(bot)
        status=Path(os.getenv('BACKUP_DIR','.local/backups'))/'last-success.json'
        result['backup_age_seconds']=None
        if status.exists():result['backup_age_seconds']=(datetime.now(timezone.utc)-datetime.fromisoformat(json.loads(status.read_text())['completed_at'])).total_seconds()
        with s.pool.connection() as c,c.transaction():
            c.execute("SET LOCAL balans.admin_metrics='on'")
            result['ai_failed_1h']=c.execute("SELECT count(*) AS n FROM balans.admin_job_metrics WHERE state='failed' AND updated_at>now()-interval '1 hour'").fetchone()['n']
            c.execute("SET LOCAL balans.notification_worker='on'")
            result['notification_uncertain']=c.execute("SELECT count(*) AS n FROM balans.notification_outbox WHERE state='uncertain'").fetchone()['n']
            config=c.execute('SELECT monthly_cost_limit_usd FROM balans.ai_configuration').fetchone()
            result['ai_cost_limit_usd']=float(config['monthly_cost_limit_usd']) if config['monthly_cost_limit_usd'] is not None else None
            result['ai_estimated_usd']=float(c.execute("SELECT coalesce(sum(estimated_usd),0) AS amount FROM balans.ai_cost_reservations WHERE period_start=date_trunc('month',now() AT TIME ZONE 'UTC')::date AND state IN ('reserved','consumed')").fetchone()['amount']) if result['ai_cost_limit_usd'] is not None else None
        result['healthy']=result['oldest_seconds']<120 and result['failed']==0 and result['ai_failed_1h']<5 and result['notification_uncertain']==0
        if result['ai_cost_limit_usd'] is not None:result['healthy'] &= result['ai_estimated_usd']<result['ai_cost_limit_usd']
        if os.getenv('BACKUP_REQUIRED','false').lower()=='true':result['healthy'] &= result['backup_age_seconds'] is not None and result['backup_age_seconds']<=3600
        print(json.dumps(result));return 0 if result['healthy'] else 1
    finally:s.close()

if __name__=='__main__':
    try:raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({'healthy':False,'error':type(exc).__name__}));raise SystemExit(1) from None
