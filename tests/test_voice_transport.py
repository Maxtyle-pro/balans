import asyncio
from aiogram.types import Update
from balans.__main__ import process_update
from test_receipt_transport import MediaBot
from test_voice import voices, audio, USERS, UPDATES, NOW
from test_receipts import send, query


def update(user,number,duration=1):
    return Update.model_validate({'update_id':number,'message':{'message_id':number,'date':int(NOW.timestamp()),'chat':{'id':user,'type':'private'},'from':{'id':user,'is_bot':False,'first_name':'Test'},'voice':{'file_id':'voice','file_unique_id':'unique','duration':duration,'mime_type':'audio/ogg','file_size':100}}})


def test_voice_without_permission_download_dedup_and_confirm(voices,database):
    s,ai=voices;user=next(USERS);bot=MediaBot(audio());u=update(user,next(UPDATES))
    asyncio.run(process_update(bot,s,u))
    asyncio.run(process_update(bot,s,u));asyncio.run(process_update(bot,s,u))
    assert bot.downloads==1 and ai.transcriptions==1
    assert 'Описание:' in bot.messages[-1][0]
    assert query(database,user,'SELECT * FROM postings')==[]


def test_oversized_duration_precedes_download(voices):
    s,ai=voices;user=next(USERS);bot=MediaBot(audio())
    send(s,user,callback='voice_on')
    asyncio.run(process_update(bot,s,update(user,next(UPDATES),181)))
    assert bot.downloads==0 and ai.transcriptions==0
    assert '3 минут' in bot.messages[-1][0]


def test_download_failure_cached(voices):
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import GetFile
    s,ai=voices;user=next(USERS);bot=MediaBot(audio());u=update(user,next(UPDATES))
    send(s,user,callback='voice_on')
    async def fail(identity):
        bot.downloads+=1
        raise TelegramBadRequest(method=GetFile(file_id=identity),message='unavailable')
    bot.get_file=fail
    asyncio.run(process_update(bot,s,u));asyncio.run(process_update(bot,s,u))
    assert bot.downloads==1 and ai.transcriptions==0


def test_voice_recognition_stays_enabled(voices):
    s,ai=voices;user=next(USERS);bot=MediaBot(audio())
    assert 'автоматически' in send(s,user,'/voice off').text
    asyncio.run(process_update(bot,s,update(user,next(UPDATES))))
    assert bot.downloads==1 and ai.transcriptions==1
