from test_service import send,query
from test_receipts import button


def test_category_buttons_and_clothing(service,database):
    u=392001
    send(service,u,'/start')
    assert query(database,u,"SELECT name FROM categories WHERE name='Одежда'")==[('Одежда',)]
    r=send(service,u,callback='ui:go:categories')
    assert '👕 Одежда' in r.text
    assert '/categories' not in r.text and 'rename' not in r.text
    assert not any(label=='Категории' for row in r.buttons for label,_ in row)
    r=send(service,u,callback=button(r,'✏️ Переименовать'))
    r=send(service,u,callback=button(r,'👕 Одежда'))
    assert 'Напишите новое название' in r.text
    r=send(service,u,'Гардероб')
    assert 'Гардероб' in r.text
    r=send(service,u,callback=button(r,'🙈 Скрыть'))
    r=send(service,u,callback=button(r,'Гардероб'))
    assert query(database,u,"SELECT archived FROM categories WHERE name='Гардероб'")==[(True,)]
    r=send(service,u,callback=button(r,'👁 Вернуть скрытую'))
    r=send(service,u,callback=button(r,'Гардероб'))
    assert query(database,u,"SELECT archived FROM categories WHERE name='Гардероб'")==[(False,)]
    send(service,u,'/start')
    assert query(database,u,"SELECT count(*) FROM categories WHERE name='Одежда'")==[(0,)]
