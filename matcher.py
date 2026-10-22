import difflib
import re
from dataclasses import dataclass

WAKE_WORD = "люся"
WAKE_RATIO = 0.66
WINDOW = 4

FILLERS = {
    "пожалуйста", "плиз", "ну", "ка", "давай", "будь", "добра", "можешь",
    "сможешь", "слушай", "эй", "мне", "а", "и",
}

YES_WORDS = {"да", "подтверждаю", "давай", "точно", "ага", "угу", "валяй", "конечно"}


@dataclass(frozen=True)
class Command:
    name: str
    triggers: tuple
    param: str = "none"
    ask: str = ""
    confirm: str = ""


COMMANDS = (
    Command("radio", ("включи музыку", "запусти музыку", "музыка")),
    Command("ctime", ("который час", "сколько времени", "время", "текущее время")),
    Command("now_playing", ("что играет", "что за песня", "что за трек", "что за музыка",
                            "какой трек", "название трека")),
    Command("next", ("следующий", "следующий трек", "дальше", "переключи")),
    Command("prev", ("предыдущий", "назад", "верни")),
    Command("plpa", ("пауза", "стоп", "хватит", "останови", "играй", "продолжи",
                     "выключи музыку")),
    Command("mute", ("выключи звук", "включи звук", "замуть", "размуть", "мут",
                     "без звука")),
    Command("desk", ("рабочий стол", "сверни", "разверни")),
    Command("search", ("найди", "гугли", "ищи", "поищи", "поиск", "в гугле"),
            param="text", ask="Что искать?"),
    Command("timer", ("таймер", "засеки"), param="duration", ask="На сколько?"),
    Command("screenshot", ("скриншот", "сделай скриншот", "сфоткай экран", "заскринь",
                           "снимок экрана")),
    Command("add_note", ("запиши", "напиши", "запомни", "добавь в заметки", "заметка",
                         "напомни", "сделай заметку"),
            param="text", ask="Что записать?"),
    Command("read_notes", ("прочитай заметки", "что в заметках", "список дел",
                           "мои записи", "покажи заметки")),
    Command("clear_notes", ("очисти заметки", "удали заметки", "удали записи",
                            "очисти записи")),
    Command("save_clipboard", ("сохрани буфер", "запомни ссылку", "сохрани копию",
                               "из буфера")),
    Command("mon_off", ("выключи монитор", "выключи мониторы", "погаси экран",
                        "отключи экран", "выключи экран")),
    Command("empty_trash", ("очисти корзину", "почисти корзину", "очистка корзины",
                            "удали мусор"), confirm="Точно очистить корзину?"),
    Command("off", ("выключи компьютер", "выключи пк", "выруби компьютер", "выруби пк",
                    "заверши работу"), confirm="Точно выключить компьютер?"),
    Command("cancel_off", ("отмени выключение", "отмена выключения", "не выключай")),
    Command("send_to_phone", ("скинь на телефон", "отправь на телефон", "кинь на телефон",
                              "перекинь на телефон", "скинь буфер")),
    Command("youtube", ("ютуб", "ютьюб", "youtube")),
    Command("discord", ("дискорд", "discord")),
    Command("game_on", ("игровой режим", "режим игры")),
    Command("game_off", ("выключи игровой режим", "отключи игровой режим",
                         "выйди из игрового режима", "обычный режим")),
)


@dataclass
class Match:
    cmd: Command
    param: str
    score: float


def tokenize(text):
    return re.findall(r"\w+", text.lower().replace("ё", "е"))


PREFIXES = ("за", "по", "на", "про", "при")


def stem(w):
    if len(w) >= 6:
        for pre in PREFIXES:
            if w.startswith(pre) and len(w) - len(pre) >= 4:
                w = w[len(pre):]
                break
    return w if len(w) <= 4 else w[:max(4, len(w) - 2)]


def same(a, b):
    a, b = stem(a), stem(b)
    if len(a) <= 3 or len(b) <= 3:
        return a == b
    m = min(len(a), len(b))
    return a[:m] == b[:m]


def _find(window, word, used):
    for i, tok in enumerate(window):
        if i not in used and same(tok, word):
            return i
    return None


def match(text):
    all_tokens = tokenize(text)
    kept = [(i, t) for i, t in enumerate(all_tokens) if t not in FILLERS]
    tokens = [t for _, t in kept]
    window = tokens[:WINDOW]

    def param_from(k):
        orig = kept[k][0] if k < len(kept) else len(all_tokens)
        return " ".join(all_tokens[orig:])

    best = None
    for order, cmd in enumerate(COMMANDS):
        for trig in cmd.triggers:
            keep = trig.startswith("=")
            words = tokenize(trig)
            positions = []
            for w in words:
                idx = _find(window, w, positions)
                if idx is None:
                    break
                positions.append(idx)
            else:
                score = sum(len(w) for w in words)
                key = (score, -min(positions), -order)
                if best is None or key > best[0]:
                    start = min(positions) if keep else max(positions) + 1
                    best = (key, Match(cmd, param_from(start), score))
    if best:
        return best[1]
    return _fuzzy(tokens, window, param_from)


def _fuzzy(tokens, window, param_from):
    best = None
    for cmd in COMMANDS:
        for trig in cmd.triggers:
            words = tokenize(trig)
            if trig.startswith("=") or len(words) != 1 or len(words[0]) < 5:
                continue
            for i, tok in enumerate(window):
                if len(tok) < 5:
                    continue
                r = difflib.SequenceMatcher(None, tok, words[0]).ratio()
                if r >= 0.84 and (best is None or r > best[0]):
                    best = (r, i, cmd)
    if best:
        r, i, cmd = best
        return Match(cmd, param_from(i + 1), r)
    return None


def _wake_forms():
    forms = {WAKE_WORD}
    if WAKE_WORD.endswith("и"):
        forms.add(WAKE_WORD[:-1] + "я")
    elif WAKE_WORD.endswith("я"):
        forms.add(WAKE_WORD[:-1] + "и")
    return forms


def _is_wake(tok):
    if len(tok) > len(WAKE_WORD) + 3:
        return False
    return any(difflib.SequenceMatcher(None, tok, f).ratio() >= WAKE_RATIO for f in _wake_forms())


def has_wake(text):
    return any(_is_wake(t) for t in tokenize(text)[:2])


def strip_wake(text):
    tokens = tokenize(text)
    for i, t in enumerate(tokens[:2]):
        if _is_wake(t):
            return " ".join(tokens[i + 1:])
    return " ".join(tokens)


def apply_corrections(text, corrections):
    for wrong, right in corrections.items():
        text = re.sub(rf"(?<!\w){re.escape(wrong)}(?!\w)", right, text, flags=re.IGNORECASE)
    return text


def strip_wake_text(text):
    for m in list(re.finditer(r"\w+", text))[:2]:
        if _is_wake(m.group(0).lower().replace("ё", "е")):
            return text[m.end():].lstrip(" ,.:;!?-—").strip()
    return text.strip()


def is_yes(text):
    toks = tokenize(text)
    return any(t in YES_WORDS or t.startswith("подтвер") for t in toks)


_UNITS = {
    "ноль": 0, "один": 1, "одну": 1, "одна": 1, "два": 2, "две": 2, "три": 3,
    "четыре": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9,
    "десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
    "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17,
    "восемнадцать": 18, "девятнадцать": 19, "двадцать": 20, "тридцать": 30,
    "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
    "восемьдесят": 80, "девяносто": 90, "сто": 100,
}


def parse_duration(text):
    total, cur, used_unit = 0.0, None, False
    for tok in tokenize(text):
        if tok.isdigit():
            cur = (cur or 0) + int(tok)
        elif tok in _UNITS:
            cur = (cur or 0) + _UNITS[tok]
        elif tok in ("полтора", "полторы"):
            cur = 1.5
        elif tok.startswith("полчас"):
            total += 1800
            used_unit = True
        elif tok.startswith("полмин"):
            total += 30
            used_unit = True
        else:
            mult = 3600 if tok.startswith("час") else 60 if tok.startswith("мин") \
                else 1 if tok.startswith("сек") else 0
            if mult:
                total += (cur if cur is not None else 1) * mult
                cur, used_unit = None, True
    if cur and not used_unit:
        total += cur * 60
    return int(total)
