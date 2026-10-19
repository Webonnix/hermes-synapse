"""Russian numbers → words, for TTS engines that read digits badly.

Qwen3-TTS in x-vector mode reads "19 октября" as noise, so backend/tts.py spells
numbers out before sending Russian text. No dependency (num2words is not in the
image) and deliberately small: it covers what Vexa's replies contain — dates
(«19 октября», «24.10», «24.10.2026»), years («2026 года/году/год»), clock times
(«14:30»), ordinals with a suffix («3-го»), percent, °C, ₽/$/€, decimals with a
comma, space-grouped thousands and plain integers.

Agreement is approximate on purpose: ordinals in dates and years follow the
preposition in front (до/с/от… → genitive, к/по → dative, в/о → prepositional),
cardinals stay nominative, unit nouns take the 1/2–4/5+ plural form. A wrong case
still reads as Russian; a raw digit string does not.
"""

from __future__ import annotations

import re
from typing import List, Optional

_UNITS_M = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
_UNITS_F = ["ноль", "одна", "две"] + _UNITS_M[3:]
_TEENS = ["десять", "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
          "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
_TENS = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
_HUNDREDS = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]
# (one, few, many, feminine)
_SCALES = [
    (("тысяча", "тысячи", "тысяч"), True),
    (("миллион", "миллиона", "миллионов"), False),
    (("миллиард", "миллиарда", "миллиардов"), False),
]

_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
               "сентября", "октября", "ноября", "декабря"]

# Ordinal stems; "oy" = nominative masculine ends in -ой (второй), else -ый; "третий" is soft.
_ORD_UNITS = ["", "перв", "втор", "трет", "четвёрт", "пят", "шест", "седьм", "восьм", "девят"]
_ORD_TEENS = ["десят", "одиннадцат", "двенадцат", "тринадцат", "четырнадцат", "пятнадцат",
              "шестнадцат", "семнадцат", "восемнадцат", "девятнадцат"]
_ORD_TENS = ["", "", "двадцат", "тридцат", "сороков", "пятидесят", "шестидесят", "семидесят", "восьмидесят", "девяност"]
_ORD_HUNDREDS = ["", "сот", "двухсот", "трёхсот", "четырёхсот", "пятисот", "шестисот", "семисот", "восьмисот", "девятисот"]
_ORD_THOUSANDS = ["", "тысячн", "двухтысячн", "трёхтысячн", "четырёхтысячн", "пятитысячн", "шеститысячн",
                  "семитысячн", "восьмитысячн", "девятитысячн"]
_OY_STEMS = {"втор", "шест", "седьм", "восьм", "сороков"}

_HARD = {"m": "ый", "n": "ое", "f": "ая", "gen": "ого", "dat": "ому", "prep": "ом", "f_gen": "ой", "f_acc": "ую"}
_SOFT_THIRD = {"m": "третий", "n": "третье", "f": "третья", "gen": "третьего", "dat": "третьему",
               "prep": "третьем", "f_gen": "третьей", "f_acc": "третью"}

_GEN_PREPS = {"до", "с", "со", "от", "после", "около", "для", "из", "без", "кроме", "начиная", "вплоть"}
_DAT_PREPS = {"к", "ко", "по"}
_PREP_PREPS = {"в", "во", "о", "об", "при"}


def _plural(n: int, forms) -> str:
    n = abs(n)
    if 11 <= n % 100 <= 14:
        return forms[2]
    if n % 10 == 1:
        return forms[0]
    if 2 <= n % 10 <= 4:
        return forms[1]
    return forms[2]


def _triad(n: int, feminine: bool) -> List[str]:
    words: List[str] = []
    h, rest = divmod(n, 100)
    if h:
        words.append(_HUNDREDS[h])
    if 10 <= rest < 20:
        words.append(_TEENS[rest - 10])
    else:
        t, u = divmod(rest, 10)
        if t:
            words.append(_TENS[t])
        if u:
            words.append((_UNITS_F if feminine else _UNITS_M)[u])
    return words


def cardinal(n: int, feminine: bool = False) -> str:
    """Nominative cardinal: 12500 → «двенадцать тысяч пятьсот»."""
    if n < 0:
        return "минус " + cardinal(-n, feminine)
    if n == 0:
        return "ноль"
    if n >= 10 ** 12:
        return " ".join(_UNITS_M[int(d)] for d in str(n))
    words: List[str] = []
    groups = []
    while n:
        n, g = divmod(n, 1000)
        groups.append(g)
    for index in range(len(groups) - 1, -1, -1):
        g = groups[index]
        if not g:
            continue
        if index == 0:
            words += _triad(g, feminine)
        else:
            forms, fem = _SCALES[index - 1]
            if not (index == 1 and g == 1):
                words += _triad(g, fem)
            words.append(_plural(g, forms))
    return " ".join(words)


def _ord_word(stem: str, form: str) -> str:
    if stem == "трет":
        return _SOFT_THIRD[form]
    if form == "m" and stem in _OY_STEMS:
        return stem + "ой"
    return stem + _HARD[form]


def ordinal(n: int, form: str = "m") -> str:
    """Ordinal in `form` (m, n, f, gen, dat, prep, f_gen, f_acc): 2026, gen → «две тысячи двадцать шестого»."""
    if n <= 0 or n >= 10 ** 9:
        return cardinal(n)
    rest = n % 100
    if rest:
        prefix = cardinal(n - rest) if n - rest else ""
        if rest < 10:
            last = _ord_word(_ORD_UNITS[rest], form)
        elif rest < 20:
            last = _ord_word(_ORD_TEENS[rest - 10], form)
        else:
            t, u = divmod(rest, 10)
            last = _ord_word(_ORD_TENS[t], form) if not u else _TENS[t] + " " + _ord_word(_ORD_UNITS[u], form)
        return (prefix + " " + last).strip()
    h = (n % 1000) // 100
    if h:
        prefix = cardinal(n - h * 100) if n - h * 100 else ""
        return (prefix + " " + _ord_word(_ORD_HUNDREDS[h], form)).strip()
    thousands = n // 1000
    if thousands < 10 and n % 1000 == 0:
        return _ord_word(_ORD_THOUSANDS[thousands], form)
    return cardinal(n)


def _preceding_word(text: str, start: int) -> str:
    match = re.search(r"([A-Za-zА-Яа-яЁё]+)\W*$", text[:start])
    return match.group(1).lower() if match else ""


def _form_from_context(text: str, start: int, default: str = "n", dates: bool = True) -> str:
    match = re.search(r"([А-Яа-яЁё]+)\s*$", text[:start])
    word = match.group(1).lower() if match else ""
    if word in _GEN_PREPS:
        return "gen"
    if word in _DAT_PREPS and not (dates and word == "по"):
        return "dat"
    if word in _PREP_PREPS:
        return "prep"
    return default


_SUFFIX_FORMS = {
    "го": "gen", "ого": "gen", "его": "gen", "му": "dat", "ому": "dat", "ему": "dat", "м": "prep", "ом": "prep",
    "ем": "prep", "й": "m", "ый": "m", "ий": "m", "ой": "m", "е": "n", "ое": "n", "ее": "n", "я": "f", "ая": "f",
    "ья": "f", "ю": "f_acc", "ую": "f_acc",
}

_SPACE = "[ \u00a0\u202f]"
_VERSION_WORDS = {"версия", "версии", "версию", "версией", "v", "ver", "version", "python", "node", "релиз", "релиза", "сборка", "сборки"}
_MONTH_RE = "|".join(_MONTHS_GEN)


def _date(day: int, month: int, year: Optional[int], form: str) -> Optional[str]:
    if not (1 <= day <= 31 and 1 <= month <= 12):
        return None
    out = f"{ordinal(day, form)} {_MONTHS_GEN[month - 1]}"
    if year:
        out += f" {ordinal(year, 'gen')} года"
    return out


def normalize_ru_numbers(text: str) -> str:
    """Spell out the numbers in Russian `text`; everything else is left as is."""
    if not re.search(r"\d", text):
        return text

    def dotted(m: re.Match) -> str:
        return " точка ".join(cardinal(int(part)) for part in m.group(0).split("."))

    # IPs / versions («192.168.0.200», «3.12.1») part by part — but dd.mm.yyyy stays a date
    text = re.sub(r"(?<![\w.])\d+(?:\.\d+){2,}(?![\w.])",
                  lambda m: m.group(0) if re.fullmatch(r"\d{1,2}\.\d{2}\.\d{4}", m.group(0)) else dotted(m), text)

    # English-style thousands («$12,500», «1,000,000 рублей», «12,500 ₽») — a decimal comma
    # never has a currency in front or two three-digit groups behind it.
    def _ungroup(m: re.Match) -> str:
        return m.group(0).replace(",", " ")

    text = re.sub(r"(?<![\d,])\d{1,3}(?:,\d{3}){2,}(?![\d,])", _ungroup, text)
    text = re.sub(r"(?<=[$€])\s?\d{1,3}(?:,\d{3})+(?![\d,])", _ungroup, text)
    text = re.sub(r"(?<![\d,])\d{1,3}(?:,\d{3})+(?=\s?(?:₽|руб|долл|евро|€|\$))", _ungroup, text)

    def full_date(m: re.Match) -> str:
        before = _preceding_word(m.string, m.start())
        if before in _VERSION_WORDS:
            return m.group(0)
        # «3.11» without a year is a date only after a date preposition («с 3.11»)
        if len(m.group(1)) == 1 and not m.group(3) and before not in _GEN_PREPS | _DAT_PREPS | {"на"}:
            return m.group(0)
        spoken = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)) if m.group(3) else None,
                       _form_from_context(m.string, m.start()))
        return spoken if spoken else m.group(0)

    text = re.sub(r"\b(\d{1,2})\.(\d{2})(?:\.(\d{4}))?\b(?!\.\d|,\d)", full_date, text)

    def day_month(m: re.Match) -> str:
        day = int(m.group(1))
        if not 1 <= day <= 31:
            return m.group(0)
        return f"{ordinal(day, _form_from_context(m.string, m.start()))} {m.group(2)}"

    text = re.sub(rf"\b(\d{{1,2}}){_SPACE}+({_MONTH_RE})\b", day_month, text, flags=re.IGNORECASE)

    def year(m: re.Match) -> str:
        n = int(m.group(1))
        word = m.group(2).lower()
        if word in ("года", "г.", "г", "гг.", "гг"):
            return f"{ordinal(n, 'gen')} года"
        if word == "году":
            return f"{ordinal(n, 'prep')} году"
        return f"{ordinal(n, 'm')} год"

    text = re.sub(r"\b(\d{4})\s*(году|года|год|гг\.|г\.|гг\b|г\b)", year, text)

    def clock(m: re.Match) -> str:
        hours, minutes = int(m.group(1)), int(m.group(2))
        spoken_minutes = "ноль ноль" if minutes == 0 else ("ноль " + _UNITS_F[minutes] if minutes < 10 else cardinal(minutes, True))
        return f"{cardinal(hours)} {spoken_minutes}"

    text = re.sub(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", clock, text)

    def suffixed(m: re.Match) -> str:
        return ordinal(int(m.group(1)), _SUFFIX_FORMS[m.group(2).lower()])

    text = re.sub(r"\b(\d+)-(" + "|".join(sorted(_SUFFIX_FORMS, key=len, reverse=True)) + r")\b", suffixed, text)

    def _number_value(raw: str):
        raw = re.sub(_SPACE, "", raw)
        if "," in raw:
            whole, frac = raw.split(",", 1)
            return int(whole), frac
        return int(raw), None

    def with_unit(forms, suffix: str = ""):
        def repl(m: re.Match) -> str:
            sign = "минус " if m.group(1) else ""
            whole, frac = _number_value(m.group(2))
            if frac is not None:
                return f"{sign}{decimal(whole, frac)} {forms[1]}{suffix}"
            return f"{sign}{cardinal(whole)} {_plural(whole, forms)}{suffix}"
        return repl

    number_re = rf"(\d{{1,3}}(?:{_SPACE}\d{{3}})+(?:,\d+)?|\d+(?:,\d+)?)"

    text = re.sub(rf"(-|−)?{number_re}\s*%", with_unit(("процент", "процента", "процентов")), text)
    text = re.sub(rf"(-|−)?{number_re}\s*°\s*[CС]\b", with_unit(("градус", "градуса", "градусов"), " Цельсия"), text)
    text = re.sub(rf"(-|−)?{number_re}\s*°", with_unit(("градус", "градуса", "градусов")), text)
    text = re.sub(rf"(-|−)?{number_re}\s*(?:₽|руб\.)", with_unit(("рубль", "рубля", "рублей")), text)

    def prefixed_currency(forms):
        def repl(m: re.Match) -> str:
            whole, frac = _number_value(m.group(1))
            return f"{decimal(whole, frac) if frac is not None else cardinal(whole)} {_plural(whole, forms) if frac is None else forms[1]}"
        return repl

    text = re.sub(rf"\$\s?{number_re}", prefixed_currency(("доллар", "доллара", "долларов")), text)
    text = re.sub(rf"€\s?{number_re}", prefixed_currency(("евро", "евро", "евро")), text)
    text = re.sub(rf"{number_re}\s?€", prefixed_currency(("евро", "евро", "евро")), text)
    text = re.sub(r"№\s*", "номер ", text)

    # what is left of «3.11» (a version, not a date) → «три точка одиннадцать»
    text = re.sub(r"(?<![\w.])(\d+)\.(\d+)(?![\w.])",
                  lambda m: f"{cardinal(int(m.group(1)))} точка {cardinal(int(m.group(2)))}", text)

    def plain(m: re.Match) -> str:
        whole, frac = _number_value(m.group(1))
        return decimal(whole, frac) if frac is not None else cardinal(whole)

    text = re.sub(rf"(?<![\w.]){number_re}(?![\w])", plain, text)
    return text


_FRACTION_NAMES = {1: ("десятая", "десятых"), 2: ("сотая", "сотых"), 3: ("тысячная", "тысячных")}


def decimal(whole: int, frac: str) -> str:
    """3,5 → «три целых пять десятых»; more than three decimals are read digit by digit."""
    frac = frac.rstrip("0") or "0"
    if frac == "0":
        return cardinal(whole)
    whole_word = "целая" if whole % 10 == 1 and whole % 100 != 11 else "целых"
    if len(frac) > 3:
        return f"{cardinal(whole, True)} {whole_word} " + " ".join(_UNITS_M[int(d)] for d in frac)
    value = int(frac)
    one, many = _FRACTION_NAMES[len(frac)]
    frac_word = one if value % 10 == 1 and value % 100 != 11 else many
    return f"{cardinal(whole, True)} {whole_word} {cardinal(value, True)} {frac_word}"
