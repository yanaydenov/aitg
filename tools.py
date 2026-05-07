"""Тулы для агента. Все функции — обычные sync-вызовы (oauth-codex run_tools их зовёт).

Контекст (chat_id, owner_id, telethon client, current message) передаётся через
ContextVar, чтобы тул-функции имели чистые сигнатуры для JSON-schema автогена.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

import httpx
import trafilatura
try:
    from ddgs import DDGS
except ImportError:  # fallback на старый пакет
    from duckduckgo_search import DDGS  # type: ignore
from telethon import TelegramClient
from telethon.tl.custom import Message

import memory

log = logging.getLogger("aitg.tools")

# простой кэш для crypto_price чтобы избежать 429
_crypto_cache: dict[str, tuple[str, float]] = {}
_CRYPTO_CACHE_TTL = 60  # секунд


@dataclass
class ToolCtx:
    tg: TelegramClient
    chat_id: int
    owner_id: int
    is_owner: bool
    trigger_msg: Optional[Message]
    loop: Optional[asyncio.AbstractEventLoop] = None
    pending_images: list = None  # type: ignore
    input_images: list = None  # type: ignore
    pending_vision: list = None  # type: ignore  # data URLs для передачи модели как vision

    def __post_init__(self):
        if self.pending_images is None:
            self.pending_images = []
        if self.input_images is None:
            self.input_images = []
        if self.pending_vision is None:
            self.pending_vision = []


_ctx: contextvars.ContextVar[ToolCtx] = contextvars.ContextVar("aitg_ctx")


def set_ctx(ctx: ToolCtx) -> contextvars.Token:
    return _ctx.set(ctx)


def reset_ctx(token: contextvars.Token) -> None:
    _ctx.reset(token)


def _ctx_get() -> ToolCtx:
    return _ctx.get()


# ----------------------------- web -----------------------------

def web_search(query: str, max_results: int = 5) -> str:
    """Ищет в вебе через DuckDuckGo. Возвращает JSON со списком {title,url,snippet}."""
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=max_results))
    out = [
        {"title": r.get("title"), "url": r.get("href"), "snippet": r.get("body")}
        for r in results
    ]
    return json.dumps(out, ensure_ascii=False)


def fetch_url(url: str, max_chars: int = 6000) -> str:
    """Качает страницу и возвращает основной текст (trafilatura)."""
    try:
        with httpx.Client(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 aitg"}) as c:
            r = c.get(url)
            r.raise_for_status()
            html = r.text
    except Exception as e:
        return f"ERROR: {e}"
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    return text[:max_chars] or "ERROR: empty extraction"


# ----------------------------- инфа --------------------------

def weather(location: str) -> str:
    """Текущая погода через wttr.in (без ключей)."""
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get(f"https://wttr.in/{location}", params={"format": "j1"})
            r.raise_for_status()
            d = r.json()
        cur = d["current_condition"][0]
        return json.dumps({
            "location": location,
            "temp_c": cur["temp_C"],
            "feels_c": cur["FeelsLikeC"],
            "desc": cur["weatherDesc"][0]["value"],
            "humidity": cur["humidity"],
            "wind_kmh": cur["windspeedKmph"],
        }, ensure_ascii=False)
    except Exception as e:
        return f"ERROR: {e}"


def fx_rate(base: str, quote: str) -> str:
    """Курс валюты base->quote через frankfurter.app."""
    try:
        with httpx.Client(timeout=15) as c:
            r = c.get("https://api.frankfurter.app/latest", params={"from": base.upper(), "to": quote.upper()})
            r.raise_for_status()
            d = r.json()
        return json.dumps({"base": base.upper(), "quote": quote.upper(), "rate": d["rates"][quote.upper()], "date": d["date"]})
    except Exception as e:
        return f"ERROR: {e}"


def crypto_price(symbol: str, vs: str = "usd") -> str:
    """Цена крипты через CoinGecko. symbol — id или тикер монеты (bitcoin, eth, ton, toncoin ...). Возвращает цену, 24ч изменение и 7-дневный sparkline."""
    cache_key = f"{symbol.lower()}_{vs.lower()}"
    cached, cached_time = _crypto_cache.get(cache_key, (None, 0))
    if cached and time.time() - cached_time < _CRYPTO_CACHE_TTL:
        log.info("crypto_price: cache hit for %s", cache_key)
        return cached

    try:
        with httpx.Client(timeout=15) as c:
            # сначала пробуем как id
            r = c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": symbol.lower(), "vs_currencies": vs.lower(),
                        "include_24hr_change": "true", "include_market_cap": "true"},
            )
            r.raise_for_status()
            d = r.json()
            coin_id = symbol.lower()

            # если пусто — ищем правильный id через /search
            if not d:
                r2 = c.get("https://api.coingecko.com/api/v3/search", params={"query": symbol})
                r2.raise_for_status()
                coins = (r2.json() or {}).get("coins", [])
                if not coins:
                    return f"ERROR: монета '{symbol}' не найдена на CoinGecko"
                coin_id = coins[0]["id"]
                r = c.get(
                    "https://api.coingecko.com/api/v3/simple/price",
                    params={"ids": coin_id, "vs_currencies": vs.lower(),
                            "include_24hr_change": "true", "include_market_cap": "true"},
                )
                r.raise_for_status()
                d = r.json()

            # дополнительно вытаскиваем 7д sparkline для графика тренда
            try:
                r3 = c.get(
                    f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart",
                    params={"vs_currency": vs.lower(), "days": "7"},
                )
                if r3.status_code == 200:
                    prices = [p[1] for p in r3.json().get("prices", [])]
                    # прорежаем до 20 точек
                    if len(prices) > 20:
                        step = len(prices) // 20
                        prices = prices[::step][:20]
                    d[coin_id]["sparkline_7d"] = [round(p, 4) for p in prices]
            except Exception:
                pass

        result = json.dumps(d, ensure_ascii=False)
        _crypto_cache[cache_key] = (result, time.time())
        return result
    except Exception as e:
        if "429" in str(e):
            return f"ERROR: CoinGecko rate limit exceeded. Попробуй через минуту или используй web_search."
        return f"ERROR: {e}"


# --------------------------- telegram --------------------------

def read_chat_history(limit: int = 50) -> str:
    """Читает последние limit сообщений ТЕКУЩЕГО чата (того откуда пришла команда). НЕ использовать если пользователь назвал имя другого чата — для этого find_chat + read_other_chat. Возвращает JSON [{from,text,ts}]."""
    ctx = _ctx_get()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 50
    limit = max(1, min(limit, 200))

    async def _run():
        log.info("read_chat_history: start chat_id=%s limit=%s", ctx.chat_id, limit)
        msgs = []
        raw = []
        sender_ids: set[int] = set()
        async for m in ctx.tg.iter_messages(ctx.chat_id, limit=limit):
            if not (m.text or m.message):
                continue
            raw.append(m)
            if m.sender_id:
                sender_ids.add(m.sender_id)
        log.info("read_chat_history: got %d msgs, %d unique senders", len(raw), len(sender_ids))
        names: dict[int, str] = {}
        for sid in sender_ids:
            try:
                e = await asyncio.wait_for(ctx.tg.get_entity(sid), timeout=5)
                names[sid] = getattr(e, "first_name", None) or getattr(e, "title", None) or str(sid)
            except Exception:
                names[sid] = str(sid)
        for m in raw:
            msgs.append({
                "from": names.get(m.sender_id or 0, str(m.sender_id or "?")),
                "text": (m.message or m.text or "")[:500],
                "ts": m.date.isoformat() if m.date else None,
            })
        msgs.reverse()
        return msgs

    fut = asyncio.run_coroutine_threadsafe(_run(), ctx.loop)
    try:
        msgs = fut.result(timeout=60)
    except Exception as e:
        fut.cancel()
        log.exception("read_chat_history failed (chat_id=%s, limit=%s)", ctx.chat_id, limit)
        return f"ERROR read_chat_history: {type(e).__name__}: {e}"
    return json.dumps(msgs, ensure_ascii=False)


def read_link_preview(link: str, limit: int = 30) -> str:
    """Читает последние limit сообщений из канала/группы по @username или t.me/ссылке. Если не сработало — попробуй find_chat."""
    ctx = _ctx_get()
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 30
    limit = max(1, min(limit, 200))

    # нормализуем ссылку
    link = (link or "").strip()
    if link.startswith("https://t.me/"):
        link = link.replace("https://t.me/", "")
    elif link.startswith("http://t.me/"):
        link = link.replace("http://t.me/", "")
    elif link.startswith("t.me/"):
        link = link.replace("t.me/", "")
    if link and not link.startswith("@"):
        link = "@" + link

    async def _run():
        log.info("read_link_preview: trying link=%s", link)
        try:
            entity = await asyncio.wait_for(ctx.tg.get_entity(link), timeout=10)
            log.info("read_link_preview: got entity=%s", entity)
        except Exception as e:
            log.warning("read_link_preview: get_entity failed for %s: %s", link, e)
            return [{"error": f"не удалось получить доступ к {link}: {type(e).__name__}. Попробуй find_chat чтобы найти чат по названию."}]
        msgs = []
        async for m in ctx.tg.iter_messages(entity, limit=limit):
            if not (m.text or m.message):
                continue
            msgs.append({"text": (m.message or m.text or "")[:500], "ts": m.date.isoformat() if m.date else None})
        msgs.reverse()
        log.info("read_link_preview: got %d messages", len(msgs))
        return msgs

    fut = asyncio.run_coroutine_threadsafe(_run(), ctx.loop)
    try:
        res = fut.result(timeout=30)
    except Exception as e:
        fut.cancel()
        log.exception("read_link_preview failed")
        return f"ERROR read_link_preview: {type(e).__name__}: {e}"
    return json.dumps(res, ensure_ascii=False)


_RU2EN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "i", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_EN2RU = {
    "sch": "щ", "sh": "ш", "ch": "ч", "ts": "ц", "yu": "ю", "ya": "я",
    "zh": "ж", "yo": "ё",
    "a": "а", "b": "б", "c": "к", "d": "д", "e": "е", "f": "ф", "g": "г",
    "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л", "m": "м", "n": "н",
    "o": "о", "p": "п", "q": "к", "r": "р", "s": "с", "t": "т", "u": "у",
    "v": "в", "w": "в", "x": "кс", "y": "й", "z": "з",
}


def _translit_ru_en(s: str) -> str:
    return "".join(_RU2EN.get(c, c) for c in s.lower())


def _translit_en_ru(s: str) -> str:
    s = s.lower()
    out = []
    i = 0
    while i < len(s):
        # сначала пробуем триграмму, потом биграмму
        for n in (3, 2, 1):
            chunk = s[i:i + n]
            if chunk in _EN2RU:
                out.append(_EN2RU[chunk])
                i += n
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def _norm(s: str) -> str:
    return "".join(c for c in (s or "").lower() if c.isalnum())


def find_chat(query: str) -> str:
    """Ищет чат/группу/канал/диалог среди диалогов владельца по части названия (с поддержкой транслитерации ru/en). Возвращает JSON [{id,title,type,score}]. Используй id чтобы потом читать read_other_chat."""
    import difflib
    ctx = _ctx_get()
    q_raw = (query or "").strip()
    q_norm = _norm(q_raw)
    q_variants = {q_norm, _norm(_translit_ru_en(q_raw)), _norm(_translit_en_ru(q_raw))}
    q_variants = {v for v in q_variants if v}

    async def _run():
        log.info("find_chat: query=%r variants=%s", q_raw, q_variants)
        candidates = []
        scanned = 0
        async for d in ctx.tg.iter_dialogs(limit=400):
            scanned += 1
            title = d.name or ""
            t_variants = {_norm(title), _norm(_translit_ru_en(title)), _norm(_translit_en_ru(title))}
            t_variants = {v for v in t_variants if v}

            best = 0.0
            for q in q_variants:
                for t in t_variants:
                    if q and q in t:
                        best = max(best, 1.0)
                    else:
                        best = max(best, difflib.SequenceMatcher(None, q, t).ratio())
            if best >= 0.6:
                kind = "channel" if d.is_channel else ("group" if d.is_group else "user")
                candidates.append({"id": d.id, "title": title, "type": kind, "score": round(best, 2)})
        # сортируем: сначала по score, затем каналы/группы выше пользователей
        type_priority = {"channel": 3, "group": 2, "user": 1}
        candidates.sort(key=lambda x: (x["score"], type_priority.get(x["type"], 0)), reverse=True)
        log.info("find_chat: scanned %d, %d candidates", scanned, len(candidates))
        return candidates[:10]

    fut = asyncio.run_coroutine_threadsafe(_run(), ctx.loop)
    try:
        res = fut.result(timeout=60)
    except Exception as e:
        fut.cancel()
        return f"ERROR find_chat: {type(e).__name__}: {e}"
    return json.dumps(res, ensure_ascii=False)


def list_all_chats() -> str:
    """Возвращает список всех чатов/групп/каналов/диалогов владельца. JSON [{id,title,type}]. Используй для поиска чата по названию - модель сама выберет подходящий."""
    ctx = _ctx_get()

    async def _run():
        log.info("list_all_chats: start")
        chats = []
        async for d in ctx.tg.iter_dialogs(limit=400):
            kind = "channel" if d.is_channel else ("group" if d.is_group else "user")
            chats.append({"id": d.id, "title": d.name or "", "type": kind})
        log.info("list_all_chats: got %d chats", len(chats))
        return chats

    fut = asyncio.run_coroutine_threadsafe(_run(), ctx.loop)
    try:
        res = fut.result(timeout=60)
    except Exception as e:
        fut.cancel()
        return f"ERROR list_all_chats: {type(e).__name__}: {e}"
    return json.dumps(res, ensure_ascii=False)


def search_messages(query: str, chat_id: int | None = None, limit: int = 20) -> str:
    """Ищет сообщения содержащие query. Если chat_id не указан — ищет по всем чатам. Возвращает JSON [{chat,from,text,ts}]. Используй для поиска конкретных слов/фраз в переписках."""
    ctx = _ctx_get()
    limit = max(1, min(int(limit), 100))

    async def _run():
        results = []
        try:
            if chat_id is not None:
                peer = int(chat_id)
                async for m in ctx.tg.iter_messages(peer, search=query, limit=limit):
                    if not (m.text or m.message):
                        continue
                    sender = getattr(m.sender, "first_name", None) or getattr(m.sender, "title", None) or str(m.sender_id)
                    results.append({"chat": peer, "from": sender, "text": m.text or m.message, "ts": m.date.isoformat()})
            else:
                # поиск по всем диалогам через глобальный поиск
                async for m in ctx.tg.iter_messages(None, search=query, limit=limit):
                    if not (m.text or m.message):
                        continue
                    chat_name = None
                    try:
                        dlg_entity = await ctx.tg.get_entity(m.peer_id)
                        chat_name = getattr(dlg_entity, "first_name", None) or getattr(dlg_entity, "title", None)
                    except Exception:
                        pass
                    sender = getattr(m.sender, "first_name", None) or getattr(m.sender, "title", None) or str(m.sender_id)
                    results.append({"chat": chat_name or str(m.peer_id), "from": sender, "text": m.text or m.message, "ts": m.date.isoformat()})
        except Exception as e:
            return [{"error": str(e)}]
        return results

    fut = asyncio.run_coroutine_threadsafe(_run(), ctx.loop)
    try:
        res = fut.result(timeout=60)
    except Exception as e:
        fut.cancel()
        return f"ERROR search_messages: {type(e).__name__}: {e}"
    return json.dumps(res, ensure_ascii=False)


def read_other_chat(chat_id: int, limit: int = 50, include_media: bool = False, since_hours: int | None = None) -> str:
    """Читает последние limit сообщений из конкретного чата по его id. Возвращает JSON [{from,text,ts,has_media}] от старых к новым. since_hours=N — только за последние N часов (например since_hours=24 = за сегодня). Для вопросов 'что сегодня/недавно' используй since_hours=24 и limit=200."""
    ctx = _ctx_get()
    try:
        chat_id = int(chat_id)
        limit = int(limit)
    except (TypeError, ValueError):
        return "ERROR: chat_id и limit должны быть числами"
    limit = max(1, min(limit, 500))

    async def _run():
        import datetime as _dt
        msgs = []
        raw = []
        sender_ids: set[int] = set()
        min_date = None
        if since_hours is not None:
            min_date = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=int(since_hours))
        async for m in ctx.tg.iter_messages(chat_id, limit=limit):
            if not (m.text or m.message) and not m.media:
                continue
            if min_date and m.date:
                msg_date = m.date if m.date.tzinfo else m.date.replace(tzinfo=_dt.timezone.utc)
                if msg_date < min_date:
                    break
            raw.append(m)
            if m.sender_id:
                sender_ids.add(m.sender_id)
        names: dict[int, str] = {}
        for sid in sender_ids:
            try:
                e = await asyncio.wait_for(ctx.tg.get_entity(sid), timeout=5)
                names[sid] = getattr(e, "first_name", None) or getattr(e, "title", None) or str(sid)
            except Exception:
                names[sid] = str(sid)
        for m in raw:
            entry = {
                "from": names.get(m.sender_id or 0, str(m.sender_id or "?")),
                "text": (m.message or m.text or "")[:500],
                "ts": m.date.isoformat() if m.date else None,
                "has_media": bool(m.media),
            }
            if include_media and m.media:
                try:
                    import media
                    parts = await media.message_to_image_parts(m)
                    entry["image_urls"] = [p.get("image_url", {}).get("url", "") for p in parts if p.get("image_url", {}).get("url")]
                except Exception as e:
                    log.warning("failed to extract media from msg %s: %s", m.id, e)
                    entry["image_urls"] = []
            msgs.append(entry)
        msgs.reverse()
        return msgs

    fut = asyncio.run_coroutine_threadsafe(_run(), ctx.loop)
    try:
        msgs = fut.result(timeout=30)
    except Exception as e:
        fut.cancel()
        log.exception("read_other_chat failed (chat_id=%s)", chat_id)
        return f"ERROR read_other_chat: {type(e).__name__}: {e}"
    return json.dumps(msgs, ensure_ascii=False)


# ---------------------------- картинки -------------------------

def generate_image(prompt: str) -> str:
    """Генерирует/редактирует картинку по описанию. Если пользователь приложил фото (в сообщении или реплае) — они автоматически используются как референсы для face-swap, редактирования, стилизации и т.п. Картинка сама отправится в чат после ответа. Возвращает 'OK' или 'ERROR'."""
    ctx = _ctx_get()
    import base64
    import tempfile
    import time

    api_key = os.getenv("OPENROUTER_API_KEY", "")
    image_model = os.getenv("IMAGE_MODEL", "google/gemini-3.1-flash-image-preview")

    # собираем контент: текст + все входные картинки как референсы
    content: list[dict] = [{"type": "text", "text": prompt}]
    for p in ctx.input_images:
        content.append(p)

    log.info("generate_image: prompt=%r with %d reference images", prompt[:80], len(ctx.input_images))

    try:
        with httpx.Client(timeout=120) as c:
            r = c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": image_model,
                    "messages": [{"role": "user", "content": content}],
                    "modalities": ["image", "text"],
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        log.exception("generate_image http failed")
        return f"ERROR generate_image: {e}"

    # ищем base64 картинку в ответе
    msg = (data.get("choices") or [{}])[0].get("message") or {}
    images = msg.get("images") or []
    b64 = None
    if images:
        url = images[0].get("image_url", {}).get("url", "")
        if url.startswith("data:") and "," in url:
            b64 = url.split(",", 1)[1]
    if not b64:
        log.warning("generate_image: no image in response: %s", str(data)[:300])
        return f"ERROR generate_image: модель не вернула картинку"

    raw = base64.b64decode(b64)
    path = os.path.join(tempfile.gettempdir(), f"aitg_img_{int(time.time()*1000)}.png")
    with open(path, "wb") as f:
        f.write(raw)
    ctx.pending_images.append(path)
    log.info("generate_image: saved %s (%d bytes)", path, len(raw))
    return "OK картинка сгенерирована и будет отправлена"


# ---------------------------- память ---------------------------

def memory_remember(key: str, value: str, scope: str = "chat") -> str:
    """Сохраняет факт. scope=chat|global. global доступен только владельцу."""
    ctx = _ctx_get()
    glob = scope == "global"
    if glob and not ctx.is_owner:
        return "ERROR: global memory доступна только владельцу"
    memory.remember(key, value, chat_id=ctx.chat_id, glob=glob)
    return "ok"


def memory_recall(key: str, scope: str = "chat") -> str:
    """Читает факт. scope=chat|global."""
    ctx = _ctx_get()
    glob = scope == "global"
    if glob and not ctx.is_owner:
        return "ERROR: global memory доступна только владельцу"
    v = memory.recall(key, chat_id=ctx.chat_id, glob=glob)
    return v if v is not None else "NOT_FOUND"


def user_remember(user_id: int, key: str, value: str) -> str:
    """Сохраняет информацию о конкретном человеке по его telegram ID. Например: user_remember(123456, 'tag', 'соня'), user_remember(123456, 'relation', 'девушка')."""
    memory.remember_user(user_id, key, value)
    return "ok"


def user_recall(user_id: int, key: str) -> str:
    """Читает информацию о конкретном человеке по его telegram ID."""
    v = memory.recall_user(user_id, key)
    return v if v is not None else "NOT_FOUND"


def user_list(user_id: int) -> str:
    """Возвращает всю сохранённую информацию о конкретном человеке по его telegram ID. JSON [{key, value}]."""
    info = memory.list_user_info(user_id)
    return json.dumps([{"key": k, "value": v} for k, v in info], ensure_ascii=False)


def user_forget(user_id: int, key: str) -> str:
    """Удаляет конкретную информацию о человеке."""
    ok = memory.forget_user(user_id, key)
    return "ok" if ok else "NOT_FOUND"


def get_user_profile(user_id_or_username: str) -> str:
    """Получает полную информацию о пользователе по telegram ID или @username: имя, username, описание, статус, аватарку (base64)."""
    ctx = _ctx_get()

    async def _run():
        try:
            # если передали числовой ID — ищем сначала в диалогах (там кэшируются все участники)
            arg = user_id_or_username.strip().lstrip("@")
            if arg.lstrip("-").isdigit():
                uid = int(arg)
                # сначала пробуем через PeerUser
                try:
                    from telethon.tl.types import PeerUser
                    entity = await asyncio.wait_for(ctx.tg.get_entity(PeerUser(uid)), timeout=10)
                except Exception:
                    # если не кэшировано — ищем в диалогах
                    entity = None
                    async for d in ctx.tg.iter_dialogs(limit=400):
                        if d.entity and getattr(d.entity, "id", None) == uid:
                            entity = d.entity
                            break
                    if entity is None:
                        return {"error": f"пользователь {uid} не найден в диалогах"}
            else:
                entity = await asyncio.wait_for(ctx.tg.get_entity(f"@{arg}" if not arg.startswith("@") else arg), timeout=10)
        except Exception as e:
            return {"error": f"не удалось получить пользователя: {e}"}

        profile = {
            "id": entity.id,
            "first_name": getattr(entity, "first_name", None),
            "last_name": getattr(entity, "last_name", None),
            "username": getattr(entity, "username", None),
            "phone": getattr(entity, "phone", None),
            "bio": getattr(entity, "about", None) or getattr(entity, "bio", None),
            "status": str(getattr(entity, "status", None)) if hasattr(entity, "status") else None,
        }

        # аватарка — скачиваем, кодируем в base64 для vision и добавляем в pending_images для отправки
        try:
            import tempfile, pathlib, base64 as _b64
            tmp = pathlib.Path(tempfile.mktemp(suffix=".jpg", prefix="aitg_avatar_"))
            path = await ctx.tg.download_profile_photo(entity, file=str(tmp))
            if path and pathlib.Path(path).exists():
                data = pathlib.Path(path).read_bytes()
                b64 = _b64.b64encode(data).decode()
                ctx.pending_vision.append(f"data:image/jpeg;base64,{b64}")
                ctx.pending_images.append(path)
                profile["avatar"] = "изображение передано модели для анализа"
            else:
                profile["avatar"] = "нет фото"
        except Exception as e:
            log.warning("failed to get avatar for %s: %s", user_id_or_username, e)
            profile["avatar"] = f"ошибка: {e}"

        return profile

    fut = asyncio.run_coroutine_threadsafe(_run(), ctx.loop)
    try:
        result = fut.result(timeout=30)
    except Exception as e:
        fut.cancel()
        log.exception("get_user_profile failed")
        return f"ERROR: {e}"
    return json.dumps(result, ensure_ascii=False)


def memory_list(scope: str = "chat") -> str:
    """Список ключей в памяти. scope=chat|global."""
    ctx = _ctx_get()
    glob = scope == "global"
    if glob and not ctx.is_owner:
        return "ERROR: global memory доступна только владельцу"
    items = memory.list_keys(chat_id=ctx.chat_id, glob=glob)
    return json.dumps([{"key": k, "value": v} for k, v in items], ensure_ascii=False)


def memory_forget(key: str, scope: str = "chat") -> str:
    ctx = _ctx_get()
    glob = scope == "global"
    if glob and not ctx.is_owner:
        return "ERROR: global memory доступна только владельцу"
    return "ok" if memory.forget(key, chat_id=ctx.chat_id, glob=glob) else "NOT_FOUND"


def set_reminder(text: str, minutes: int | None = None, at: str | None = None) -> str:
    """Ставит напоминание. minutes=N — через N минут от сейчас. at='YYYY-MM-DDTHH:MM' — в конкретное время UTC (используй UTC время из системного промпта). Возвращает подтверждение с временем."""
    ctx = _ctx_get()
    now = int(time.time())
    if minutes is not None:
        fire_at = now + int(minutes) * 60
    elif at is not None:
        import datetime as _dt
        try:
            dt = _dt.datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            fire_at = int(dt.timestamp())
        except Exception as e:
            return f"ERROR: неверный формат времени: {e}"
    else:
        return "ERROR: укажи minutes или at"
    if fire_at <= now:
        return "ERROR: время напоминания уже прошло"
    user_id = ctx.trigger_msg.sender_id if ctx.trigger_msg else ctx.owner_id
    reminder_id = memory.add_reminder(ctx.chat_id, user_id, text, fire_at)
    import datetime as _dt
    fire_dt = _dt.datetime.fromtimestamp(fire_at, tz=_dt.timezone.utc)
    local_h = (fire_dt.hour + 5) % 24
    return f"ok, напомню в {local_h:02d}:{fire_dt.minute:02d} (id={reminder_id})"


def list_reminders_tool() -> str:
    """Показывает все активные напоминания в текущем чате."""
    ctx = _ctx_get()
    items = memory.list_reminders(ctx.chat_id)
    if not items:
        return "нет активных напоминаний"
    import datetime as _dt
    result = []
    for r in items:
        dt = _dt.datetime.fromtimestamp(r["fire_at"], tz=_dt.timezone.utc)
        local_h = (dt.hour + 5) % 24
        result.append({"id": r["id"], "text": r["text"], "time": f"{dt.strftime('%Y-%m-%d')} {local_h:02d}:{dt.minute:02d}"})
    return json.dumps(result, ensure_ascii=False)


def cancel_reminder(reminder_id: int) -> str:
    """Отменяет напоминание по его id."""
    ctx = _ctx_get()
    ok = memory.cancel_reminder(int(reminder_id), ctx.chat_id)
    return "отменено" if ok else "NOT_FOUND"


def search_log(query: str, all_chats: bool = False, limit: int = 20) -> str:
    """Ищет по истории всех разговоров бота. query — слово или фраза. all_chats=true — искать во всех чатах, иначе только в текущем. Возвращает JSON [{role,content,ts}]."""
    ctx = _ctx_get()
    chat_id = None if all_chats else ctx.chat_id
    results = memory.search_log(query, chat_id=chat_id, limit=max(1, min(int(limit), 100)))
    return json.dumps(results, ensure_ascii=False)


ALL_TOOLS = [
    web_search,
    fetch_url,
    weather,
    fx_rate,
    crypto_price,
    read_chat_history,
    read_link_preview,
    list_all_chats,
    find_chat,
    search_messages,
    read_other_chat,
    generate_image,
    get_user_profile,
    user_remember,
    user_recall,
    user_list,
    user_forget,
    memory_remember,
    memory_recall,
    memory_list,
    memory_forget,
    set_reminder,
    list_reminders_tool,
    cancel_reminder,
    search_log,
]
