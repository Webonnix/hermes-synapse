"""Russian abbreviations → letter names, for TTS engines that read them badly.

Piper reads «ООО» as «а у» and «ИНН» as «Энен», so backend/tts.py spells such
tokens out before sending Russian text. Two rules, both deliberately narrow:

* a short list of abbreviations Vexa actually says (legal forms, tax and payment
  terms, a few everyday ones), spelled letter by letter;
* any other all-caps Cyrillic token of 2–5 letters with no vowel («СБП», «КПП»),
  which cannot be read as a word anyway.

Abbreviations that are said as words («ГОСТ», «ВУЗ», «МИД») have vowels and are
not in the list, so they are left for the engine to read.
"""

from __future__ import annotations

import re

_LETTERS = {
    "А": "а", "Б": "бэ", "В": "вэ", "Г": "гэ", "Д": "дэ", "Е": "е", "Ё": "ё", "Ж": "жэ", "З": "зэ",
    "И": "и", "Й": "и краткое", "К": "ка", "Л": "эль", "М": "эм", "Н": "эн", "О": "о", "П": "пэ",
    "Р": "эр", "С": "эс", "Т": "тэ", "У": "у", "Ф": "эф", "Х": "ха", "Ц": "цэ", "Ч": "че", "Ш": "ша",
    "Щ": "ща", "Ы": "ы", "Э": "э", "Ю": "ю", "Я": "я",
}
_VOWELS = set("АЕЁИОУЫЭЮЯ")

# Spelled out even though they contain vowels.
_SPELLED = {
    "ООО", "ОАО", "ЗАО", "ПАО", "АО", "ИП", "ИНН", "ОГРН", "ОГРНИП", "ЕГРЮЛ", "ЕГРИП", "НДФЛ", "УСН",
    "ЕНС", "ЭДО", "ЭЦП", "ОКВЭД", "ЮЛ", "ФИО", "РФ", "ЕС", "ОАЭ", "ЖК", "ЖКХ", "ТЦ", "ТО", "ИИ",
    "СМС", "МФЦ", "ПФР", "ДМС",
}
# Said as words although they look like abbreviations.
_AS_WORD = {"ОСАГО"}

_TOKEN = re.compile(r"(?<![А-ЯЁа-яё])([А-ЯЁ]{2,6})(?![А-ЯЁа-яё])")


def spell(token: str) -> str:
    return "-".join(_LETTERS[ch] for ch in token)


def _replace(match: re.Match) -> str:
    token = match.group(1)
    if token in _AS_WORD:
        return token.lower()
    if token in _SPELLED or (len(token) <= 5 and not (set(token) & _VOWELS)):
        return spell(token)
    return token


def expand_ru_abbreviations(text: str) -> str:
    return _TOKEN.sub(_replace, text)
