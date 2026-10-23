"""English meanings for words, from JMdict (via the jmdict-simplified project).

The dictionary is downloaded once into data/ and only read when filling in meanings
(`load_dictionary`, `build_corpus`), never per request.
"""
import json
import re
import urllib.request
import zipfile
from pathlib import Path

from django.conf import settings

from .corpus import has_kanji, to_hiragana
from .models import KanaWord, Word


RELEASE_URL = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"
FULL_ENGLISH_ASSET = re.compile(r"jmdict-eng-\d[^/]*\.json\.zip")  # not the -common or examples variants
MAX_SENSES = 3


def cached_dictionary():
    """The newest downloaded JMdict file in data/, or None."""
    found = sorted(path for path in settings.DATA_DIR.glob("jmdict-eng-*") if FULL_ENGLISH_ASSET.fullmatch(path.name))
    return found[-1] if found else None


def download_dictionary():
    """Download the latest full English JMdict release into data/ and return its path."""
    with urllib.request.urlopen(RELEASE_URL, timeout=60) as response:
        assets = json.load(response)["assets"]
    asset = next((a for a in assets if FULL_ENGLISH_ASSET.fullmatch(a["name"])), None)
    if asset is None:
        raise RuntimeError("No full English JMdict file in the latest jmdict-simplified release.")

    settings.DATA_DIR.mkdir(exist_ok=True)
    target = settings.DATA_DIR / asset["name"]
    partial = target.with_name(target.name + ".part")
    with urllib.request.urlopen(asset["browser_download_url"], timeout=60) as response, open(partial, "wb") as file:
        while chunk := response.read(1 << 20):
            file.write(chunk)
    partial.rename(target)
    return target


def read_entries(path):
    """The list of JMdict entries from a .json file or a .json.zip holding one."""
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            name = next(n for n in archive.namelist() if n.endswith(".json"))
            with archive.open(name) as file:
                return json.load(file)["words"]
    with open(path, encoding="utf-8") as file:
        return json.load(file)["words"]


def index_by_kanji(entries, texts):
    """{kanji text: [entries]} for just the texts we need, so the 200k entries aren't all kept around."""
    index = {}
    for entry in entries:
        for form in entry["kanji"]:
            if form["text"] in texts:
                index.setdefault(form["text"], []).append(entry)
    return index


def index_by_kana(entries, readings):
    """{reading: [entries]} for just the readings we need, for kana-only words."""
    index = {}
    for entry in entries:
        for reading in {to_hiragana(form["text"]) for form in entry["kana"]} & readings:
            index.setdefault(reading, []).append(entry)
    return index


def entry_meanings(entry, text, reading=None):
    """One string per sense (its English glosses joined with '; '), up to MAX_SENSES.
    A sense that applies only to certain kanji or kana forms is skipped for the others (text=None: kana-only word)."""
    meanings = []
    for sense in entry["sense"]:
        if text is not None and sense["appliesToKanji"] != ["*"] and text not in sense["appliesToKanji"]:
            continue
        if reading and sense["appliesToKana"] != ["*"] and reading not in map(to_hiragana, sense["appliesToKana"]):
            continue
        glosses = [g["text"] for g in sense["gloss"] if g["lang"] == "eng"]
        if glosses:
            meanings.append("; ".join(glosses))
    return meanings[:MAX_SENSES]


def find_meanings(index, text, reading):
    """Meanings for a word. Entries with this exact text and reading win; failing that, any entry with
    the text. Among several matches the common one wins (then JMdict's own order)."""
    entries = index.get(text, [])
    exact = [e for e in entries if any(to_hiragana(k["text"]) == reading for k in e["kana"])]
    for group, group_reading in ((exact, reading), (entries, None)):
        def commonness(entry):
            kanji = any(k["common"] for k in entry["kanji"] if k["text"] == text)
            kana = any(k["common"] for k in entry["kana"] if to_hiragana(k["text"]) == reading)
            return -(kanji + kana)
        for entry in sorted(group, key=commonness):
            meanings = entry_meanings(entry, text, group_reading)
            if meanings:
                return meanings
    return []


def find_kana_meanings(kanji_index, kana_index, text, reading, lemma):
    """Meanings for a kana-only word. The lemma's kanji spelling (噤む for つぐむ) picks the entry when there is
    one; otherwise entries with this reading, preferring ones usually written in kana, then common ones."""
    if has_kanji(lemma):
        meanings = find_meanings(kanji_index, lemma, reading)
        if meanings:
            return meanings

    def rank(entry):
        usually_kana = not entry["kanji"] or "uk" in entry["sense"][0].get("misc", [])
        common = any(k["common"] for k in entry["kana"] if to_hiragana(k["text"]) == reading)
        return (not usually_kana, not common)

    for entry in sorted(kana_index.get(reading, []), key=rank):
        meanings = entry_meanings(entry, None, reading)
        if meanings:
            return meanings
    return []


def fill_meanings(words, path, kana_words=()):
    """Set `meanings` on the given Words and KanaWords from the dictionary file (read once).
    Returns how many of each have meanings."""
    words, kana_words = list(words), list(kana_words)
    entries = read_entries(path)
    kanji_index = index_by_kanji(entries, {word.text for word in words} | {kana.lemma for kana in kana_words})
    kana_index = index_by_kana(entries, {kana.reading for kana in kana_words})

    def update(items, lookup, model):
        changed = []
        for item in items:
            meanings = lookup(item)
            if meanings and meanings != item.meanings:
                item.meanings = meanings
                changed.append(item)
        model.objects.bulk_update(changed, ["meanings"], batch_size=1000)
        return sum(1 for item in items if item.meanings)

    return (
        update(words, lambda word: find_meanings(kanji_index, word.text, word.reading), Word),
        update(kana_words, lambda kana: find_kana_meanings(kanji_index, kana_index, kana.text, kana.reading, kana.lemma), KanaWord),
    )
