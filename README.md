# bot_4_facebook

Telegram-бот для скачивания видео и Reels из Facebook.

## Возможности

- Скачивание видео, Reels и Stories из Facebook
- Автоматическое определение ссылок — просто отправь ссылку
- Кэширование скачанных видео (повторная отправка без перезагрузки)
- Fallback-цепочка: direct → WARP → резидентный прокси → cookies-варианты
- Мультиязычность: русский, узбекский, английский
- Админ-панель: статистика, управление каналами, рассылка, cookies
- Обязательная подписка на каналы
- Local Bot API — файлы до 2 ГБ
- Автоочистка временных файлов и данных Local Bot API

## Поддерживаемые ссылки

| Формат | Пример |
|--------|--------|
| Видео | `facebook.com/watch?v=123` |
| Видео в профиле | `facebook.com/user/videos/123` |
| Reels | `facebook.com/reel/123` |
| Reels (профиль) | `facebook.com/user/reels/123` |
| Шеринг Reels | `facebook.com/share/r/abc123/` |
| Шеринг видео | `facebook.com/share/v/abc123/` |
| Общий шеринг | `facebook.com/share/abc123/` |
| Stories | `facebook.com/stories/123/...` (нужны cookies) |
| Короткие ссылки | `fb.watch/abc123` |
| Мобильные | `m.facebook.com/...` |

## Стек

- Python 3.12 + aiogram 3.26
- yt-dlp (движок скачивания)
- PostgreSQL 16 (asyncpg + SQLAlchemy 2.0)
- Docker Compose + Local Bot API + Cloudflare WARP

## Быстрый старт

```bash
cp .env.example .env
# заполнить BOT_TOKEN, API_ID, API_HASH, ADMIN_IDS, DB_PASSWORD
docker compose up -d --build
docker compose logs -f bot
```

## Docker-сервисы

| Сервис | Образ | Назначение |
|--------|-------|------------|
| `bot` | python:3.12-slim | Сам бот |
| `bot-api` | aiogram/telegram-bot-api | Local Bot API (файлы до 2 ГБ) |
| `warp` | ghcr.io/mon-ius/docker-warp-socks:v5 | Cloudflare WARP SOCKS5 прокси |
| `postgres` | postgres:16-alpine | База данных |
| `autoheal` | willfarrell/autoheal | Автоперезапуск unhealthy контейнеров |

## Структура

```
bot/
├── main.py              # точка входа, фоновая очистка
├── config.py            # настройки из .env (pydantic-settings)
├── i18n.py              # переводы ru/uz/en
├── emojis.py            # эмодзи и custom emoji ID
├── handlers/
│   ├── start.py         # /start, /menu, /profile, /help, /language
│   ├── admin.py         # /admin, статистика, каналы, рассылка
│   ├── cookies.py       # /update_cookies (загрузка Facebook cookies)
│   └── download.py      # обработка ссылок, скачивание, отправка
├── services/
│   └── facebook.py      # FacebookDownloader (yt-dlp, fallback chain)
├── middlewares/
│   ├── rate_limit.py    # лимит 5 запросов/минуту
│   └── subscription.py  # проверка подписки на каналы
├── keyboards/
│   ├── inline.py        # пользовательские клавиатуры
│   └── admin.py         # админские клавиатуры
├── database/
│   ├── models.py        # User, Channel, MediaCache
│   └── crud.py          # CRUD-операции
└── utils/
    ├── helpers.py       # is_facebook_url, clean_facebook_url
    └── commands.py      # меню команд бота
```

## Команды

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/menu` | Главное меню |
| `/profile` | Профиль пользователя |
| `/help` | Помощь |
| `/language` | Сменить язык |
| `/admin` | Админ-панель |
| `/update_cookies` | Обновить Facebook cookies (админы) |
