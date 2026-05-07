"""Агент на OpenRouter (OpenAI-совместимый API) с function-calling и vision."""
from __future__ import annotations

import ast
import asyncio
import inspect
import json
import logging
import os
import re
from collections import defaultdict, deque
from datetime import datetime, timezone

from openai import OpenAI

import memory
import tools

log = logging.getLogger("aitg.agent")

_client: OpenAI | None = None

# История диалога на каждый chat_id: deque из dict-сообщений (user/assistant)
HISTORY_MAX_PAIRS = 10
_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_MAX_PAIRS * 2))


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=os.getenv("OPENROUTER_API_KEY", ""),
            base_url="https://openrouter.ai/api/v1",
            timeout=float(os.getenv("OPENROUTER_TIMEOUT", "60")),
            max_retries=1,
        )
    return _client


def _build_tools() -> list[dict]:
    """Собирает список tool-схем в формате OpenAI function calling."""
    result = []
    for fn in tools.ALL_TOOLS:
        sig = inspect.signature(fn)
        params: dict = {}
        required = []
        for name, p in sig.parameters.items():
            ann = p.annotation
            if ann is int:
                t = "integer"
            elif ann is float:
                t = "number"
            elif ann is bool:
                t = "boolean"
            else:
                t = "string"
            params[name] = {"type": t}
            if p.default is inspect.Parameter.empty:
                required.append(name)
        result.append({
            "type": "function",
            "function": {
                "name": fn.__name__,
                "description": (fn.__doc__ or "").strip()[:200],
                "parameters": {
                    "type": "object",
                    "properties": params,
                    **({"required": required} if required else {}),
                },
            },
        })
    return result


_OA_TOOLS: list[dict] | None = None


def _oa_tools() -> list[dict]:
    global _OA_TOOLS
    if _OA_TOOLS is None:
        _OA_TOOLS = _build_tools()
    return _OA_TOOLS


def _system_prompt(ctx: tools.ToolCtx) -> str:
    chat_mem = memory.list_keys(chat_id=ctx.chat_id, glob=False)
    glob_mem = memory.list_keys(glob=True) if ctx.is_owner else []
    parts = [
        "Ты — AI-ассистент, встроенный в Telegram-аккаунт Ярика. Ты ОТДЕЛЬНАЯ третья сторона, НЕ Ярик и НЕ кто-либо из пользователей.",
        "С тобой могут общаться разные люди. Перед каждым сообщением есть тег [От: имя, user_id=...] — это показывает КТО именно тебе пишет. Используй это чтобы понять к кому обращаться и что помнить.",
        f"Владелец аккаунта: Ярик (user_id={ctx.owner_id}). Ты работаешь на его аккаунте, но ты не он.",
        "Отвечай кратко, по делу, на языке последнего вопроса.",
        "Используй тулы агрессивно: для свежей инфы — web_search/fetch_url.",
        "ВАЖНО: read_chat_history читает ТОЛЬКО ТЕКУЩИЙ чат. Используй его если просят 'прочитай чат', 'саммари', 'последние сообщения' БЕЗ указания другого чата.",
        "Если спрашивают про сообщения за период — используй read_other_chat с since_hours: 'сегодня'=24, 'вчера'=48, '2 дня'=48, '3 дня'=72, 'неделя'=168. Всегда ставь limit=200 при таких запросах. Если результат пустой — честно скажи что за этот период сообщений не было, и укажи ts последнего сообщения из обычного вызова без since_hours.",
        "СТРОГО: если просят рандомное/случайное сообщение из чатов — вызови list_all_chats(), выбери случайный чат, вызови read_other_chat(chat_id, limit=100), выбери случайное сообщение из результата и процитируй ДОСЛОВНО с указанием чата и автора. НИКОГДА не придумывай текст сообщений — только реальные данные из тулов. Если просят 'ещё' — снова вызывай read_other_chat на другом чате.",
        "Если в запросе названо имя ДРУГОГО чата/группы/канала (любое слово/фраза которая выглядит как имя: 'охуенко чат', 'scared cat clan', 'наша группа', 'мама', 'работа', 'сони' и т.п.) — СНАЧАЛА вызови list_all_chats() чтобы получить список всех чатов, САМОСТОЯТЕЛЬНО выбери подходящий по названию, получи id, затем read_other_chat(chat_id, limit). find_chat можно использовать как fallback если list_all_chats не помог. НЕ используй read_link_preview - он работает только для публичных ссылок к которым есть доступ. Если нужно взять фотки из другого чата для генерации/редактирования — вызови read_other_chat с include_media=true чтобы получить image_urls.",
        "Если вопрос про погоду/курс/крипту — соответствующие тулы.",
        "Если нужно узнать полную информацию о человеке (имя, username, описание, аватарка) — используй get_user_profile(user_id). Проактивно сохраняй важную информацию о людях через user_remember(user_id, key, value) когда видишь полезные данные в контексте (отношения, теги, предпочтения). ВАЖНО: когда пользователь говорит 'X это Y' (например 'соня это @b4rmalda', '123456 это моя девушка') — это ЗАПОМИНАНИЕ факта. СНАЧАЛА используй get_user_profile чтобы получить user_id по username, затем user_remember(user_id, key, value). НЕ ищи чат через find_chat когда речь о конкретном человеке.",
        "Если просят 'нарисуй', 'сгенерируй картинку' БЕЗ фото — вызови generate_image с описанием. Если просят 'сделай из этого', 'переделай', 'исправь', 'замени', 'отредактируй' И приложили фото — это РЕДАКТИРОВАНИЕ изображения: вызови generate_image(prompt='инструкция что именно изменить в референсном изображении на английском, например turn this sandwich into a gourmet version with more ingredients'). ВАЖНО: если пользователь приложил фото (в сообщении или реплае) — они автоматически передадутся как референсы, НЕ нужно описывать внешность людей словами. НЕ додумывай детали которых нет в запросе пользователя - используй только то что он сказал. Если запрос неясен - спроси уточнение. Картинка сама отправится в чат.",
        "ПАМЯТЬ: у тебя есть контекст последних ~10 сообщений этого чата. Если пользователь просит 'запомни', 'не забудь', 'это важно' или говорит личные факты (имена родственников/друзей, отношения, предпочтения, важные договорённости) — ОБЯЗАТЕЛЬНО вызови memory_remember(key='короткое описание', value='детали', scope='global' если это про владельца в целом, или 'chat' если специфично для чата). Если речь идёт о КОНКРЕТНОМ ЧЕЛОВЕКЕ по его telegram ID (например 'запомни что 123456 это соня', '123456 моя девушка') — используй user_remember(user_id, key, value). НЕ пиши 'запомнил' текстом - вызови тула. Проактивно используй memory_recall/memory_list и user_recall/user_list если видишь что вопрос требует ранее сохранённой инфы.",
        f"Текущий chat_id: {ctx.chat_id}. is_owner={ctx.is_owner}.",
        f"Сейчас: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} (UTC+5 = {datetime.now(timezone.utc).strftime('%Y-%m-%d')} {(datetime.now(timezone.utc).hour + 5) % 24:02d}:{datetime.now(timezone.utc).strftime('%M')}). Учитывай это при ответах про 'сегодня', 'вчера', 'сейчас'. Если читаешь сообщения из чата — проверяй их timestamp и говори когда они были написаны.",
    ]
    # добавляем данные текущего отправителя
    if ctx.trigger_msg and ctx.trigger_msg.sender_id:
        sid = ctx.trigger_msg.sender_id
        saved = memory.list_user_info(sid)
        saved_pairs = ("; ".join(f"{k}={v}" for k, v in saved[:10])) if saved else "нет"
        parts.append(f"Текущий собеседник: user_id={sid}. Сохранённые данные: {saved_pairs}. Если говорит 'мои аватарки'/'моё фото' — get_user_profile('{sid}').")
    style = os.getenv("STYLE_PROMPT", "").strip()
    if style:
        parts.append("Стиль общения владельца (подражай ему в ответах): " + style)
    if chat_mem:
        parts.append("Память этого чата: " + "; ".join(f"{k}={v}" for k, v in chat_mem[:30]))
    if glob_mem:
        parts.append("Глобальная память владельца: " + "; ".join(f"{k}={v}" for k, v in glob_mem[:30]))
    return "\n".join(parts)


def _call_tool(name: str, args: dict) -> str:
    fn_map = {fn.__name__: fn for fn in tools.ALL_TOOLS}
    fn = fn_map.get(name)
    if fn is None:
        return f"ERROR: unknown tool {name}"
    try:
        return str(fn(**args))
    except Exception as e:
        return f"ERROR: {e}"


async def run_agent(
    user_text: str,
    image_parts: list[dict],
    ctx: tools.ToolCtx,
) -> str:
    client = get_client()
    token = tools.set_ctx(ctx)
    try:
        return await asyncio.to_thread(_run_sync, client, user_text, image_parts, ctx)
    finally:
        tools.reset_ctx(token)


_TOOL_NAMES = None


def _get_tool_names() -> set[str]:
    global _TOOL_NAMES
    if _TOOL_NAMES is None:
        _TOOL_NAMES = {fn.__name__ for fn in tools.ALL_TOOLS}
    return _TOOL_NAMES


def _parse_and_run_inline_tools(text: str) -> tuple[list[tuple[str, dict, str]], str]:
    """Ищет inline tool calls в тексте вида tool_name(args...) и выполняет их.
    Возвращает (список (name, args, result), очищенный текст)."""
    executed = []
    clean = text
    tool_names = _get_tool_names()

    # паттерн: имя_тула(...)
    pattern = re.compile(r'\b(' + '|'.join(re.escape(n) for n in tool_names) + r')\s*\(([^)]*)\)')

    _kwarg_re = re.compile(r'(\w+)\s*=\s*(' + r"'[^']*'" + r'|"[^"]*"|-?\d+(?:\.\d+)?|True|False|None)')

    for m in pattern.finditer(text):
        name = m.group(1)
        args_str = m.group(2).strip()
        args = {}
        if args_str:
            # парсим python-style kwargs: key='val', key2=123
            for km in _kwarg_re.finditer(args_str):
                k = km.group(1)
                try:
                    args[k] = ast.literal_eval(km.group(2))
                except Exception:
                    args[k] = km.group(2).strip("'\"")
            # если kwargs не распарсились — пробуем JSON
            if not args:
                try:
                    args = json.loads(args_str)
                except Exception:
                    continue

        result = _call_tool(name, args)
        log.info("inline tool %s(%s) -> %s", name, args, result[:80])
        executed.append((name, args, result))
        # убираем вызов из текста
        clean = clean.replace(m.group(0), "").strip()

    return executed, clean


def _run_sync(
    client: OpenAI,
    user_text: str,
    image_parts: list[dict],
    ctx: tools.ToolCtx,
) -> str:
    model = os.getenv("MODEL", "google/gemini-2.5-flash")
    system = _system_prompt(ctx)

    # строим user-сообщение: текст + картинки, с тегом отправителя
    sender_id = ctx.trigger_msg.sender_id if ctx.trigger_msg else None
    sender_tag = ""
    if sender_id:
        # имя и username из объекта сообщения (кэш Telethon)
        tg_sender = getattr(ctx.trigger_msg, "sender", None)
        tg_first = getattr(tg_sender, "first_name", None) or ""
        tg_last = getattr(tg_sender, "last_name", None) or ""
        tg_username = getattr(tg_sender, "username", None)
        tg_name = (tg_first + " " + tg_last).strip() or None

        # дополнительно смотрим что запомнили в профиле
        saved_info = memory.list_user_info(sender_id)
        saved_name = next((v for k, v in saved_info if k in ("имя", "name", "tag")), None)

        display_name = saved_name or tg_name or f"user_id={sender_id}"
        parts = [f"От: {display_name}"]
        parts.append(f"user_id={sender_id}")
        if tg_username:
            parts.append(f"@{tg_username}")
        sender_tag = f"[{', '.join(parts)}] "

    user_content: list[dict] = []
    if user_text:
        user_content.append({"type": "text", "text": sender_tag + user_text})
    for p in image_parts:
        if p.get("type") == "input_audio":
            user_content.append(p)
        elif p.get("image_url", {}).get("url"):
            user_content.append({"type": "image_url", "image_url": p["image_url"]})
    if not user_content:
        user_content.append({"type": "text", "text": sender_tag + "Опиши/проанализируй медиа."})

    hist = list(_history[ctx.chat_id])
    messages: list[dict] = [
        {"role": "system", "content": system},
        *hist,
        {"role": "user", "content": user_content},
    ]

    # tool-loop (макс 8 итераций)
    for _ in range(15):
        log.info("calling model: %s (history=%d)", model, len(hist))
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=_oa_tools(),
            tool_choice="auto",
            temperature=0.7,
            max_tokens=8192,
        )
        if not getattr(response, "choices", None):
            err = getattr(response, "error", None) or response.model_dump()
            log.error("empty response from model: %s", str(err)[:500])
            return f"Модель вернула ошибку: {str(err)[:200]}"
        msg = response.choices[0].message

        # нет tool_calls — проверяем не написала ли модель вызов текстом
        if not msg.tool_calls:
            final = msg.content or "(пусто)"
            # парсим inline tool calls вида: tool_name(key='val', ...) или tool_name({'key': 'val'})
            executed, clean = _parse_and_run_inline_tools(final)
            if executed:
                # если всё сообщение было только тул-коллами — продолжаем цикл с результатами
                tool_results_text = "\n".join(f"{name}({args}) → {res}" for name, args, res in executed)
                messages.append({"role": "assistant", "content": final})
                messages.append({"role": "user", "content": f"[Результаты выполненных операций: {tool_results_text}]"})
                # если после очистки ничего не осталось — продолжаем цикл за ответом
                if not clean.strip():
                    continue
                final = clean
            # упрощённый текст user для истории (с тегом отправителя)
            user_log = sender_tag + (user_text or "(медиа)")
            _history[ctx.chat_id].append({"role": "user", "content": user_log})
            _history[ctx.chat_id].append({"role": "assistant", "content": final})
            memory.log_message(ctx.chat_id, "user", user_log)
            memory.log_message(ctx.chat_id, "assistant", final)
            return final

        # добавляем ответ модели в историю
        messages.append(msg)

        # выполняем все tool_calls
        vision_before = len(ctx.pending_vision)
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            log.info("tool %s(%s) calling...", tc.function.name, args)
            result = _call_tool(tc.function.name, args)
            if result.startswith("ERROR"):
                log.warning("tool %s -> %s", tc.function.name, result[:500])
            else:
                log.info("tool %s -> %s…", tc.function.name, result[:80])
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": result,
            })

        # если тул добавил изображения в pending_vision — передаём их модели
        new_vision = ctx.pending_vision[vision_before:]
        if new_vision:
            vision_parts = [{"type": "text", "text": "Изображения от тулов:"}]
            for url in new_vision:
                vision_parts.append({"type": "image_url", "image_url": {"url": url}})
            messages.append({"role": "user", "content": vision_parts})

    return "(превышен лимит итераций tool-loop)"
