# Nails Booking Bot

SaaS самозаписи в Telegram для мастеров маникюра. Каждый мастер — отдельный tenant со своим ботом. Клиенты не ищут мастеров: мастер сам приводит людей в свой бот.

Стек: Python 3.12, FastAPI, aiogram 3 (long polling), SQLAlchemy 2 async, Alembic, PostgreSQL 16.

## Локальный запуск

1. Скопируйте `.env.example` в `.env`.
2. Поднимите базу:

```bash
docker compose up -d
```

3. Создайте виртуальное окружение и зависимости:

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

4. Миграции:

```bash
alembic upgrade head
```

5. Запуск API и ботов (строго один worker):

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
```

Почему `--workers 1`: FSM aiogram хранится в `MemoryStorage` в памяти процесса, polling и цикл напоминаний тоже живут в этом процессе. Несколько worker'ов uvicorn дали бы несколько polling-циклов на одни и те же токены и рассинхрон FSM.

Проверка:

- http://127.0.0.1:8000/health
- http://127.0.0.1:8000/health/db

## Тесты

Нужен запущенный Postgres из `docker-compose.yml`. Тесты создают базу `nails_booking_test` и накатывают миграции.

```bash
pytest
```

## Онбординг мастера

1. В Telegram откройте [@BotFather](https://t.me/BotFather).
2. Команда `/newbot`, задайте имя и username бота.
3. Скопируйте токен вида `123456:ABC...`.
4. Узнайте свой Telegram ID (например через `@userinfobot`) — это `owner-telegram-id`.
5. Подключите бизнес:

```bash
python -m app.cli add-business --name "Студия Анны" --bot-token "123:ABC" --owner-telegram-id 111111 --timezone Europe/Minsk
```

Опционально: `--slot-step 15 --min-notice 60 --max-days 30 --reminders 1440,120`.

6. Перезапустите приложение — polling подхватит новых ботов только на старте.

Список:

```bash
python -m app.cli list-businesses
```

`/start` от аккаунта владельца открывает меню мастера, от любого другого — меню клиента.

## Деплой на VPS (Ubuntu 24.04)

1. Установите Docker Engine и плагин Compose.
2. Скопируйте проект, создайте `.env` (пароль БД не оставляйте дефолтный).
3. В `.env` для контейнера приложения:

```
APP_ENV=production
DATABASE_URL=postgresql+asyncpg://nails:НАДЁЖНЫЙ_ПАРОЛЬ@db:5432/nails_booking
```

Пароль должен совпадать с `POSTGRES_PASSWORD` в compose или в `.env`.

4. Запуск:

```bash
docker compose -f docker-compose.prod.yml up -d --build
```

Порт Postgres наружу не пробрасывается. API слушает `8000`.

5. После `add-business` перезапустите сервис `app`.

### Бэкапы

Cron раз в сутки, например `/etc/cron.d/nails-backup`:

```
0 3 * * * root docker compose -f /opt/nails-booking-bot/docker-compose.prod.yml exec -T db pg_dump -U nails nails_booking | gzip > /var/backups/nails/nails_$(date +\%F).sql.gz
```

Храните дампы вне контейнера. Проверяйте восстановление на копии.

## Напоминания

Таблица `notification_tasks` + asyncio-цикл каждые 15 секунд внутри приложения. Redis/Celery нет. После рестарта неотправленные `pending` задачи уходят снова.
