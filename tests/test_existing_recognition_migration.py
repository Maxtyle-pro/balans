from pathlib import Path
import psycopg
from test_service import send


def test_migration_enables_existing_user_without_actor(service,database):
    user=393001
    send(service,user,'/start')
    with service._actor_transaction(user) as c:
        c.execute('UPDATE user_settings SET ai_enabled=false,voice_enabled=false,receipts_enabled=false WHERE user_id=actor_user_id()')
    with psycopg.connect(database[0]) as c:
        c.execute(Path('migrations/032_enable_existing_recognition.sql').read_text())
    with service._actor_transaction(user) as c:
        flags=c.execute('SELECT ai_enabled,voice_enabled,receipts_enabled FROM user_settings WHERE user_id=actor_user_id()').fetchone()
        assert all(flags.values())
