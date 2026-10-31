"""Personal data detection (privacy stage 1): who and where, not just secrets.

Deterministic and local, before anything leaves for an external provider:

* e-mail addresses, phone numbers, Russian passport numbers (labelled), SNILS
  and personal (12-digit) INN by checksum, and street addresses (ru/en shapes);
* person names in Russian text with Natasha's NER (CPU, a few milliseconds;
  models ship inside the wheel, nothing is downloaded at runtime). Only names
  of two or more words are masked — "Анна Ковалёва", "Сидоров Пётр Ильич" — so
  a sentence-initial verb or a bare public surname keeps its meaning for the
  external model.

Each find becomes a typed placeholder ([PERSON_…], [EMAIL_…], …) so the
provider still knows what stood there, and is restored in its answer.
Names in other languages come from the local-model pass (local_llm.py).
"""

from __future__ import annotations

import re
import threading
from typing import Callable, List, Optional, Tuple

from backend.privacy.vault import PLACEHOLDER_RE, PlaceholderVault

Guard = Optional[Callable[[str], bool]]


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _phone_is_noise(value: str) -> bool:
    digits = _digits(value)
    if not 10 <= len(digits) <= 15:
        return True
    # A bare run of digits is an order or tracking number unless it is a
    # Russian mobile/landline shape; real numbers are written with + or separators.
    bare = value.strip().isdigit()
    return bare and not (len(digits) == 11 and digits[0] in "78")


def _snils_invalid(value: str) -> bool:
    digits = _digits(value)
    if len(digits) != 11:
        return True
    total = sum(int(d) * (9 - i) for i, d in enumerate(digits[:9]))
    check = total % 101 % 100
    return check != int(digits[9:])


def _inn12_invalid(value: str) -> bool:
    d = [int(x) for x in _digits(value)]
    if len(d) != 12:
        return True
    c1 = sum(w * x for w, x in zip((7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d)) % 11 % 10
    c2 = sum(w * x for w, x in zip((3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8), d)) % 11 % 10
    return (c1, c2) != (d[10], d[11])


_STREET_RU = (r"(?:ул\.?|улиц[аеуы]|просп\.?|проспект\w*|пр-т|пер\.?|переул\w*|ш\.|шоссе|бульв\w*|б-р|"
              r"наб\.?|набережн\w*|пл\.?|площад\w*|мкр\.?|микрорайон\w*)")

# (kind, pattern, guard) — guard returns True to keep the match as plain text.
PATTERNS: List[Tuple[str, "re.Pattern[str]", Guard]] = [
    ("EMAIL", re.compile(r"(?<![\w.+-])[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-zА-Яа-я]{2,}"), None),
    # Group 1 is the value; the label around it stays readable.
    ("ID", re.compile(r"(?i)паспорт[^\d\n]{0,25}(\d{2}\s?\d{2}\s?№?\s?\d{6})\b"), None),
    ("ID", re.compile(r"\b\d{3}-\d{3}-\d{3}[ -]\d{2}\b"), _snils_invalid),
    ("ID", re.compile(r"(?<!\d)\d{12}(?!\d)"), _inn12_invalid),
    ("ADDRESS", re.compile(_STREET_RU + r"\s+[А-ЯЁA-Z0-9][\w\-]*(?:\s+[А-ЯЁA-Z][\w\-]*){0,2},?\s*"
                           r"(?:д\.?|дом)\s*\d+\s*[а-яА-Я]?(?:[,\s]*(?:к\.?|корп\.?|стр\.?)\s*\d+)?"
                           r"(?:[,\s]*(?:кв\.?|квартира|оф\.?|офис)\s*\d+)?"), None),
    ("ADDRESS", re.compile(r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s+){1,3}(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|"
                           r"Boulevard|Blvd|Drive|Dr|Court|Ct|Place|Pl|Way)\b\.?"), None),
    ("PHONE", re.compile(r"(?<![\w+])(?:\+\d{1,3}[\s\-.]?)?(?:\(\d{2,5}\)|\d{2,5})(?:[\s\-.]?\d{2,4}){2,4}(?!\d)"),
     _phone_is_noise),
]


def _strip_placeholder_overlap(span: str) -> bool:
    return bool(PLACEHOLDER_RE.search(span)) or "[" in span or "]" in span


class _NameTagger:
    """Natasha's news NER, loaded once per process on first use (≈2 s, ~60 MB)."""

    _lock = threading.Lock()
    _ready = None

    @classmethod
    def get(cls):
        if cls._ready is None:
            with cls._lock:
                if cls._ready is None:
                    from natasha import NewsEmbedding, NewsNERTagger, Segmenter
                    cls._ready = (Segmenter(), NewsNERTagger(NewsEmbedding()))
        return cls._ready


def person_names(text: str) -> List[str]:
    """Multi-word person names Natasha finds in `text` (Cyrillic only)."""
    if not re.search(r"[А-Яа-яЁё]", text):
        return []
    from natasha import Doc

    segmenter, tagger = _NameTagger.get()
    doc = Doc(text)
    doc.segment(segmenter)
    doc.tag_ner(tagger)
    names = []
    for span in doc.spans:
        value = span.text.strip(" ,.;:«»\"'()")
        if span.type == "PER" and len(value.split()) >= 2 and not _strip_placeholder_overlap(value):
            names.append(value)
    return names


def pii_redact(text: str, vault: PlaceholderVault) -> str:
    """Replace personal data in `text` with typed vault placeholders."""
    result = text
    for kind, pattern, guard in PATTERNS:
        def _replace(match: "re.Match[str]", kind=kind, guard=guard) -> str:
            whole = match.group(0)
            value = (match.group(1) if match.lastindex else whole).strip()
            if guard is not None and guard(value):
                return whole
            return whole.replace(value, vault.placeholder_for(value, kind), 1)

        parts = PLACEHOLDER_RE.split(result)
        tokens = PLACEHOLDER_RE.findall(result)
        rebuilt = [pattern.sub(_replace, parts[0])]
        for token, part in zip(tokens, parts[1:]):
            rebuilt += [token, pattern.sub(_replace, part)]
        result = "".join(rebuilt)
    for name in sorted(set(person_names(result)), key=len, reverse=True):
        placeholder = vault.placeholder_for(name, "PERSON")
        parts = PLACEHOLDER_RE.split(result)
        tokens = PLACEHOLDER_RE.findall(result)
        rebuilt = [parts[0].replace(name, placeholder)]
        for token, part in zip(tokens, parts[1:]):
            rebuilt += [token, part.replace(name, placeholder)]
        result = "".join(rebuilt)
    return result


def classify_span(span: str) -> str:
    """Kind for a span the local model returned: names and addresses keep their type."""
    if re.fullmatch(r"[A-ZА-ЯЁ][\w'’\-]+(?:\s+[A-ZА-ЯЁ][\w'’\-.]*){1,3}", span) and not re.search(r"\d", span):
        return "PERSON"
    if re.search(r"\d", span) and re.search(r"(?i)\b(street|st|ave|road|rd|lane|ул|улиц|просп|пер|д\.|дом|кв)\b", span):
        return "ADDRESS"
    return "SECRET"
