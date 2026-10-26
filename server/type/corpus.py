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

# UniDic splits 1人 and 2人 into a bare number and the counter 人 (にん), but they're read ひとり and ふたり, like
# 一人 and 二人. Not after another digit (12人 is じゅうににん) and not before 前 (1人前, a serving, is いちにんまえ).
PEOPLE = {"1": "ひとり", "１": "ひとり", "2": "ふたり", "２": "ふたり"}
DIGITS = "0123456789０１２３４５６７８９.．,，"
PEOPLE_ROMAJI_RE = re.compile(r"(?<![0-9.,])([12])nin(?! ?mae)")  # cutlet's romaji has the same mistake: 2nin


@dataclass
class Token:
    surface: str            # the text exactly as it appears in the sentence
    reading: str            # hiragana reading of the surface ("" for kana and symbols)
    word: str | None        # dictionary form to learn, e.g. 食べる for 食べ; None if not learnable
    word_reading: str = ""  # hiragana reading of the dictionary form
    kana_word: tuple | None = None  # (dictionary form, reading, lemma) of a kana-only word, for Tab look-ups


# Kana-only words worth looking up with Tab (つぐむ, とりあえず). Particles and auxiliaries are grammar, not words.
CONTENT_POS = {"名詞", "代名詞", "動詞", "形容詞", "形状詞", "副詞", "連体詞", "接続詞", "感動詞"}


KATAKANA_RE = re.compile("[\u30a1-\u30f4\u30f7-\u30fa\uff66-\uff9d]")  # letters only: not ー, and not ヵ/ヶ (counters)


def katakana_count(text):
    return len(KATAKANA_RE.findall(text))


def has_kanji(text):
    return bool(KANJI_RE.search(text))


def to_hiragana(text):
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in text)


@lru_cache(maxsize=1)
def invisible_re():
    """Format characters (Unicode category Cf: direction marks, zero-width spaces, the BOM...). They don't show and
    nobody can type them, so a sentence with one could never be finished."""
    chars = "".join(re.escape(chr(c)) for c in range(0x110000) if unicodedata.category(chr(c)) == "Cf")
    return re.compile(f"[{chars}]")


def strip_invisible(text):
    return invisible_re().sub("", text)


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
            tokens.append(Token(surface, "", None, kana_word=kana_word(feature)))
            continue

        reading = READING_OVERRIDES.get(surface) or to_hiragana(getattr(feature, "kana", None) or "")
        base = getattr(feature, "orthBase", None) or ""
        base_reading = READING_OVERRIDES.get(base) or to_hiragana(getattr(feature, "kanaBase", None) or "")
        # Names (田中, 東京) have unpredictable readings, and numbers and their counters change reading
        # together (四つ, 六日/むいか, 三本/さんぼん), so they get furigana but are never graded.
        learnable = (reading and base_reading and has_kanji(base) and not follows_number
                     and getattr(feature, "pos2", "") not in ("固有名詞", "数詞"))
        tokens.append(Token(surface, reading, base if learnable else None, base_reading if learnable else ""))
    for index, reading in sorted(counted_people([t.surface for t in tokens], [t.reading for t in tokens]).items(), reverse=True):
        tokens[index:index + 2] = [Token(tokens[index].surface + "人", reading, None)]
    return tokens


def counted_people(surfaces, readings):
    """Where a number token and the 人 after it read ひとり or ふたり (see PEOPLE): {index of the number: reading}."""
    return {
        index: PEOPLE[surface]
        for index, surface in enumerate(surfaces[:-1])
        if surface in PEOPLE and surfaces[index + 1] == "人" and readings[index + 1] == "にん"
        and not (index and surfaces[index - 1][-1] in DIGITS)
        and not (index + 2 < len(surfaces) and surfaces[index + 2].startswith("前"))
    }


def people_romaji(text):
    """Romaji with 1nin and 2nin read hitori and futari (see PEOPLE), capitalized where a sentence starts."""
    def word(match):
        spelled = "hitori" if match[1] == "1" else "futari"
        return spelled.capitalize() if text[:match.start()].rstrip()[-1:] in ("", ".", "!", "?") else spelled
    return PEOPLE_ROMAJI_RE.sub(word, text)


def kana_word(feature):
    """(dictionary form, reading, lemma) for a kana-only content word, else None.
    UniDic's lemma is the standard spelling, often with kanji (つぐむ -> 噤む), which pins down the JMdict entry."""
    base, base_reading = getattr(feature, "orthBase", None), getattr(feature, "kanaBase", None)
    if not base or not base_reading or getattr(feature, "pos1", "") not in CONTENT_POS:
        return None
    if getattr(feature, "pos2", "") in ("助動詞語幹", "数詞"):
        return None
    lemma = (getattr(feature, "lemma", None) or "").split("-")[0]  # "ドル-dollar" -> "ドル"
    return base, to_hiragana(base_reading), lemma


def romaji(sentence):
    try:
        return people_romaji(romanizer().romaji(sentence))
    except Exception:
        return ""


@lru_cache(maxsize=1)
def kanji_dictionary():
    with open(settings.ASSETS_DIR / "kanji_data.json", encoding="utf-8") as file:
        return json.load(file)


@lru_cache(maxsize=1)
def joyo_kanji():
    """The jōyō kanji: school grades 1-6 and 8 (secondary school) in kanji_data.json. Grade 9/10 (jinmeiyō)
    and kanji with no grade are not. 々 〆 ヶ count as kanji here but aren't characters to learn."""
    return frozenset(
        kanji for kanji, info in kanji_dictionary().items() if info.get("grade") in (1, 2, 3, 4, 5, 6, 8)
    ) | frozenset("々〆ヶ")


def is_joyo_only(text):
    joyo = joyo_kanji()
    return all(kanji in joyo for kanji in KANJI_RE.findall(text))


@lru_cache(maxsize=1)
def jlpt_kanji():
    """Kanji -> JLPT level (5 = N5 ... 1 = N1), from the same lists as the frontend's onboarding page.
    kanji_data.json's own "jlpt" is the old four-level test, so it can't say N5 from N4."""
    with open(settings.ASSETS_DIR / "jlpt_kanji.json", encoding="utf-8") as file:
        lists = json.load(file)
    return {kanji: level for level in range(1, 6) for kanji in lists[f"N{level}"]}


def jlpt_level(text):
    """A word is as hard as its hardest kanji: 食べる is N5, 部屋 is N3. 0 means beyond N1.
    々 〆 ヶ aren't kanji to learn, so they don't count."""
    levels = jlpt_kanji()
    return min((levels.get(kanji, 0) for kanji in KANJI_RE.findall(text) if kanji not in "々〆ヶ"), default=0)


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
