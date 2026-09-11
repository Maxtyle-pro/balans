"""One-time preservation of available shared receipt originals after migration 010.

Schema-owner transaction reads only migration targets, restores FORCE RLS before
commit. Files are copied afterwards through normal actor-scoped application code.
No financial values, Telegram IDs, filenames or credentials are printed.
"""
import os
import psycopg
from psycopg import sql
from dotenv import load_dotenv
from balans.service import Service


def main():
    load_dotenv()
    tables=('workspaces','memberships','operations','operation_drafts','receipt_files')
    with psycopg.connect(os.environ['MIGRATION_DATABASE_URL']) as c:
        for table in tables:c.execute(sql.SQL('ALTER TABLE balans.{} NO FORCE ROW LEVEL SECURITY').format(sql.Identifier(table)))
        targets=c.execute("SELECT DISTINCT w.id,m.telegram_user_id,o.id FROM balans.workspaces w JOIN balans.memberships m ON m.workspace_id=w.id AND m.role='owner' JOIN balans.operations o ON o.workspace_id=w.id JOIN balans.operation_drafts d ON d.id=o.source_draft_id JOIN balans.receipt_files f ON f.batch_id=d.receipt_batch_id WHERE w.kind='shared'").fetchall()
        for table in tables:c.execute(sql.SQL('ALTER TABLE balans.{} FORCE ROW LEVEL SECURITY').format(sql.Identifier(table)))
    service=Service(os.environ['DATABASE_URL']);preserved=0;missing=0
    try:
        for workspace,actor,operation in targets:
            with service._actor_transaction(actor) as c:
                c.execute("SELECT set_config('balans.workspace_id',%s,true)",(str(workspace),))
                service._attach_source_documents(c,operation)
                expected=c.execute('SELECT count(*) AS n FROM operations o JOIN operation_drafts d ON d.id=o.source_draft_id JOIN receipt_files f ON f.batch_id=d.receipt_batch_id WHERE o.id=%s',(operation,)).fetchone()['n']
                actual=c.execute('SELECT count(*) AS n FROM documents WHERE set_id=%s AND source_receipt_id IS NOT NULL',(operation,)).fetchone()['n']
                preserved+=actual;missing+=max(0,expected-actual)
    finally:service.close()
    print(f'Shared originals preserved: {preserved}; unavailable originals: {missing}.')

if __name__=='__main__':main()
