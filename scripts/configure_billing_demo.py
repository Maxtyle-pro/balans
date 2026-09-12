"""Allow one registered tester to simulate billing without enabling Stars payments."""
import argparse
import os
import psycopg
from dotenv import load_dotenv


def configure(dsn,actor,stars=100,disable=False):
    if not 0<actor<2**63 or not 1<=stars<=10000:raise ValueError('Проверьте Telegram ID и тестовую цену (1–10000 Stars).')
    with psycopg.connect(dsn) as c:
        c.execute("SELECT set_config('balans.telegram_user_id',%s,true)",(str(actor),))
        c.execute('SET LOCAL search_path=balans,pg_catalog')
        user=c.execute('SELECT actor_user_id()').fetchone()[0]
        if not user:raise ValueError('Пользователь ещё не зарегистрирован. Сначала отправьте боту /start.')
        c.execute('INSERT INTO billing_demo(user_id,allowed,enabled,stars) VALUES(%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET allowed=excluded.allowed,enabled=excluded.enabled,stars=excluded.stars,generation=gen_random_uuid(),checkout_state=CASE WHEN billing_demo.checkout_state=\'pending\' THEN \'cancelled\' ELSE billing_demo.checkout_state END',
                  (user,not disable,not disable,stars))


def main():
    load_dotenv()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--user',type=int,required=True,help='Telegram ID зарегистрированного тестировщика')
    parser.add_argument('--stars',type=int,default=100,help='Условная цена для макета, без списаний')
    parser.add_argument('--disable',action='store_true',help='Отозвать доступ к имитации')
    args=parser.parse_args()
    configure(os.environ['MIGRATION_DATABASE_URL'],args.user,args.stars,args.disable)
    print('Имитация '+('отключена.' if args.disable else 'включена. В личном бюджете откройте /start или /demo. Настоящая оплата не включалась.'))


if __name__=='__main__':main()
