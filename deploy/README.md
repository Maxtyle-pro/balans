# Автодеплой Баланса через GitHub

Репозиторий: https://github.com/Maxtyle-pro/balans. Ветка выпуска — `main`.
Каждый push запускает тесты с отдельным PostgreSQL 15 и проверочную сборку Docker.
После успеха GitHub Actions доставляет именно проверенный коммит по SSH на сервер,
собирает образ, останавливает бота, применяет миграции отдельной ролью и запускает
новую версию. Pull request выполняет только проверки. Ручной запуск доступен
в Actions → «Проверка и деплой» → Run workflow для `main`.

## Подготовка Linux-сервера один раз

Нужны Docker Engine с Compose v2, Bash, `flock`, SSH и PostgreSQL 15+.
До первого деплоя создайте сеть `docker network create balans-backend`.
Отдельный контейнер PostgreSQL для Баланса подключайте к этой сети, без
публикации порта БД на хосте. В строках подключения используйте его сетевое имя.
Бот и контейнер миграций подключаются к этой же сети.
Порты webhook и панели на хосте: `127.0.0.1:18080` и `127.0.0.1:18088`;
при настройке reverse proxy используйте эти адреса.
Пользователь `deploy` должен входить в группу `docker` (это даёт права уровня root).
Добавьте публичную часть отдельного SSH-ключа в его `~/.ssh/authorized_keys`.
Выполните от администратора, заменив `deploy`, если выбрано другое имя:

```bash
sudo install -d -o deploy -g deploy -m 700 /opt/balans /opt/balans/releases /etc/balans
sudo install -d -o 10001 -g 10001 -m 700 /opt/balans/shared
sudo usermod -aG docker deploy
```

После изменения групп откройте новую SSH-сессию. Создайте на сервере:

- `/etc/balans/runtime.env` по `deploy/runtime.env.example`; дополнительные настройки
  возьмите из корневого `.env.example`. Не добавляйте сюда `MIGRATION_DATABASE_URL`
  или реквизиты резервной роли.
- `/etc/balans/migration.env` по `deploy/migration.env.example`.

Оба файла должны принадлежать пользователю деплоя и иметь права `600`.
Базу и роли создайте заранее по инструкции в корневом README: владелец схемы
без SUPERUSER/BYPASSRLS и отдельная runtime-роль. Деплой сам применит миграции.
Не используйте для production локальную тестовую БД.

Если PostgreSQL стоит на этом сервере, адрес из контейнера —
`host.docker.internal`, а не `localhost`. Настройте `listen_addresses`, `pg_hba.conf`
и firewall для доступа из Docker-сети без открытия базы в интернет. Для внешней
БД используйте её адрес и TLS с проверкой сертификата.

Все постоянные файлы лежат в `/opt/balans/shared`, который подключён как
`/app/.local`: чеки, документы, ключи, реестр удалений. Пути в runtime.env
должны указывать внутрь `/app/.local`. Для Google JSON, например:
`GOOGLE_SERVICE_ACCOUNT_FILE=/app/.local/google-service-account.json`.
Файлы должны быть доступны UID 10001. Резервирование базы и этого каталога
настраивается отдельно: см. `docs/20-stage-18.md`.

По умолчанию используется polling, домен не нужен. Перед первым запуском
остановите локальный экземпляр этого же Telegram-бота. Для webhook настройте
HTTPS reverse proxy по существующему шаблону Caddy; порты контейнера открываются
только на loopback сервера.

## Настройка GitHub

В репозитории откройте Settings → Environments и создайте `production`.
Ограничьте deployment branches веткой `main`. Добавьте environment secrets:

| Secret | Значение |
| --- | --- |
| `DEPLOY_HOST` | IP или DNS-имя сервера |
| `DEPLOY_USER` | SSH-пользователь, например `deploy` |
| `DEPLOY_PORT` | SSH-порт; можно не задавать, по умолчанию 22 |
| `DEPLOY_SSH_KEY` | Полный приватный SSH-ключ без passphrase, отдельный для деплоя |
| `DEPLOY_KNOWN_HOSTS` | Проверенная строка host key сервера в формате known_hosts |

Получите host key через доверенную консоль сервера и сверяйте отпечаток при
подготовке known_hosts. Для нестандартного SSH-порта запись должна иметь вид
`[hostname]:port key-type key-data`. Workflow требует проверки host key.
BOT_TOKEN, ключ OpenAI и пароли БД остаются на сервере, в GitHub их вводить не нужно.

Загрузите файлы проекта в `main`. Не коммитьте `.env`, `.local`, дампы и ключи:
они исключены существующим `.gitignore`. При первом подключении пустого
Git-репозитория можно настроить remote:

```bash
git remote add origin https://github.com/Maxtyle-pro/balans.git
```

## Проверка и обслуживание

Успех workflow означает: тесты и сборка прошли, миграции применены, процесс
завершил подключение к Telegram и БД, не перезапускался во время проверки,
`scripts.healthcheck` вернул успех. Это не проверка реальных платежей или OCR.
После первого выпуска проверьте в Telegram `/start`, ввод расхода и `/report`.

На сервере:

```bash
cd /opt/balans/current
export BALANS_IMAGE="balans:$(cat /opt/balans/last-successful-revision)"
docker compose -p balans -f deploy/production.compose.yaml ps
docker compose -p balans -f deploy/production.compose.yaml logs --tail=100 bot
```

Ошибка сборки не останавливает текущую версию. Если ошибка возникла после
остановки бота, workflow завершится неуспешно; бот может остаться остановленным
или новая версия может работать с ошибкой проверки. Автоматического отката
схемы нет: исправьте причину и повторите workflow. Не запускайте старый образ
поверх новой схемы без проверки совместимости. `current` указывает только
на последний успешный выпуск; при неудаче исследуйте каталог коммита в
`/opt/balans/releases/<SHA>`, задав `BALANS_IMAGE=balans:<SHA>`.

Старые образы и каталоги выпусков автоматически не удаляются. Периодически
очищайте ненужные версии после проверки нового выпуска, сохраняя текущую
и необходимые для восстановления. Каталог `shared` не удаляйте.
