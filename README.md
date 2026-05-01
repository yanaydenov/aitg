# aitg — Telegram AI userbot

Юзербот на базе Telethon + OpenRouter. Отвечает на `.ai ...` в любом чате, читает реплаи с фото/видео/гифками, суммаризирует переписки, ищет в вебе, генерирует и редактирует изображения, помнит контекст через SQLite.

## Стек

- [telethon](https://docs.telethon.dev/) — userbot-клиент
- [openrouter](https://openrouter.ai/) — LLM API (Gemini, Claude, GPT и др.)
- SQLite — память (per-chat, global, user profiles, whitelist)
- `ffmpeg` (системный) — кадры из видео
- ddgs, trafilatura, httpx — инструменты агента

## Установка

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install ffmpeg   # или apt install ffmpeg

cp .env.example .env
# заполни TG_API_ID, TG_API_HASH, OWNER_ID, OPENROUTER_API_KEY

# первый запуск — телеграм спросит телефон и код
python bot.py
```

## Команды

| Команда | Описание |
|---|---|
| `.ai <вопрос>` | задать вопрос / дать задачу |
| `.ai` реплаем на фото/видео | анализ медиа |
| `.stop` | приостановить бота (только владелец) |
| `.start` | возобновить работу бота |
| `.add <user_id>` | добавить пользователя в whitelist (ЛС с владельцем) |

## Возможности агента

- Веб-поиск и чтение страниц
- Генерация и редактирование изображений
- Чтение истории текущего и других чатов
- Поиск сообщений по тексту по всем чатам
- Курсы валют, крипто, погода
- Просмотр профилей пользователей (аватарка, описание)
- Долгосрочная память: per-chat, глобальная, per-user

## Переменные окружения

См. `.env.example`.
