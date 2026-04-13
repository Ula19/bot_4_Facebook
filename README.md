# bot_4_facebook

Telegram-бот для скачивания видео и Reels из Facebook.

## Поддерживаемые ссылки

- `facebook.com/watch?v=...`
- `facebook.com/reel/...`
- `facebook.com/username/videos/...`
- `fb.watch/...` (короткие ссылки)

## Стек

- Python 3.12 + aiogram 3.26
- PostgreSQL (asyncpg + SQLAlchemy 2.0)
- Docker + Local Bot API (файлы до 2 ГБ)

## Быстрый старт

```bash
cp .env.example .env
# заполнить BOT_TOKEN, API_ID, API_HASH, ADMIN_IDS, DB_PASSWORD
docker compose up -d --build
docker compose logs -f bot
```

## Структура

```
bot/
├── main.py         # точка входа
├── config.py       # настройки из .env
├── i18n.py         # переводы ru/uz/en
├── handlers/       # start, admin, cookies, download (TODO)
├── services/       # facebook.py (TODO)
├── middlewares/    # подписка, rate limit
├── keyboards/      # inline клавиатуры
├── database/       # модели и CRUD
└── utils/          # helpers (is_facebook_url), commands
```

## Основные команды

| Команда | Описание |
|---------|----------|
| `/start` | Главное меню |
| `/menu` | Главное меню |
| `/profile` | Профиль пользователя |
| `/help` | Помощь |
| `/language` | Сменить язык |
| `/admin` | Админ-панель |
| `/update_cookies` | Обновить Facebook cookies (только для админов) |
