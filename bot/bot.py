#!/usr/bin/env python3
import os, json, time, re, html, random, urllib.request, urllib.parse, email.utils
from datetime import datetime, timezone, timedelta

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DATA_DIR", BASE)          # изменяемое состояние: история, offset, лог
os.makedirs(DATA, exist_ok=True)


def env(name, default=None, required=False):
    v = os.environ.get(name, default)
    if required and not v:
        raise SystemExit("Не задана переменная окружения %s (см. .env.example)" % name)
    return v


TOKEN = env("TG_TOKEN", required=True)
API = "https://api.telegram.org/bot" + TOKEN

# ─── мозг: облако или локально ───────────────────────────────────────────
# BRAIN=zai   → облако Z.ai (нужен ZAI_API_KEY)
# BRAIN=local → локальный llama-server
BRAIN = env("BRAIN", "zai").strip().lower()
ZAI_URL = env("ZAI_URL", "https://api.z.ai/api/paas/v4/chat/completions")
ZAI_MODEL = env("ZAI_MODEL", "glm-4.5-flash")
ZAI_KEY = env("ZAI_API_KEY", "")
LLAMA = env("LLAMA_URL", "http://llama:8080/v1/chat/completions")
LOCAL_MODEL = env("LOCAL_MODEL", "local")
CLASSIFIER = env("CLASSIFIER_URL", "http://classifier:8080/v1/chat/completions")

BOT_USERNAME = env("BOT_USERNAME", "").strip().lstrip("@")
KEYWORDS = [k.strip().lower() for k in env("KEYWORDS", "петрович,алкаш").split(",") if k.strip()]
SPAM_REPLY = env("SPAM_REPLY", "ненавижу эту ебучую рекламу")
SPAM_COOLDOWN = int(env("SPAM_COOLDOWN", "60"))
SPAM_HINTS = [h.strip().lower() for h in env("SPAM_HINTS",
    "бесплатн,подпишись,подписаться,заработ,заработай,промокод,крипт,nft,подарк,кейс,"
    "обучени,расклад,инвест,ставк,казино,увлекательн,переходи по ссылк,скидк,выигр,бонус"
).split(",") if h.strip()]
_spam_last = {}
URL_RE = re.compile(r"https?://|t\.me/|www\.", re.I)

PERSONA = env("PERSONA_FILE", os.path.join(BASE, "persona.txt"))
JOKES = env("JOKES_FILE", os.path.join(BASE, "jokes.txt"))
HIST = os.path.join(DATA, "history.json")
OFF = os.path.join(DATA, "offset.txt")
LOG = os.path.join(DATA, "incoming.log")
MAX_MSGS = int(env("MAX_MSGS", "10"))
TZ = timezone(timedelta(hours=float(env("TZ_OFFSET", "2"))))
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"

TOOLS = [{
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Найти информацию в интернете. Используй, когда нужен свежий факт, которого ты не знаешь.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "поисковый запрос"}},
                       "required": ["query"]},
    },
}, {
    "type": "function",
    "function": {
        "name": "random_joke",
        "description": "Взять настоящий анекдот из своей коллекции. Используй, когда хочешь пошутить сам или просят анекдот.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}, {
    "type": "function",
    "function": {
        "name": "get_news",
        "description": "Свежие новости за сегодня из лент Lenta.ru и ТАСС. Используй, когда просят новости / что нового в мире.",
        "parameters": {"type": "object",
                       "properties": {"limit": {"type": "integer", "description": "сколько новостей, по умолчанию 6"}},
                       "required": []},
    },
}]

FEEDS = ["https://lenta.ru/rss/news", "https://tass.ru/rss/v2.xml"]


def http_json(url, payload=None, timeout=120, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def backend():
    """Куда идти: облако Z.ai или локальный llama-server."""
    if BRAIN == "zai" and ZAI_KEY:
        return ZAI_URL, ZAI_MODEL, {"Authorization": "Bearer " + ZAI_KEY}
    return LLAMA, LOCAL_MODEL, None


def tg(method, **params):
    r = http_json(API + "/" + method, params, timeout=120)
    if method == "sendMessage":
        try:
            ok = (r or {}).get("ok")
            mid = ((r or {}).get("result") or {}).get("message_id")
            print("  <- sent chat=%s msg_id=%s ok=%s len=%s" % (
                params.get("chat_id"), mid, ok, len(params.get("text") or "")), flush=True)
        except Exception:
            pass
    return r


SEARCH_CACHE = {}


def _http_text(url, data=None):
    hdrs = {"User-Agent": UA, "Accept-Language": "ru,en;q=0.8"}
    if data:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=hdrs)
    return urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "ignore")


def _clean(x):
    return html.unescape(re.sub(r"<[^>]+>", "", x)).strip()


_brave_last = [0.0]


def _search_brave(q):
    """Brave: пауза между запросами и один ретрай на 429 (иначе отдаёт Too Many Requests)."""
    for attempt in range(2):
        wait = 8 - (time.time() - _brave_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            h = _http_text("https://search.brave.com/search?q=" + urllib.parse.quote(q))
        except Exception as e:
            _brave_last[0] = time.time()
            if attempt == 0 and "429" in str(e):
                continue
            raise
        _brave_last[0] = time.time()
        out = []
        for blk in re.split(r"<div class=\"snippet ", h)[1:]:
            blk = blk[:6000]
            t = re.search(r"search-snippet-title[^>]*>(.*?)</div>", blk, re.S)
            if not t:
                continue
            s = re.search(r"generic-snippet[^>]*>.*?class=\"content[^\"]*\"[^>]*>(.*?)</div>", blk, re.S)
            u = re.search(r"<a href=\"(https?://[^\"]+)\"", blk)
            out.append((_clean(t.group(1)), _clean(s.group(1))[:300] if s else "",
                        u.group(1) if u else ""))
            if len(out) >= 5:
                break
        return out
    return []


def _search_ddg(q):
    h = _http_text("https://lite.duckduckgo.com/lite/",
                   urllib.parse.urlencode({"q": q, "kl": "ru-ru"}).encode())
    links = re.findall(r"<a[^>]+class=.result-link.[^>]*>(.*?)</a>", h, re.S)
    snips = re.findall(r"class=.result-snippet.>(.*?)</td>", h, re.S)
    out = []
    for i in range(min(5, len(links))):
        out.append((_clean(links[i]), _clean(snips[i])[:300] if i < len(snips) else "", ""))
    return out


def _search_wiki(q):
    d = json.loads(_http_text("https://ru.wikipedia.org/w/api.php?action=query&list=search"
                              "&srsearch=" + urllib.parse.quote(q) + "&format=json&srlimit=4"))
    out = []
    for x in d.get("query", {}).get("search", []):
        out.append((_clean(x["title"]), _clean(x["snippet"])[:300],
                    "https://ru.wikipedia.org/wiki/" + urllib.parse.quote(x["title"].replace(" ", "_"))))
    return out


def web_search(query):
    """Поиск с каскадом: Brave -> DDG -> Wikipedia. Побеждает первый, кто отдал результаты."""
    query = (query or "").strip()
    if not query:
        return "Пустой запрос."
    if query in SEARCH_CACHE:
        return SEARCH_CACHE[query]
    for name, fn in (("brave", _search_brave), ("ddg", _search_ddg), ("wiki", _search_wiki)):
        try:
            rows = fn(query)
        except Exception as e:
            print("search %s err: %s" % (name, str(e)[:120]), flush=True)
            continue
        if rows:
            print("search %s -> %d" % (name, len(rows)), flush=True)
            res = "\n\n".join("%d. %s\n%s\n%s" % (i, t, s, u) for i, (t, s, u) in enumerate(rows, 1))
            SEARCH_CACHE[query] = res
            if len(SEARCH_CACHE) > 50:
                SEARCH_CACHE.clear()
            return res
    return "Ничего не нашлось."


def get_news(limit=6, hours=24):
    """Свежие заголовки из RSS. Надёжнее поиска: нет капчи, всегда сегодняшние."""
    now = datetime.now(TZ)
    rows = []
    for feed in FEEDS:
        try:
            req = urllib.request.Request(feed, headers={"User-Agent": UA})
            xml = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        except Exception:
            continue
        src = "Lenta" if "lenta" in feed else "ТАСС"
        for m in re.finditer(r"<item>(.*?)</item>", xml, re.S):
            b = m.group(1)

            def g(tag):
                mm = re.search(r"<%s>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</%s>" % (tag, tag), b, re.S)
                return html.unescape(mm.group(1).strip()) if mm else ""

            title, pub = g("title"), g("pubDate")
            if not title:
                continue
            try:
                dt = email.utils.parsedate_to_datetime(pub).astimezone(TZ)
            except Exception:
                continue
            if (now - dt).total_seconds() > hours * 3600:
                continue
            rows.append((dt, title, src))
    rows.sort(key=lambda r: r[0], reverse=True)
    seen, out = set(), []
    for dt, title, src in rows:
        key = title.lower()[:40]
        if key in seen:
            continue
        seen.add(key)
        out.append("%s (%s) — %s" % (dt.strftime("%H:%M"), src, title))
        if len(out) >= limit:
            break
    return "\n".join(out) if out else "Новости не загрузились."


NEWS_RE = re.compile(r"новост|что нового|что происходит в мире|что там в мире", re.I)
SEARCH_RE = re.compile(r"кто так|что так|сколько|курс|погод|цена|стоимост|где находит|"
                         r"в каком году|когда родил|когда умер|расскажи про|правда ли что", re.I)
JOKE_RE = re.compile(r"анекдот|шутк|пошути|рассмеши|прикол|юмор", re.I)


def random_joke():
    """Настоящий анекдот из файла — модель их сочинять не умеет."""
    try:
        raw = open(JOKES, encoding="utf-8").read()
    except Exception:
        return ""
    jokes = [b.strip() for b in raw.split("\n\n") if b.strip()]
    return random.choice(jokes) if jokes else ""


def should_reply(m):
    """Только группы, и только если позвали. В личке молчит — иначе задёргают."""
    if (m.get("chat") or {}).get("type") == "private":
        return False
    text = m.get("text") or ""
    low = text.lower()
    if "@" + BOT_USERNAME.lower() in low:
        return True
    if any(k in low for k in KEYWORDS):
        return True
    rt = (m.get("reply_to_message") or {}).get("from") or {}
    if (rt.get("username") or "").lower() == BOT_USERNAME.lower():
        return True
    return False


def is_spam(text):
    """Реклама или нет. Работает только для сообщений от ботов."""
    low = text.lower()
    if any(h in low for h in SPAM_HINTS):
        return True
    # нейронка склонна называть спамом живую речь (14/56 ложных на реальных сообщениях),
    # поэтому её слово учитывается только когда в сообщении есть ссылка
    if not URL_RE.search(text):
        return False
    try:
        p = {"model": "c", "messages": [
            {"role": "system", "content": "Спам или нет? Ответь одним словом: СПАМ или ОК."},
            {"role": "user", "content": text[:800]}], "max_tokens": 6, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False}}
        r = http_json(CLASSIFIER, p, timeout=30)
        out = (r["choices"][0]["message"].get("content") or "").upper()
        return "СПАМ" in out
    except Exception as e:
        print("classifier err: %s" % e, flush=True)
        return False


# --- анти-повтор: механический предохранитель, а не надежда на промпт ---
STOPWORDS = set("""который которая которое которые этого этому этом чтобы очень просто
значит вообще всегда совсем может можно нужно надо быть есть был была было были будет
только даже если когда тогда здесь там такой такая такое такие тебя меня себя нам вам
сказал сказала говорю говорит делай делаешь делать хочешь хочу знаешь знаю думаю вижу
давай давайте ладно хорошо плохо ничего почему-то человек мужик петрович сегодня вчера
завтра потому поэтому кстати между потом после перед около чтобы разве неужели
конечно наверное кажется помню помнишь знаешь""".split())


def _words(t):
    return set(w for w in re.findall(r"[а-яёa-z]{5,}", (t or "").lower()) if w not in STOPWORDS)


def crutch_hits(text, prev_texts):
    """Слова, которые уже были в 2+ из прошлых ответов и снова вылезли. Это зацикливание."""
    if not prev_texts:
        return []
    cnt = {}
    for pt in prev_texts[:3]:
        for w in _words(pt):
            cnt[w] = cnt.get(w, 0) + 1
    return sorted(w for w in _words(text) if cnt.get(w, 0) >= 2)


def build_payload(url, model, convo, force_answer):
    payload = {"model": model, "messages": convo, "max_tokens": 250, "temperature": 0.8}
    if not force_answer:
        if url == ZAI_URL:
            payload["tools"] = [{"type": "web_search",
                                 "web_search": {"enable": True, "search_result": True}}]
        else:
            payload["tools"] = TOOLS
            payload["tool_choice"] = "auto"
    if url == ZAI_URL:
        payload["thinking"] = {"type": "disabled"}
    else:
        payload["repeat_penalty"] = 1.15
        payload["presence_penalty"] = 0.3
        payload["chat_template_kwargs"] = {"enable_thinking": False}
    return payload


def load(path, default):
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return default


def save(path, obj):
    tmp = path + ".tmp"
    json.dump(obj, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, path)


def ask(chat_id, user_text):
    hist = load(HIST, {})
    msgs = hist.get(str(chat_id), [])
    msgs.append({"role": "user", "content": user_text})
    system = open(PERSONA, encoding="utf-8").read().strip()
    now = datetime.now(TZ)
    system += "\n\nСЕЙЧАС: %s, %s, %s (UTC+2). Это точная дата — не выдумывай другую." % (
        now.strftime("%d.%m.%Y"), WEEKDAYS[now.weekday()], now.strftime("%H:%M"))
    prefetched_news = False
    if NEWS_RE.search(user_text):
        # тянем ленты заранее: модель не может «забыть» поискать
        system += ("\n\nСВЕЖИЕ НОВОСТИ ЗА СЕГОДНЯ (только что из лент, время UTC+2):\n"
                   + get_news(8)
                   + "\n\nПерескажи 6 из них своими словами, по одной короткой фразе на каждую, "
                     "в своём стиле. Ничего не выдумывай сверх этого списка.")
        prefetched_news = True
    if JOKE_RE.search(user_text):
        j = random_joke()
        if j:
            system += ("\n\nВот анекдот, который ты сейчас рассказываешь. Перескажи его своими "
                       "словами, коротко, в своём стиле. Суть не меняй, мораль в конце не добавляй:\n" + j)
    if SEARCH_RE.search(user_text) and not prefetched_news:
        # встроенный поиск Z.ai отдаёт устаревшее — берём свежее сами и кладём в промпт
        try:
            found = web_search(user_text[:200])
        except Exception as e:
            found = ""
            print("prefetch search err: %s" % str(e)[:120], flush=True)
        if found and found != "Ничего не нашлось.":
            system += ("\n\nСВЕЖИЕ РЕЗУЛЬТАТЫ ПОИСКА (только что, интернет):\n" + found[:2000]
                       + "\n\nОпирайся на них. Если данных не хватает — скажи честно, не выдумывай.")
    if prefetched_news:
        # историю не подмешиваем: модель копирует шаблон прошлых ответов и выдумывает новости
        convo = [{"role": "system", "content": system}, msgs[-1]]
    else:
        convo = [{"role": "system", "content": system}] + msgs[-MAX_MSGS:]
    prev_asst = [m.get("content") or "" for m in msgs[:-1] if m.get("role") == "assistant"][-3:]
    retried = False
    for hop in range(5):
        # на последних хопах отбираем инструменты — модель обязана ответить текстом
        force_answer = (hop >= 3)
        url, model, hdrs = backend()
        payload = build_payload(url, model, convo, force_answer)
        try:
            r = http_json(url, payload, timeout=180, headers=hdrs)
        except Exception as e:
            if url != ZAI_URL:
                raise
            print("ZAI FAIL (%s) -> локальный fallback" % str(e)[:140], flush=True)
            url, model, hdrs = LLAMA, "local", None
            payload = build_payload(url, model, convo, force_answer)
            r = http_json(url, payload, timeout=900, headers=None)
        m = r["choices"][0]["message"]
        tc = m.get("tool_calls")
        if tc:
            print("TOOLCALL: %s" % [ (c.get("function") or {}).get("name") for c in tc ], flush=True)
            # не тащим reasoning обратно в промпт
            convo.append({"role": "assistant", "content": m.get("content") or "", "tool_calls": tc})
            for call in tc:
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except Exception:
                    args = {}
                if (call["function"].get("name") or "") == "get_news":
                    res = get_news(int(args.get("limit") or 6))
                elif (call["function"].get("name") or "") == "random_joke":
                    res = random_joke() or "Анекдотов не завезли."
                else:
                    res = web_search(args.get("query", ""))
                convo.append({"role": "tool", "tool_call_id": call["id"], "content": res[:2500]})
            continue
        text = (m.get("content") or "").strip()
        if not text:
            text = "…что-то я завис. Переспроси."
        if not retried:
            hits = crutch_hits(text, prev_asst)
            if hits:
                print("CRUTCH %s -> перегенерация" % hits[:3], flush=True)
                retried = True
                convo.append({"role": "assistant", "content": text})
                convo.append({"role": "user", "content":
                              "Стоп. Ты опять тянешь одно и то же: %s. Это уже было в твоих "
                              "прошлых ответах. Ответь на то же самое, но по-другому: другие "
                              "слова, другой заход, без этой темы." % ", ".join(hits[:3])})
                continue
        msgs.append({"role": "assistant", "content": text})
        hist[str(chat_id)] = msgs[-MAX_MSGS:]
        save(HIST, hist)
        return text
    return "…что-то я завис. Переспроси."


def main():
    off = int(open(OFF).read().strip()) if os.path.exists(OFF) else 0
    print("Петрович на связи, offset=%d" % off, flush=True)
    while True:
        try:
            r = tg("getUpdates", offset=off + 1, timeout=50)
            for u in r.get("result", []):
                off = max(off, u["update_id"])
                open(OFF, "w").write(str(off))
                m = u.get("message") or u.get("edited_message")
                if not m:
                    extra = u.get("my_chat_member") or u.get("chat_member") or {}
                    print("RAW upd %s kind=%s chat=%s/%s" % (
                        u["update_id"], sorted(k for k in u if k != "update_id"),
                        ((extra.get("chat") or {}).get("type")), ((extra.get("chat") or {}).get("id"))), flush=True)
                    continue
                text = m.get("text") or m.get("caption")
                if not text:
                    continue
                chat_id = m["chat"]["id"]
                ctype = (m.get("chat") or {}).get("type")
                uname = (m.get("from") or {}).get("username")
                # полный лог входящих — чтобы можно было разбирать примеры спама
                try:
                    with open(LOG, "a", encoding="utf-8") as lf:
                        lf.write("%s | %s/%s | %s | bot=%s | %s\n" % (
                            time.strftime("%Y-%m-%d %H:%M:%S"), ctype, chat_id, uname,
                            (m.get("from") or {}).get("is_bot"),
                            (text or "")[:2000].replace("\n", " ")))
                except Exception:
                    pass
                if (m.get("from") or {}).get("is_bot"):
                    # сообщение от другого бота: проверяем на рекламу крошечной моделью
                    spam = is_spam(text)
                    print("upd %s BOT chat=%s/%s spam=%s text=%r" % (
                        u["update_id"], ctype, chat_id, spam, text[:60]), flush=True)
                    if spam and ctype != "private":
                        now = time.time()
                        if now - _spam_last.get(chat_id, 0) > SPAM_COOLDOWN:
                            _spam_last[chat_id] = now
                            try:
                                tg("sendMessage", chat_id=chat_id, text=SPAM_REPLY,
                                   reply_to_message_id=m.get("message_id"))
                                print("  -> ответил на рекламу", flush=True)
                            except Exception as e:
                                print("  spam send err: %s" % e, flush=True)
                    continue
                decision = should_reply(m)
                print("upd %s chat=%s/%s from=%s text=%r reply=%s" % (
                    u["update_id"], ctype, chat_id, uname, text[:70], decision), flush=True)
                if not decision:
                    continue
                text = text.replace("@" + BOT_USERNAME, " ").strip()
                if text.startswith("/start") or not text:
                    text = "представься коротко, одним абзацем"
                try:
                    reply = ask(chat_id, text)
                except Exception as e:
                    reply = "…сломалось. " + str(e)[:120]
                for i in range(0, max(1, len(reply)), 3800):
                    try:
                        tg("sendMessage", chat_id=chat_id, text=reply[i:i + 3800])
                    except Exception as e:
                        print("send err: %s" % e, flush=True)
        except Exception as e:
            print("loop err: %s" % e, flush=True)
            time.sleep(5)


def check():
    """Самопроверка конфигурации и связности. Запуск: python3 bot.py --check"""
    def mask(v):
        return "(не задан)" if not v else (v[:6] + "…" + v[-4:] if len(v) > 12 else "задан")

    print("── конфигурация ─────────────────────────────")
    print("  TG_TOKEN          %s" % mask(TOKEN))
    print("  BRAIN             %s" % BRAIN)
    print("  ZAI_API_KEY       %s" % mask(ZAI_KEY))
    print("  ZAI_MODEL         %s" % ZAI_MODEL)
    print("  BOT_USERNAME      %s" % (BOT_USERNAME or "(пусто — в группе будет отвечать только по KEYWORDS)"))
    print("  KEYWORDS          %s" % ", ".join(KEYWORDS))
    print("  DATA_DIR          %s" % DATA)
    print("  PERSONA           %s (%d симв.)" % (PERSONA, len(open(PERSONA, encoding="utf-8").read()) if os.path.exists(PERSONA) else 0))
    print("  TZ_OFFSET         UTC+%s" % (TZ.utcoffset(None).total_seconds() / 3600))
    print("  MAX_MSGS          %d" % MAX_MSGS)

    ok = True

    def probe(name, url):
        nonlocal ok
        try:
            body = json.dumps({"model": "check", "messages": [{"role": "user", "content": "скажи ок"}],
                               "max_tokens": 4, "temperature": 0}).encode()
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                json.load(r)
            print("  %-17s OK" % name)
        except Exception as e:
            ok = False
            print("  %-17s НЕДОСТУПЕН (%s)" % (name, str(e)[:80]))

    print("── модели ───────────────────────────────────")
    if BRAIN == "zai" and ZAI_KEY:
        try:
            body = json.dumps({"model": ZAI_MODEL, "messages": [{"role": "user", "content": "ок"}],
                               "max_tokens": 4, "thinking": {"type": "disabled"}}).encode()
            req = urllib.request.Request(ZAI_URL, data=body, headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + ZAI_KEY})
            with urllib.request.urlopen(req, timeout=60) as r:
                json.load(r)
            print("  %-17s OK" % "облако Z.ai")
        except Exception as e:
            ok = False
            print("  %-17s НЕДОСТУПНО (%s)" % ("облако Z.ai", str(e)[:80]))
    else:
        print("  %-17s пропущено (BRAIN=%s)" % ("облако Z.ai", BRAIN))
    probe("локальная", LLAMA)
    probe("классификатор", CLASSIFIER)

    print("── telegram ─────────────────────────────────")
    try:
        me = http_json(API + "/getMe", None, timeout=20)
        print("  бот               @%s (%s)" % (me["result"].get("username"), me["result"].get("first_name")))
    except Exception as e:
        ok = False
        print("  бот               ОШИБКА (%s)" % str(e)[:100])

    print("─────────────────────────────────────────────")
    print("ИТОГ: %s" % ("всё на месте" if ok else "есть проблемы, см. выше"))
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(check() if "--check" in sys.argv else main())
