from decimal import Decimal
import asyncio

from test_receipts import receipts,send,query,button,review,USERS,IDS,NOW
from test_media_flow import transaction,category,paid,save_row,open_row
from test_receipt_transport import MediaBot,photo_update
from receipt_fixtures import photo_bytes
from balans.__main__ import process_update


def labels(reply):return [label for row in reply.buttons for label,_ in row]


def products(food,home):
    return [dict(name='Молоко',quantity='1',unit_price='100',line_total='100',discount=None,category_id=food),
            dict(name='Мыло',quantity='1',unit_price='50',line_total='50',discount=None,category_id=home)]


def setup_products(s,ai,u,db):
    send(s,u,'/start');food=category(db,u)
    home=str(query(db,u,"SELECT id FROM categories WHERE name='Дом'")[0][0])
    ai.changes={'total':'150','items':products(food,home),'items_complete':True,'receipt_reference':'receipt-reference-for-split'}
    return food,home


def test_auto_receipt_and_split_saves_exact_total(receipts,database):
    s,ai,_=receipts;u=next(USERS);food,home=setup_products(s,ai,u,database)
    bot=MediaBot(photo_bytes());update=photo_update(u,next(IDS))
    asyncio.run(process_update(bot,s,update))
    assert len(bot.messages)==1 and len(ai.calls)==1
    text,kwargs=bot.messages[0]
    for removed in ('Бюджет:','Счёт:','Часовой пояс:','Пока не сохран','Добавлено файлов','Распознаю'):
        assert removed not in text
    keyboard=kwargs['reply_markup'].inline_keyboard
    assert any(b.text=='Выбрать другую категорию' for row in keyboard for b in row)
    split=next(b.callback_data for row in keyboard for b in row if b.text=='Разбить по категориям')
    listing=send(s,u,callback=split)
    assert 'Продукты: 100,00 ₽' in listing.text and 'Дом: 50,00 ₽' in listing.text
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]
    assert 'устарела' in send(s,u,callback=split).text
    save_row(s,u,paid(s,u,open_row(s,u,listing)))
    listing=send(s,u,'/media')
    save_row(s,u,paid(s,u,open_row(s,u,listing,2)))
    assert query(database,u,'SELECT sum(delta) FROM postings')==[(Decimal('-150'),)]
    assert query(database,u,'SELECT count(*) FROM documents')==[(2,)]
    assert query(database,u,'SELECT count(DISTINCT category_id) FROM operation_revisions')==[(2,)]
    repeat=review(s,u)
    assert 'Найден сохранённый расход' in repeat.text


def test_incomplete_or_mismatched_receipt_cannot_split(receipts,database):
    s,ai,_=receipts;u=next(USERS);setup_products(s,ai,u,database)
    ai.changes['total']='160'
    card=review(s,u)
    reply=send(s,u,callback=button(card,'Разбить по категориям'))
    assert 'не совпадает' in reply.text
    assert query(database,u,"SELECT count(*) FROM operation_drafts WHERE state='pending'")==[(1,)]
    assert query(database,u,'SELECT count(*) FROM media_queues')==[(0,)]


def test_auto_screenshot_category_change_and_product_split(receipts,database):
    s,ai,_=receipts;u=next(USERS);food,home=setup_products(s,ai,u,database)
    item=transaction(food);item['items']=products(food,home);item['items_complete']=True
    ai.changes={'transactions':[item],'source_type':'screenshot','document_kind':'multiple'}
    bot=MediaBot(photo_bytes())
    asyncio.run(process_update(bot,s,photo_update(u,next(IDS))))
    assert len(bot.messages)==1 and 'Категория: Продукты' in bot.messages[0][0]
    card=send(s,u,'/media');card=open_row(s,u,card)
    menu=send(s,u,callback=button(card,'Выбрать другую категорию'))
    changed=send(s,u,callback=button(menu,'Дом'))
    assert 'Категория: Дом' in changed.text
    listing=send(s,u,callback=button(changed,'Разбить по категориям'))
    assert 'Продукты: 100,00 ₽' in listing.text and 'Дом: 50,00 ₽' in listing.text
    assert len(ai.calls)==1


def test_screenshot_multiple_transactions_group_and_bulk_category(receipts,database):
    s,ai,_=receipts;u=next(USERS);food,home=setup_products(s,ai,u,database)
    ai.changes={'source_type':'screenshot','document_kind':'multiple','transactions':[transaction(food,'100'),transaction(home,'50')]}
    bot=MediaBot(photo_bytes());asyncio.run(process_update(bot,s,photo_update(u,next(IDS))))
    listing=send(s,u,'/media')
    grouped=send(s,u,callback=button(listing,'Разбить по категориям'))
    assert 'Продукты: 100,00 ₽' in grouped.text and 'Дом: 50,00 ₽' in grouped.text
    menu=send(s,u,callback=button(grouped,'Выбрать другую категорию'))
    changed=send(s,u,callback=button(menu,'Дом'))
    assert query(database,u,'SELECT count(*) FROM operations')==[(0,)]
    assert all(i['category_id']==home for i in query(database,u,'SELECT items FROM media_queues')[0][0])
    assert 'устарела' in send(s,u,callback=button(menu,'Дом')).text
    assert all(len(data.encode())<=64 for row in menu.buttons for _,data in row)


def test_album_collected_automatically_one_result(receipts):
    s,ai,_=receipts;u=next(USERS);bot=MediaBot(photo_bytes())
    first=photo_update(u,next(IDS)).model_copy(deep=True)
    second=photo_update(u,next(IDS)).model_copy(deep=True)
    first=first.model_copy(update={'message':first.message.model_copy(update={'media_group_id':'album-1'})})
    second=second.model_copy(update={'message':second.message.model_copy(update={'media_group_id':'album-1'})})
    s.accept_updates(bot.id,[first,second])
    asyncio.run(process_update(bot,s,first))
    assert bot.messages==[] and ai.calls==[]
    bot.data=photo_bytes(True)
    asyncio.run(process_update(bot,s,second))
    assert len(bot.messages)==1 and len(ai.calls)==1 and ai.calls[0].pages==2
    asyncio.run(process_update(bot,s,second))
    assert len(ai.calls)==1 and bot.downloads==2


def test_category_menu_keeps_identity_after_new_category(receipts,database):
    s,ai,_=receipts;u=next(USERS);food,home=setup_products(s,ai,u,database)
    card=review(s,u);listing=send(s,u,callback=button(card,'Разбить по категориям'))
    menu=send(s,u,callback=button(listing,'Выбрать другую категорию'))
    send(s,u,'/categories add Ааа новая')
    send(s,u,callback=button(menu,'Дом'))
    assert all(i['category_id']==home for i in query(database,u,'SELECT items FROM media_queues')[0][0])
