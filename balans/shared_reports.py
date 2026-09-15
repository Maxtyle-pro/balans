from datetime import date,datetime,timedelta,timezone
from decimal import Decimal,ROUND_HALF_UP
from uuid import UUID
from zoneinfo import ZoneInfo
from psycopg.types.json import Jsonb
from balans.report_data import period,summarize,amount,MAX_ROWS

class SharedReports:
    def _shared_funds_snapshot(self,c,start,end,member):
        members=c.execute("SELECT *,telegram_user_id AS telegram_id FROM memberships WHERE workspace_id=current_workspace() AND role='participant' AND (%s::bigint IS NULL OR telegram_user_id=%s) ORDER BY telegram_user_id LIMIT 101",(member,member)).fetchall()
        if len(members)>100:raise ValueError('В отчёте больше 100 участников. Выберите участника: /report all | member=Telegram_ID')
        result=[]
        for m in members:
            def balance(before):
                return c.execute("SELECT coalesce(sum(delta),0) AS value FROM (SELECT p.delta FROM postings p JOIN journal_entries e ON e.id=p.entry_id JOIN accounts a ON a.id=p.account_id WHERE a.responsible_membership_id=%s AND e.effective_on<%s UNION ALL SELECT f.delta FROM fund_entries f JOIN accounts a ON a.id=f.account_id WHERE a.responsible_membership_id=%s AND f.effective_on<%s) values_",(m['id'],before,m['id'],before)).fetchone()['value']
            flow=c.execute("SELECT coalesce(sum(-e.delta) FILTER(WHERE e.event_type='sent' AND f.recipient_user_id=%s),0) AS issued,coalesce(sum(e.delta) FILTER(WHERE e.event_type='received' AND f.recipient_user_id=%s),0) AS received,coalesce(sum(-e.delta) FILTER(WHERE e.event_type='sent' AND f.sender_user_id=%s),0) AS returned FROM fund_entries e JOIN fund_transfers f ON f.id=e.transfer_id WHERE e.account_id IS NOT NULL AND e.effective_on BETWEEN %s AND %s",(m['user_id'],m['user_id'],m['user_id'],start,end)).fetchone()
            transit=c.execute("SELECT coalesce(sum(e.delta),0) AS value FROM fund_entries e JOIN fund_transfers f ON f.id=e.transfer_id WHERE e.account_id IS NULL AND f.recipient_user_id=%s AND e.effective_on<=%s",(m['user_id'],end)).fetchone()['value']
            pending=c.execute("SELECT coalesce(sum(amount),0) AS value FROM fund_claims WHERE author_user_id=%s AND state='pending' AND occurred_on<=%s",(m['user_id'],end)).fetchone()['value']
            reviewed=c.execute("SELECT coalesce(sum(r.amount) FILTER(WHERE coalesce(ds.status,'unreviewed')<>'accepted'),0) AS unreviewed FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id LEFT JOIN document_sets ds ON ds.id=o.id WHERE o.created_by_user_id=%s AND o.kind='expense' AND o.state='active' AND r.occurred_on BETWEEN %s AND %s",(m['user_id'],start,end)).fetchone()['unreviewed']
            closing=balance(end+timedelta(days=1))
            result.append({'participant':str(m['telegram_id']),'opening':str(balance(start)),'closing':str(closing),'declared':str(closing+pending),'issued':str(flow['issued']),'received':str(flow['received']),'returned':str(flow['returned']),'transit':str(transit),'pending':str(pending),'unreviewed':str(reviewed)})
        transit=c.execute('SELECT coalesce(sum(e.delta),0) AS value FROM fund_entries e JOIN fund_transfers f ON f.id=e.transfer_id WHERE e.account_id IS NULL AND e.effective_on<=%s AND (%s::bigint IS NULL OR f.sender_telegram_id=%s OR f.recipient_telegram_id=%s)',(end,member,member,member)).fetchone()['value']
        return {'participants':result,'transit':str(transit)}

    def _report_snapshot(self,c,user_id,arg,sent_at):
        pieces=[p.strip() for p in arg.split('|')];scope=pieces[0];member=None
        for piece in pieces[1:]:
            key,sep,value=piece.partition('=')
            if not sep or key!='member':raise ValueError('/report all | member=Telegram_ID либо /report месяц')
            member=int(value)
            if not 0<member<2**63:raise ValueError('Telegram ID должен быть положительным числом.')
        zone=self._account_context(c)['timezone'];today=sent_at.astimezone(ZoneInfo(zone)).date()
        if scope.lower()=='all':
            earliest=c.execute("SELECT min(day) AS day FROM (SELECT r.occurred_on AS day FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id JOIN memberships m ON m.id=o.responsible_membership_id WHERE (%s::bigint IS NULL OR m.telegram_user_id=%s) UNION ALL SELECT occurred_on FROM fund_claims WHERE (%s::bigint IS NULL OR author_telegram_id=%s) UNION ALL SELECT occurred_on FROM fund_transfers WHERE (%s::bigint IS NULL OR sender_telegram_id=%s OR recipient_telegram_id=%s)) dates",(member,member,member,member,member,member,member)).fetchone()['day']
            start,end=earliest or today,today
            try:previous_end=start-timedelta(days=1);previous_start=previous_end-timedelta(days=(end-start).days)
            except OverflowError:raise ValueError('Дата слишком ранняя для сравнения. Укажите ограниченный период.') from None
        else:start,end,previous_start,previous_end=period(scope,today)
        def rows(a,b):
            result=c.execute("SELECT o.id,r.occurred_on,r.amount,r.currency,r.base_amount,r.exchange_rate,r.exchange_rate_on,r.rate_source,r.description,r.merchant,r.source_kind,o.kind,r.opening_negative,r.refund_of,coalesce(parent_cat.name,cat.name,'—') AS category,a.name AS account_name,dest.name AS destination_account,m.telegram_user_id AS participant,r.revision_no,coalesce(ds.status,'unreviewed') AS review_status,(SELECT count(*) FROM documents d WHERE d.set_id=o.id AND d.state='active' AND (d.expires_at IS NULL OR d.expires_at>now())) AS document_count FROM operations o JOIN operation_revisions r ON r.id=o.current_revision_id JOIN memberships m ON m.id=o.responsible_membership_id LEFT JOIN categories cat ON cat.id=r.category_id LEFT JOIN operations parent ON parent.id=r.refund_of LEFT JOIN operation_revisions parent_rev ON parent_rev.id=parent.current_revision_id LEFT JOIN categories parent_cat ON parent_cat.id=parent_rev.category_id JOIN accounts a ON a.id=r.account_id LEFT JOIN accounts dest ON dest.id=r.destination_account_id LEFT JOIN document_sets ds ON ds.id=o.id WHERE o.state='active' AND r.currency=coalesce(nullif(current_setting('balans.report_currency',true),''),'RUB') AND r.occurred_on BETWEEN %s AND %s AND (%s::bigint IS NULL OR m.telegram_user_id=%s) ORDER BY r.occurred_on,o.id LIMIT %s",(a,b,member,member,MAX_ROWS+1)).fetchall()
            external=c.execute("SELECT cl.id,cl.occurred_on,cl.amount,a.currency,cl.purpose AS description,cl.source AS merchant,'manual' AS source_kind,'income' AS kind,false AS opening_negative,NULL AS refund_of,'Внешнее поступление' AS category,a.name AS account_name,NULL AS destination_account,cl.author_telegram_id AS participant,1 AS revision_no,coalesce(ds.status,'unreviewed') AS review_status,(SELECT count(*) FROM documents d WHERE d.set_id=cl.id AND d.state='active' AND (d.expires_at IS NULL OR d.expires_at>now())) AS document_count FROM fund_claims cl JOIN accounts a ON a.id=cl.account_id LEFT JOIN document_sets ds ON ds.id=cl.id WHERE cl.state='external' AND a.currency=coalesce(nullif(current_setting('balans.report_currency',true),''),'RUB') AND cl.occurred_on BETWEEN %s AND %s AND (%s::bigint IS NULL OR cl.author_telegram_id=%s) ORDER BY cl.occurred_on,cl.id LIMIT %s",(a,b,member,member,MAX_ROWS+1)).fetchall()
            transfers=c.execute("SELECT f.id,f.occurred_on,f.amount,a.currency,f.purpose AS description,'' AS merchant,'funds' AS source_kind,'transfer' AS kind,false AS opening_negative,NULL AS refund_of,'—' AS category,coalesce(a.name,'Счёт отправителя') AS account_name,dest.name AS destination_account,CASE WHEN f.sender_user_id=(SELECT owner_user_id FROM workspaces WHERE id=f.workspace_id) THEN f.recipient_telegram_id ELSE f.sender_telegram_id END AS participant,f.version AS revision_no,coalesce(ds.status,'unreviewed') AS review_status,(SELECT count(*) FROM documents d WHERE d.set_id=f.id AND d.state='active' AND (d.expires_at IS NULL OR d.expires_at>now())) AS document_count,f.state AS receipt_status,coalesce((SELECT sum(e.delta) FROM fund_entries e WHERE e.transfer_id=f.id AND e.event_type='received' AND e.account_id IS NOT NULL AND e.effective_on<=%s),0) AS received_amount FROM fund_transfers f LEFT JOIN accounts a ON a.id=f.source_account_id LEFT JOIN accounts dest ON dest.id=f.target_account_id LEFT JOIN document_sets ds ON ds.id=f.id WHERE f.state<>'cancelled' AND a.currency=coalesce(nullif(current_setting('balans.report_currency',true),''),'RUB') AND f.occurred_on BETWEEN %s AND %s AND (%s::bigint IS NULL OR f.sender_telegram_id=%s OR f.recipient_telegram_id=%s) ORDER BY f.occurred_on,f.id LIMIT %s",(b,a,b,member,member,member,MAX_ROWS+1)).fetchall()
            result.extend(external);result.extend(transfers);result.sort(key=lambda r:(r['occurred_on'],str(r['id'])))
            if len(result)>MAX_ROWS:raise ValueError(f'В выбранном или сравниваемом периоде больше {MAX_ROWS} строк. Сократите период или выберите участника.')
            return [{'id':str(r['id']),'date':r['occurred_on'].isoformat(),'amount':str(-r['amount'] if r['kind']=='opening' and r['opening_negative'] else r['amount']),'currency':r.get('currency') or c.execute('SELECT base_currency FROM workspaces WHERE id=current_workspace()').fetchone()['base_currency'],'base_amount':str(r['base_amount']) if r.get('base_amount') is not None else None,'exchange_rate':str(r['exchange_rate']) if r.get('exchange_rate') is not None else None,'exchange_rate_on':str(r['exchange_rate_on']) if r.get('exchange_rate_on') else None,'rate_source':r.get('rate_source'),'description':r['description'],'merchant':r['merchant'] or '','source':r['source_kind'],'category':r['category'],'kind':r['kind'],'account':r['account_name'],'destination_account':r['destination_account'] or '','refund_of':str(r['refund_of']) if r['refund_of'] else '','participant':str(r['participant']),'revision':r['revision_no'],'review_status':r['review_status'],'document_count':r['document_count'],'receipt_status':r.get('receipt_status',''),'received_amount':str(r.get('received_amount',''))} for r in result]
        current=rows(start,end);previous=rows(previous_start,previous_end)
        summary=summarize(current,start,end);past=summarize(previous,previous_start,previous_end);delta=amount(summary['total'])-amount(past['total'])
        workspace=c.execute('SELECT * FROM workspaces WHERE id=current_workspace()').fetchone()
        snapshot={'start':str(start),'end':str(end),'previous_start':str(previous_start),'previous_end':str(previous_end),'timezone':zone,'workspace':workspace['name'],'workspace_kind':workspace['kind'],'filter_member':member,'created_at':datetime.now(timezone.utc).isoformat(),'rows':current,'previous_rows':previous,'currency':c.execute("SELECT coalesce(nullif(current_setting('balans.report_currency',true),''),'RUB') AS c").fetchone()['c'],'summary':summary,'previous':past,'delta':str(delta),'delta_percent':str((delta*100/amount(past['total'])).quantize(Decimal('.1'),rounding=ROUND_HALF_UP)) if amount(past['total']) else None}
        snapshot['requested_period']=arg
        if workspace['kind']=='shared' and snapshot['currency']==workspace['base_currency']:snapshot['funds']=self._shared_funds_snapshot(c,start,end,member)
        return c.execute('INSERT INTO reports(workspace_id,author_user_id,snapshot) VALUES(%s,%s,%s) RETURNING *',(workspace['id'],user_id,Jsonb(snapshot))).fetchone()

    def _resolve_report(self,actor,identity,format):
        if format!='auditcsv':return super()._resolve_report(actor,identity,format)
        import base64
        import csv
        from io import StringIO
        from balans.domain import Reply
        with self._actor_transaction(actor) as c:
            report=self._get_report(c,identity)
            if not report:return Reply('Отчёт недоступен или истёк.')
            c.execute("SELECT set_config('balans.workspace_id',%s,true)",(str(report['workspace_id']),))
            chosen=[{'id':r['id'],'revision':r.get('revision',1)} for r in report['snapshot']['rows']]
            rows=c.execute("SELECT r.operation_id,r.revision_no,r.occurred_on,r.amount,r.currency,r.base_amount,r.exchange_rate,r.exchange_rate_on,r.rate_source,r.description,r.change_reason,r.created_at FROM operation_revisions r JOIN jsonb_to_recordset(%s) AS chosen(id uuid,revision integer) ON chosen.id=r.operation_id WHERE r.revision_no<=chosen.revision ORDER BY r.operation_id,r.revision_no LIMIT 10001",(Jsonb(chosen),)).fetchall()
            if len(rows)>10000:return Reply('Больше 10 000 версий. Сократите период или выберите участника.')
        out=StringIO(newline='');writer=csv.writer(out)
        writer.writerow(['ID операции','Версия','Дата операции','Сумма','Валюта','Сумма в базовой валюте','Курс','Дата курса','Источник курса','Описание','Причина','Создана UTC'])
        for r in rows:writer.writerow([("'"+str(x) if str(x).lstrip().startswith(('=','+','-','@')) else x) for x in r.values()])
        return Reply('История версий операций до состояния выбранного снимка.',generated_document=base64.b64encode(('\ufeff'+out.getvalue()).encode()).decode(),generated_filename='balans-revisions.csv')
