"""Turning raw Japanese sentences into tokens with readings.

fugashi (MeCab + UniDic) splits a sentence into words and gives each one its
reading and dictionary form; cutlet turns the sentence into romaji. Both only
run when building the corpus, never per request.
"""
import json
import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

from django.conf import settings


KANJI = "[㐀-䶿一-鿿豈-﫿々〆ヶ]"
KANJI_RE = re.compile(KANJI)
RUNS_RE = re.compile(f"{KANJI}+|(?:(?!{KANJI}).)+")
TRAILING_PUNCTUATION = "。.!?！？"

# UniDic reads some very common words formally (私 as わたくし); use the everyday reading instead.
READING_OVERRIDES = {"私": "わたし"}


@dataclass
class Token:
    surface: str            # the text exactly as it appears in the sentence
    reading: str            # hiragana reading of the surface ("" for kana and symbols)
    word: str | None        # dictionary form to learn, e.g. 食べる for 食べ; None if not learnable
    word_reading: str = ""  # hiragana reading of the dictionary form


def has_kanji(text):
    return bool(KANJI_RE.search(text))


def to_hiragana(text):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


def normalize(text):
    """What counts as 'the same text' when checking what the user typed."""
    return unicodedata.normalize("NFKC", text).strip()


def same_text(typed, target):
    """The typed sentence matches, allowing the final 。 or ! to be left off."""
    typed, target = normalize(typed), normalize(target)
    return typed == target or typed == target.rstrip(TRAILING_PUNCTUATION)


def furigana(surface, reading):
    """Split a word into [text, ruby] pairs so the reading only sits over the kanji.

    食べ/たべ -> [["食", "た"], ["べ", ""]], 取り扱い/とりあつかい -> [["取", "と"], ["り", ""], ["扱", "あつか"], ["い", ""]].
    If the kana in the word can't be lined up with the reading, the whole word gets one ruby.
    """
    if not reading or not has_kanji(surface):
        return [[surface, ""]]

    runs = RUNS_RE.findall(surface)
    pattern = "".join("(.+?)" if has_kanji(run) else f"({re.escape(to_hiragana(run))})" for run in runs)
    match = re.fullmatch(pattern, reading)
    if not match:
        return [[surface, reading]]
    return [[run, rt if has_kanji(run) else ""] for run, rt in zip(runs, match.groups())]


@lru_cache(maxsize=1)
def tagger():
    import fugashi
    return fugashi.Tagger()


@lru_cache(maxsize=1)
def romanizer():
    import cutlet
    katsu = cutlet.Cutlet()
    katsu.use_foreign_spelling = False  # パテ -> "pate", not "putty"
    for word, reading in READING_OVERRIDES.items():
        katsu.add_exception(word, katsu.romaji(reading).lower())
    return katsu


def tokenize(sentence):
    tokens, after_number = [], False
    for node in tagger()(sentence):
        if node.white_space:
            tokens.append(Token(node.white_space, "", None))

        feature, surface = node.feature, node.surface
        follows_number, after_number = after_number, getattr(feature, "pos2", "") == "数詞"
        if not has_kanji(surface):
            tokens.append(Token(surface, "", None))
            continue

        reading = READING_OVERRIDES.get(surface) or to_hiragana(getattr(feature, "kana", None) or "")
        base = getattr(feature, "orthBase", None) or ""
        base_reading = READING_OVERRIDES.get(base) or to_hiragana(getattr(feature, "kanaBase", None) or "")
        # Names (田中, 東京) have unpredictable readings, and numbers and their counters change reading
        # together (四つ, 六日/むいか, 三本/さんぼん), so they get furigana but are never graded.
        learnable = (reading and base_reading and has_kanji(base) and not follows_number
                     and getattr(feature, "pos2", "") not in ("固有名詞", "数詞"))
        tokens.append(Token(surface, reading, base if learnable else None, base_reading if learnable else ""))
    return tokens


def romaji(sentence):
    try:
        return romanizer().romaji(sentence)
    except Exception:
        return ""


@lru_cache(maxsize=1)
def kanji_dictionary():
    with open(settings.ASSETS_DIR / "kanji_data.json", encoding="utf-8") as file:
        return json.load(file)


def kanji_info(text):
    """On/kun readings and meanings for each kanji in the text, for the kanji cards."""
    data = kanji_dictionary()
    return [
        {
            "kanji": kanji,
            "on_readings": data[kanji]["on_readings"],
            "kun_readings": data[kanji]["kun_readings"],
            "meanings": data[kanji]["meanings"],
        }
        for kanji in dict.fromkeys(KANJI_RE.findall(text))
        if kanji in data
    ]
