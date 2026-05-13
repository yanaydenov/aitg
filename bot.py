"""Telethon userbot. Триггер: префикс `.ai` в группах/каналах. Лички игнорим."""
from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from telethon import TelegramClient, events

import agent
import media
import memory
import tools

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("aitg")

API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
SESSION = os.getenv("TG_SESSION", "aitg")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
PREFIX = os.getenv("AI_PREFIX", ".ai")
ALLOW_PRIVATE = os.getenv("ALLOW_PRIVATE", "0") == "1"

tg = TelegramClient(SESSION, API_ID, API_HASH)


AGENT_TIMEOUT = float(os.getenv("AGENT_TIMEOUT", "120"))
agent_lock = asyncio.Lock()
_bot_enabled = True


_HELP_TEXT = (
    "флаги `.ai`:\n"
    "  -c / --cost      — токены и стоимость\n"
    "  -d / --debug     — список тулов и время\n"
    "  -m <model>       — модель на запрос (--model)\n"
    "  -t <0-2>         — температура (--temp)\n"
    "  --no-style       — без STYLE_PROMPT\n"
    "  --no-tools       — без вызова тулов\n"
    "  -h / --help      — этот список\n"
    "пример: .ai -c -d какие новости"
)


def _parse_flags(text: str) -> tuple[str, dict]:
    """Извлекает CLI-флаги из текста запроса. Возвращает (clean_text, flags_dict)."""
    flags: dict = {}
    parts = text.split()
    clean: list[str] = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in ("-c", "--cost"):
            flags["cost"] = True
        elif p in ("-d", "--debug"):
            flags["debug"] = True
        elif p in ("--no-tools",):
            flags["no_tools"] = True
        elif p in ("--no-style", "--raw"):
            flags["no_style"] = True
        elif p in ("-h", "--help"):
            flags["help"] = True
        elif p in ("-m", "--model") and i + 1 < len(parts):
            flags["model"] = parts[i + 1]
            i += 1
        elif p in ("-t", "--temp") and i + 1 < len(parts):
            try:
                flags["temp"] = max(0.0, min(2.0, float(parts[i + 1])))
            except ValueError:
                pass
            i += 1
        else:
            clean.append(p)
        i += 1
    return " ".join(clean).strip(), flags


@tg.on(events.NewMessage(pattern=r"(?is)^\s*\.stop\s*$"))
async def on_stop(event: events.NewMessage.Event):
    global _bot_enabled
    if event.sender_id != OWNER_ID:
        return
    _bot_enabled = False
    await event.respond("⏸ бот остановлен")
    log.info("bot disabled by owner")


@tg.on(events.NewMessage(pattern=r"(?is)^\s*\.start\s*$"))
async def on_start(event: events.NewMessage.Event):
    global _bot_enabled
    if event.sender_id != OWNER_ID:
        return
    _bot_enabled = True
    await event.respond("▶️ бот запущен")
    log.info("bot enabled by owner")


@tg.on(events.NewMessage(pattern=r"(?is)^\s*\.add\s+(\d+)"))
async def on_add(event: events.NewMessage.Event):
    """Добавляет пользователя в whitelist (только владелец)."""
    if event.sender_id != OWNER_ID:
        return
    if not event.is_private:
        return

    user_id = int(event.pattern_match.group(1))
    memory.whitelist_add(user_id)
    await event.respond(f"✅ добавил {user_id} в whitelist")
    log.info("added user %s to whitelist", user_id)


@tg.on(events.NewMessage(pattern=r"(?is)^\s*\.remove\s+(\d+)"))
async def on_remove(event: events.NewMessage.Event):
    """Удаляет пользователя из whitelist (только владелец)."""
    if event.sender_id != OWNER_ID:
        return
    if not event.is_private:
        return

    user_id = int(event.pattern_match.group(1))
    memory.whitelist_remove(user_id)
    await event.respond(f"❌ удалил {user_id} из whitelist")
    log.info("removed user %s from whitelist", user_id)


@tg.on(events.NewMessage(pattern=rf"(?is)^\s*{PREFIX}(\b|\s|$)(.*)"))
async def on_ai(event: events.NewMessage.Event):
    if not _bot_enabled:
        return
    # владелец или whitelist пользователь (только в ЛС)
    is_whitelisted = event.is_private and memory.whitelist_check(event.sender_id)
    if event.sender_id != OWNER_ID and not is_whitelisted:
        return
    if event.is_private and not ALLOW_PRIVATE and not is_whitelisted:
        return

    msg = event.message
    raw_text = (event.pattern_match.group(2) or "").strip()
    text, flags = _parse_flags(raw_text)
    log.info("on_ai: chat=%s msg_id=%s text=%r flags=%s", event.chat_id, msg.id, text[:80], flags)

    # --help — отдаём список флагов и выходим
    if flags.get("help"):
        try:
            await msg.edit(f"<pre>{_HELP_TEXT}</pre>", parse_mode="html")
        except Exception:
            await event.respond(f"<pre>{_HELP_TEXT}</pre>", parse_mode="html")
        return

    # для whitelist пользователей не редактируем сообщение - сразу отвечаем
    should_edit = not is_whitelisted

    # реплай — берём медиа и текст из реплая как контекст
    image_parts: list[dict] = []
    quoted_text = ""
    if msg.is_reply:
        replied = await msg.get_reply_message()
        if replied:
            if replied.media:
                try:
                    image_parts.extend(await media.message_to_image_parts(replied))
                except Exception as e:
                    log.exception("media parse failed: %s", e)
            if replied.message:
                quoted_text = replied.message

    # медиа в самом сообщении с командой (добавляем к тем что из реплая)
    if msg.media:
        try:
            image_parts.extend(await media.message_to_image_parts(msg))
        except Exception as e:
            log.exception("media parse (self) failed: %s", e)

    # текст запроса пользователя для отображения (без цитаты реплая)
    user_query = text
    # текст который пойдёт в агента (включая цитату реплая, если была)
    text_for_agent = text
    if quoted_text:
        text_for_agent = (text + "\n\n[Цитата]:\n" + quoted_text).strip()
    if not text_for_agent and not image_parts:
        text_for_agent = "Привет. Что ты умеешь?"
        user_query = ""

    ctx = tools.ToolCtx(
        tg=tg,
        chat_id=event.chat_id,
        owner_id=OWNER_ID,
        is_owner=True,
        trigger_msg=msg,
        loop=asyncio.get_running_loop(),
        input_images=list(image_parts),
        flags=flags,
    )

    # редактируем своё сообщение: показываем "⏳ думаю..." с query в blockquote
    import html as _html_pre
    placeholder_header = _html_pre.escape(f"{PREFIX} {user_query}".strip())
    placeholder = (
        f"<blockquote>{placeholder_header}</blockquote>\n⏳ думаю..."
        if user_query else "⏳ думаю..."
    )
    if should_edit:
        try:
            await msg.edit(placeholder, parse_mode="html")
        except Exception as e:
            log.warning("placeholder edit failed: %s", e)

    try:
        async with agent_lock:
            answer = await asyncio.wait_for(
                agent.run_agent(text_for_agent, image_parts, ctx), timeout=AGENT_TIMEOUT
            )
    except asyncio.TimeoutError:
        log.error("agent timeout (>%ss) for msg_id=%s", AGENT_TIMEOUT, msg.id)
        answer = f"Таймаут агента (>{int(AGENT_TIMEOUT)}s)"
    except Exception as e:
        import traceback
        log.error("agent failed:\n%s", traceback.format_exc())
        answer = f"Ошибка агента: {e}"

    # вставляем ответ под исходным запросом в том же сообщении
    answer = (answer or "").strip() or "(пусто)"

    # CLI-флаги: добавляем footer со статистикой
    footer_lines = []
    if flags.get("cost"):
        s = ctx.stats
        pt, ct = s.get("prompt_tokens", 0), s.get("completion_tokens", 0)
        cost = s.get("cost", 0.0)
        cost_str = f"${cost:.5f}" if cost else "n/a"
        footer_lines.append(f"💰 {pt}+{ct}={pt+ct} ток · {cost_str}")
    if flags.get("debug"):
        s = ctx.stats
        dur = s.get("duration_ms", 0)
        calls = s.get("tool_calls", [])
        footer_lines.append(f"⏱ {dur}ms · {len(calls)} тулов")
        for tc in calls[:15]:
            footer_lines.append(f"  • {tc.get('name')}({tc.get('args_preview','')}) {tc.get('duration_ms',0)}ms")
    if footer_lines:
        answer = answer + "\n\n" + "─" * 20 + "\n" + "\n".join(footer_lines)

    import html as _html
    def _fmt(header_text: str, body: str) -> str:
        h = _html.escape(header_text)
        b = _html.escape(body)
        if header_text:
            return f"<blockquote>{h}</blockquote>\n{b}"
        return b

    # собираем финальное сообщение и режем на чанки (по body, header только в первом)
    body_chunks = [answer[i : i + 3800] for i in range(0, len(answer), 3800)] or [""]
    first = _fmt(f"{PREFIX} {user_query}".strip(), body_chunks[0])
    if should_edit:
        try:
            await msg.edit(first, parse_mode="html")
        except Exception as e:
            log.warning("final edit failed, sending as reply: %s", e)
            try:
                await event.respond(first, parse_mode="html")
            except Exception as e2:
                log.error("respond also failed: %s", e2)
    else:
        # для whitelist пользователей сразу отправляем как reply
        try:
            await event.respond(first, parse_mode="html")
        except Exception as e:
            log.error("respond failed: %s", e)
    for chunk in body_chunks[1:]:
        try:
            await event.respond(_html.escape(chunk), parse_mode="html")
        except Exception as e:
            log.error("respond chunk failed: %s", e)

    # отправляем стикеры если есть
    for path in ctx.pending_stickers:
        try:
            from telethon.tl.types import DocumentAttributeSticker, InputStickerSetEmpty
            await tg.send_file(
                event.chat_id, path,
                attributes=[DocumentAttributeSticker(alt="", stickerset=InputStickerSetEmpty())],
            )
            log.info("sent sticker %s", path)
        except Exception as e:
            log.error("send_sticker failed: %s", e)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    # отправляем сгенерированные картинки если есть
    for path in ctx.pending_images:
        try:
            await tg.send_file(event.chat_id, path)
            log.info("sent image %s", path)
        except Exception as e:
            log.error("send_file failed: %s", e)
        finally:
            try:
                os.remove(path)
            except Exception:
                pass

    log.info("on_ai: done msg_id=%s answer_len=%s images=%d", msg.id, len(answer), len(ctx.pending_images))


async def _reminder_loop():
    """Фоновая задача: каждые 30 сек проверяет и отправляет напоминания."""
    import time as _time
    while True:
        await asyncio.sleep(30)
        try:
            due = memory.get_due_reminders(int(_time.time()))
            for r in due:
                try:
                    await tg.send_message(r["chat_id"], f"⏰ Напоминание: {r['text']}")
                    memory.mark_reminder_done(r["id"])
                    log.info("reminder %s fired in chat %s", r["id"], r["chat_id"])
                except Exception as e:
                    log.error("reminder send failed: %s", e)
        except Exception as e:
            log.error("reminder loop error: %s", e)


async def main():
    await tg.start()
    me = await tg.get_me()
    log.info("logged in as @%s (id=%s). owner_id=%s", me.username, me.id, OWNER_ID)
    log.info("listening for prefix %r. private=%s", PREFIX, ALLOW_PRIVATE)
    asyncio.create_task(_reminder_loop())
    await tg.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
