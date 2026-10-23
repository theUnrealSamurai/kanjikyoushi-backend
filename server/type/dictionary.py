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

from .corpus import to_hiragana
from .models import Word


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


def entry_meanings(entry, text, reading=None):
    """One string per sense (its English glosses joined with '; '), up to MAX_SENSES.
    A sense that applies only to certain kanji or kana forms is skipped for the others."""
    meanings = []
    for sense in entry["sense"]:
        if sense["appliesToKanji"] != ["*"] and text not in sense["appliesToKanji"]:
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


def fill_meanings(words, path):
    """Set `meanings` on the given Words from the dictionary file. Returns how many got some."""
    words = list(words)
    index = index_by_kanji(read_entries(path), {word.text for word in words})
    changed = []
    for word in words:
        meanings = find_meanings(index, word.text, word.reading)
        if meanings and meanings != word.meanings:
            word.meanings = meanings
            changed.append(word)
    Word.objects.bulk_update(changed, ["meanings"], batch_size=1000)
    return sum(1 for word in words if word.meanings)
