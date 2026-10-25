from backend.speech_abbrev_ru import expand_ru_abbreviations, spell


def test_known_abbreviations_are_spelled_letter_by_letter():
    assert expand_ru_abbreviations("ООО «Ромашка», ИНН и ИП Петрова") == "о-о-о «Ромашка», и-эн-эн и и-пэ Петрова"


def test_vowelless_caps_are_spelled_even_when_not_listed():
    assert expand_ru_abbreviations("через СБП, КПП и НДС") == "через эс-бэ-пэ, ка-пэ-пэ и эн-дэ-эс"


def test_abbreviations_said_as_words_are_left_alone():
    assert expand_ru_abbreviations("ГОСТ, ВУЗ и МИД") == "ГОСТ, ВУЗ и МИД"
    assert expand_ru_abbreviations("полис ОСАГО") == "полис осаго"


def test_ordinary_words_and_latin_are_untouched():
    text = "Привет, Vexa! Счёт в CRM готов, а API работает."
    assert expand_ru_abbreviations(text) == text


def test_spell_uses_russian_letter_names():
    assert spell("НДФЛ") == "эн-дэ-эф-эль"
