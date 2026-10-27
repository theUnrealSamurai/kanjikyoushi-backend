"""The JLPT vocabulary list: which Words are taught, and at which level.

The list (assets/jlpt_vocab/n1.csv ... n5.csv, see the README there) is matched to Word rows by text and reading;
the result is stored in `Word.list_level` (5 = N5 ... 1 = N1, None = not on the list). Pure Python and Django, no
fugashi or cutlet, so it runs on the phone as well as on the Mac.
"""
import csv
import re
from dataclasses import dataclass

from django.conf import settings
from django.db import transaction

from type.corpus import KANJI_RE, has_kanji, to_hiragana


LEVELS = (5, 4, 3, 2, 1)
CHUNK = 900  # ids per IN (...) query: SQLite allows at most 999 variables in older builds

PARENTHESISED_RE = re.compile(r"[(（][^)）]*[)）]")  # (かさを～) さす: the bracketed part is context, not the word
AFFIX_RE = re.compile(r"[～〜~]")
STRIP_RE = re.compile(r"[～〜~\s]")
HIRAGANA_TAIL_RE = re.compile(r"[ぁ-ゖ]+$")
ENDS_IN_HIRAGANA_RE = re.compile(r"[ぁ-ゖ]$")

# Suffix rows the list writes in kana only (～たち) although the corpus has them as a kanji word (達). Kana-only rows
# are never matched in general (homophones), so these four are spelled out; they stay affixes, so the reading
# must still agree.
KANA_AFFIX_SPELLINGS = {"たち": "達", "すぎ": "過ぎ", "やすい": "易い", "おき": "置き"}


@dataclass
class Entry:
    level: int
    expression: str      # the row as listed, e.g. "足; 脚"
    variants: list       # text spellings to look for: ["足", "脚"]
    readings: list       # hiragana readings that count as the right one
    affix: bool          # had a ～: a counter or suffix (～円, ～化), only safe to match with the reading


def clean_part(text):
    """One ';'-separated piece without parenthesised parts, ～ marks or spaces: '〜 (まる) ごと' -> 'ごと'."""
    return STRIP_RE.sub("", PARENTHESISED_RE.sub("", text))


def spellings(expression):
    """The text variants of a listed expression. The list writes the okurigana once, on the last spelling that has
    it: '堅; 硬; 固い' means 堅い, 硬い and 固い, '伯父; 叔父さん' means 伯父さん too. So a kanji spelling that ends in
    a kanji gets that tail added as well (the bare one is kept, for nouns like 足; 脚)."""
    parts = [part for part in map(clean_part, expression.split(";")) if part]
    tail = ""
    for part in reversed(parts):
        match = HIRAGANA_TAIL_RE.search(part)
        if match and has_kanji(part):
            tail = match.group(0)
            break
    variants = []
    for part in parts:
        variants.append(part)
        if tail and has_kanji(part) and not ENDS_IN_HIRAGANA_RE.search(part):
            variants.append(part + tail)
    return variants


def skeleton(text):
    """The kanji of a spelling, in order, without okurigana: 終る and 終わる are both 終."""
    return "".join(KANJI_RE.findall(text))


def parse_list(folder=None):
    """Every row of n5.csv ... n1.csv as an Entry, easiest level first."""
    folder = folder or settings.ASSETS_DIR / "jlpt_vocab"
    entries = []
    for level in LEVELS:
        with open(folder / f"n{level}.csv", encoding="utf-8", newline="") as file:
            for row in csv.DictReader(file):
                expression = row["expression"]
                readings = [to_hiragana(clean_part(reading)) for reading in row["reading"].split(";")]
                variants = spellings(expression)
                affix = bool(AFFIX_RE.search(expression))
                if affix and len(variants) == 1 and variants[0] in KANA_AFFIX_SPELLINGS:
                    variants.append(KANA_AFFIX_SPELLINGS[variants[0]])
                entries.append(Entry(
                    level=level,
                    expression=expression,
                    variants=variants,
                    readings=[reading for reading in readings if reading],
                    affix=affix,
                ))
    return entries


def list_levels(entries, words):
    """{word id: level} for the Words on the list. `words` is an iterable of (id, text, reading, frequency).

    Each entry picks ONE Word per entry (a list row is one word, even when the corpus has several readings of
    its text): the reading the list gives beats a more frequent reading it doesn't, then a Word that occurs in the
    corpus beats one that never does, then the more frequent one. Affix entries (～円, ～化) are not words by
    themselves, so they only match when the reading agrees. Kana-only entries never match, as Words always have a
    kanji. Spellings with other okurigana compete too: a Word with the same kanji skeleton as one of the entry's
    spellings and a reading the list gives. The list has old ones (終る, 落る) where the corpus mostly has 終わる and
    落ちる, and a row like 下す,おろす means 下ろす, not 下す read くだす. A word on several levels gets the easiest
    one (the highest number)."""
    by_text, by_skeleton = {}, {}
    for id, text, reading, frequency in words:
        by_text.setdefault(text, []).append((id, reading, frequency))
        by_skeleton.setdefault(skeleton(text), []).append((id, reading, frequency))

    levels = {}
    for entry in entries:
        candidates = [candidate for variant in entry.variants if has_kanji(variant)
                      for candidate in by_text.get(variant, [])]
        if entry.affix:
            candidates = [candidate for candidate in candidates if candidate[1] in entry.readings]
        skeletons = {skeleton(variant) for variant in entry.variants if has_kanji(variant)}
        candidates += [candidate for key in skeletons for candidate in by_skeleton.get(key, [])
                       if candidate[1] in entry.readings and candidate not in candidates]
        if not candidates:
            continue
        id = max(candidates, key=lambda c: (c[1] in entry.readings, c[2] > 0, c[2], -c[0]))[0]
        levels[id] = max(levels.get(id, 0), entry.level)
    return levels


def chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def apply_list_levels(word_model, folder=None):
    """Set `list_level` on every row of `word_model`: the list's level, or None for words not on it. Returns
    {level: how many words got it}. Takes the model as an argument so a migration can pass the historical one.
    Only rows whose value changes are written, one bulk UPDATE per level and chunk of ids."""
    rows = list(word_model.objects.values_list("id", "text", "reading", "frequency", "list_level"))
    levels = list_levels(parse_list(folder), [row[:4] for row in rows])

    changes = {}  # new level (None to clear) -> ids
    for id, _, _, _, current in rows:
        wanted = levels.get(id)
        if wanted != current:
            changes.setdefault(wanted, []).append(id)
    if changes:
        with transaction.atomic():
            for level, ids in changes.items():
                for part in chunks(ids, CHUNK):
                    word_model.objects.filter(id__in=part).update(list_level=level)

    counts = {level: 0 for level in LEVELS}
    for level in levels.values():
        counts[level] += 1
    return counts
