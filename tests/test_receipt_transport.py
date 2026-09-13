import asyncio
from datetime import datetime,timezone

from aiogram.types import File,Update

from balans.__main__ import process_update
from receipt_fixtures import photo_bytes,pdf_bytes
from test_receipts import receipts,send,USERS,IDS


class MediaBot:
    id=42
    def __init__(self,data):
        self.data=data;self.downloads=0;self.messages=[];self.photos=[];self.documents=[]
    async def get_file(self,identity):
        self.downloads+=1
        return File(file_id=identity,file_unique_id='unique',file_path='photos/test.jpg',file_size=len(self.data))
    async def download_file(self,path,destination,**kwargs):
        destination.write(self.data)
    async def send_message(self,chat_id,text,**kwargs):
        self.messages.append((text,kwargs))
    async def answer_callback_query(self,*args):
        pass
    async def send_photo(self,chat_id,file_id):
        self.photos.append(file_id)
    async def send_document(self,chat_id,file_id):
        self.documents.append(file_id)


def photo_update(user,number,as_document=False):
    message={'message_id':number,'date':int(datetime.now(timezone.utc).timestamp()),'chat':{'id':user,'type':'private'},'from':{'id':user,'is_bot':False,'first_name':'Test'}}
    media={'file_id':'file-test','file_unique_id':'unique','file_size':100}
    if as_document:
        message['document']={**media,'mime_type':'application/pdf','file_name':'../../test.PDF'}
    else:
        message['photo']=[{**media,'width':900,'height':1100}]
    return Update.model_validate({'update_id':number,'message':message})


def test_pdf_upload_without_permission_and_dedup(receipts):
    s,_,storage=receipts;user=next(USERS);bot=MediaBot(pdf_bytes());u=photo_update(user,next(IDS),True)
    asyncio.run(process_update(bot,s,u))
    asyncio.run(process_update(bot,s,u))
    assert bot.downloads==1
    assert 'Продавец:' in bot.messages[-1][0]
    assert all('Добавлено файлов' not in text for text,_ in bot.messages)
    assert len(s.receipt_ai.calls)==1
    assert len(list(storage.root.glob('*.bin')))==1
    assert not (storage.root.parent/'test.PDF').exists()


def test_unavailable_telegram_file_does_not_poison_polling(receipts):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetFile
    s,_,_=receipts;user=next(USERS);bot=MediaBot(pdf_bytes())
    send(s,user,callback='receipts_on')
    async def unavailable(identity):
        raise TelegramBadRequest(method=GetFile(file_id=identity),message='file is too big')
    bot.get_file=unavailable
    asyncio.run(process_update(bot,s,photo_update(user,next(IDS),True)))
    assert 'не выдал файл' in bot.messages[-1][0]


def test_missing_source_delivery_keeps_bot_running():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import SendDocument
    from balans.__main__ import deliver_reply
    from balans.domain import Reply
    bot=MediaBot(b'')
    async def unavailable(chat_id,file_id):
        raise TelegramBadRequest(method=SendDocument(chat_id=chat_id,document=file_id),message='invalid file identifier')
    bot.send_document=unavailable
    asyncio.run(deliver_reply(bot,42,Reply('Исходник',document_ids=['missing'])))
    assert 'Данные расхода сохранены' in bot.messages[-1][0]


def test_receipt_recognition_stays_enabled(receipts):
    s,_,_=receipts;user=next(USERS);bot=MediaBot(photo_bytes())
    assert 'автоматически' in send(s,user,'/receipts off').text
    asyncio.run(process_update(bot,s,photo_update(user,next(IDS))))
    assert bot.downloads==1
