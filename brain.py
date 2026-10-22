import html as htmllib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_PERSONA = """Ты - {name}, голосовой помощник на компьютере хозяина.
Как отвечать: по-русски, на «ты», простыми словами. По умолчанию одно-два коротких предложения. Если просят рассказать, пересказать или объяснить подробно, отвечай развёрнуто, сколько посчитаешь нужным, и заканчивай, когда всё сказано. Не повторяй вопрос и не начинай с «Конечно» или «Разумеется».
Не выдумывай факты и новости: если не знаешь или не уверен, так и скажи. Если к реплике приложены результаты поиска в интернете, опирайся на них и пересказывай своими словами.
Ты умеешь управлять компьютером голосовыми командами (музыка, таймер, заметки, игровой режим и другое), но сейчас просто общайся."""

SEARCH_DECIDER = (
    "Ты решаешь, нужен ли интернет, чтобы ответить на последнюю реплику пользователя. "
    "Интернет НУЖЕН для: любых вопросов про живых людей (возраст, должность, новости), "
    "дат, цен, курсов, погоды, спорта, событий, компаний, игр, фильмов, технологий, "
    "а также если нет полной уверенности. "
    "Если спрашивают возраст человека, ищи дату рождения: запрос вида «Имя Фамилия дата рождения». "
    "Интернет НЕ нужен для: болтовни, мнений, шуток, подколок, оскорблений, личных замечаний, "
    "вопросов о тебе самой, а также вопросов про текущее время и дату (они известны без поиска). "
    "Ответь строго одной строкой: НЕТ или ПОИСК: <короткий запрос на русском>."
)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _today():
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря"]
    t = time.localtime()
    return f"{t.tm_mday} {months[t.tm_mon - 1]} {t.tm_year} года, {t.tm_hour:02d}:{t.tm_min:02d}"


def _visible(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    i = text.find("<think>")
    if i != -1:
        text = text[:i]
    m = re.search(r"<(t(h(i(n(k)?)?)?)?)?$", text)
    if m:
        text = text[:m.start()]
    return text.lstrip()


def _no_ellipsis(text):
    return re.sub(r"\s*(?:\.{2,}|…)\s*", ", ", text)


def _strip_tags(s):
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def _html_search(query, n):
    data = urllib.parse.urlencode({"q": query, "kl": "ru-ru"}).encode("utf-8")
    req = urllib.request.Request("https://html.duckduckgo.com/html/", data=data,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as r:
        page = r.read().decode("utf-8", "ignore")
    titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', page, re.S)
    snips = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    rows = []
    for i, t in enumerate(titles[:n]):
        rows.append((_strip_tags(t), _strip_tags(snips[i]) if i < len(snips) else ""))
    return rows


def _web_search(query, n):
    DDGS = None
    try:
        from ddgs import DDGS
    except ImportError:
        try:
            from duckduckgo_search import DDGS
        except ImportError:
            DDGS = None
    if DDGS is not None:
        try:
            try:
                d = DDGS(timeout=8)
            except TypeError:
                d = DDGS()
            rows = list(d.text(query, region="ru-ru", max_results=n))
            out = [(r.get("title", ""), r.get("body", "")) for r in rows]
            if out:
                return out
        except Exception:
            pass
    return _html_search(query, n)


class Brain:
    def __init__(self, cfg, base_dir):
        self.cfg = cfg
        self.history = []
        self.last_turn = 0.0
        self.lock = threading.Lock()
        self.persona = self._load_persona(base_dir)

    def _load_persona(self, base_dir):
        path = os.path.join(base_dir, self.cfg.chat_persona_file)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(DEFAULT_PERSONA)
        with open(path, encoding="utf-8") as f:
            text = f.read().strip() or DEFAULT_PERSONA
        return text.replace("{name}", self.cfg.wake_word.capitalize())

    def _request(self, path, payload=None, timeout=5):
        url = self.cfg.chat_host.rstrip("/") + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=timeout)

    def server_up(self):
        try:
            self._request("/api/tags", timeout=1.5).close()
            return True
        except Exception:
            return False

    def ensure_server(self):
        if self.server_up():
            return True
        if not self.cfg.chat_autostart:
            return False
        try:
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, creationflags=0x08000000)
        except Exception:
            return False
        for _ in range(30):
            time.sleep(0.5)
            if self.server_up():
                return True
        return False

    def explain(self, e):
        if isinstance(e, urllib.error.HTTPError):
            try:
                detail = json.loads(e.read().decode("utf-8")).get("error", "")
            except Exception:
                detail = ""
            if e.code == 404 or "not found" in detail:
                return f"модель не найдена. Выполните: ollama pull {self.cfg.chat_model}"
            return f"Ollama вернула ошибку {e.code}: {detail}"
        if isinstance(e, urllib.error.URLError):
            return "Ollama не запущена (установите её с ollama.com и запустите)"
        return str(e)

    def warmup(self):
        try:
            if self.ensure_server():
                self._request("/api/generate", {"model": self.cfg.chat_model,
                                                "keep_alive": self.cfg.chat_keep_alive},
                              timeout=120).close()
        except Exception:
            pass

    def unload(self):
        try:
            if self.server_up():
                self._request("/api/generate", {"model": self.cfg.chat_model, "keep_alive": 0},
                              timeout=10).close()
        except Exception:
            pass

    def _search_query(self, user_text):
        cfg = self.cfg
        messages = ([{"role": "system", "content": SEARCH_DECIDER}]
                    + self.history[-2:]
                    + [{"role": "user", "content": user_text}])
        payload = {"model": cfg.chat_model, "messages": messages, "stream": False,
                   "think": False, "keep_alive": cfg.chat_keep_alive,
                   "options": {"temperature": 0, "num_predict": 40}}
        with self._request("/api/chat", payload, timeout=60) as r:
            answer = json.loads(r.read().decode("utf-8")).get("message", {}).get("content", "")
        answer = _visible(answer).strip()
        if not answer.upper().startswith("ПОИСК"):
            return None
        query = answer.split(":", 1)[1].strip().strip("\"'«»") if ":" in answer else ""
        return query or user_text

    def _search_context(self, user_text):
        cfg = self.cfg
        if not getattr(cfg, "chat_search", 1):
            return ""
        if len(user_text.split()) < 3:
            return ""
        try:
            query = self._search_query(user_text)
            if not query:
                return ""
            print(f"» [Поиск] {query}")
            rows = _web_search(query, int(getattr(cfg, "chat_search_results", 4)))
        except Exception as e:
            print(f"» [Поиск] не удалось: {e}")
            return ""
        lines = [f"- {t}: {s[:300]}" for t, s in rows if t or s]
        return "\n".join(lines)

    def reset(self):
        self.history.clear()

    def reply_stream(self, user_text):
        cfg = self.cfg
        with self.lock:
            if time.time() - self.last_turn > cfg.chat_memory_minutes * 60:
                self.history.clear()
            found = self._search_context(user_text)
            system = (self.persona + f"\n\nСейчас {_today()} по часам компьютера. "
                      "Называй дату и время, только если спросили напрямую.")
            content = user_text
            if found:
                content += ("\n\n[Результаты поиска в интернете. Опирайся только на них. "
                            "Возраст считай от даты рождения и сегодняшней даты. "
                            "Если точного ответа в них нет, скажи «не нашла», не угадывай. "
                            "Если результаты не по теме, игнорируй их и ответь как обычно, "
                            "не упоминая поиск.]\n" + found)
            messages = ([{"role": "system", "content": system}]
                        + self.history[-2 * cfg.chat_memory_turns:]
                        + [{"role": "user", "content": content}])
            payload = {"model": cfg.chat_model, "messages": messages, "stream": True,
                       "think": False, "keep_alive": cfg.chat_keep_alive,
                       "options": {"temperature": cfg.chat_temperature,
                                   "num_predict": cfg.chat_max_tokens,
                                   "repeat_penalty": float(getattr(cfg, "chat_repeat_penalty", 1.15))}}
            resp = self._request("/api/chat", payload, timeout=180)
            raw, pos, said, spoken = "", 0, 0, []
            try:
                for line in resp:
                    if not line.strip():
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise RuntimeError(chunk["error"])
                    raw += chunk.get("message", {}).get("content", "")
                    visible = _no_ellipsis(_visible(raw))
                    while said < cfg.chat_max_sentences:
                        m = re.search(r"[.!?]+\s", visible[pos:])
                        if not m:
                            break
                        end = pos + m.end()
                        sentence = visible[pos:end].strip()
                        pos = end
                        if sentence:
                            said += 1
                            spoken.append(sentence)
                            yield sentence
                    if said >= cfg.chat_max_sentences or chunk.get("done"):
                        break
                tail = _no_ellipsis(_visible(raw))[pos:].strip().rstrip(",")
                if tail and said < cfg.chat_max_sentences:
                    spoken.append(tail)
                    yield tail
            finally:
                resp.close()
                if spoken:
                    self.history += [{"role": "user", "content": user_text},
                                     {"role": "assistant", "content": " ".join(spoken)}]
                    self.last_turn = time.time()
