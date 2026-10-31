"""Privacy stage 1: personal data becomes typed placeholders; ordinary text stays."""
import pytest

from backend.privacy.detectors.pii import classify_span, pii_redact
from backend.privacy.vault import PLACEHOLDER_RE, PlaceholderVault


def redact(text):
    vault = PlaceholderVault()
    return pii_redact(text, vault), vault


@pytest.mark.parametrize("text,value,kind", [
    ("Позвони Анне Сергеевне Ковалёвой завтра", "Анне Сергеевне Ковалёвой", "PERSON"),
    ("номер +7 916 123-45-67, звони", "+7 916 123-45-67", "PHONE"),
    ("Call me at (212) 555-0147.", "(212) 555-0147", "PHONE"),
    ("договор на ivan.petrov@zenith-trade.ru до пятницы", "ivan.petrov@zenith-trade.ru", "EMAIL"),
    ("Паспорт клиента: 4510 123456, выдан ОВД", "4510 123456", "ID"),
    ("СНИЛС 112-233-445 95", "112-233-445 95", "ID"),
    ("ИНН физлица 500100732259", "500100732259", "ID"),
    ("офис: ул. Тверская, д. 12, кв. 5, Москва", "ул. Тверская, д. 12, кв. 5", "ADDRESS"),
    ("lives at 14 Baker Street, London", "14 Baker Street", "ADDRESS"),
])
def test_personal_data_becomes_a_typed_placeholder(text, value, kind):
    redacted, vault = redact(text)
    assert value not in redacted
    [placeholder] = PLACEHOLDER_RE.findall(redacted)
    assert placeholder.startswith(f"[{kind}_") and vault.value_for(placeholder) == value


@pytest.mark.parametrize("text", [
    "Перенеси созвон с командой маркетинга на 15:00 четверга 31.10.2026.",
    "Заказ №1234567890 на сумму 15 000 руб., трек 80080012345678.",
    "Версия 1.26.0, сервер 192.168.0.200:9443, коммит 8b35e83.",
    "Путин и Трамп провели переговоры, сообщает ТАСС.",
    "Курс доллара 92.45, ETH 3 450,12 $, температура -5.",
    "ИНН ООО «Ромашка» 7701234567 — это реквизит компании.",
])
def test_ordinary_text_is_left_alone(text):
    assert redact(text)[0] == text


def test_a_number_failing_the_snils_checksum_is_not_labelled_an_id():
    # It may still be masked as a phone: an 11-digit grouped number errs private.
    assert "[ID_" not in redact("СНИЛС 112-233-445 96 с неверной контрольной суммой")[0]


def test_existing_placeholders_are_not_re_redacted():
    text = "клиент [PERSON_9f8e7d6c] ждёт, почта [EMAIL_1a2b3c4d]"
    assert redact(text)[0] == text


def test_the_same_person_keeps_one_placeholder():
    redacted, _ = redact("Сидоров Пётр Ильич подписал. Позвони Сидоров Пётр Ильич завтра.")
    assert len(set(PLACEHOLDER_RE.findall(redacted))) == 1


def test_local_model_spans_keep_their_kind():
    assert classify_span("Sarah Goldberg") == "PERSON"
    assert classify_span("42 Herzl Street") == "ADDRESS"
    assert classify_span("синяя лампа 77") == "SECRET"
