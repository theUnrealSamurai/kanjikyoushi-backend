"""The CPU-heavy half of build_corpus: tokenizing and romaji for a batch of sentences, run in a pool of processes.

Kept free of models (a pool process only imports this and corpus.py) and of database ids: a worker doesn't know
them, so it returns each sentence's tokens as a JSON template with a %s where a word id goes, plus the words it
used. build_corpus (the one process that touches the database) turns the words into ids and fills the templates.
"""
import json
import os

from .corpus import romaji, tokenize


MARK = ""  # stands for an id in the template; a sentence that has it is skipped (private use: nobody can type it)


def init_worker():
    """Pool initializer: build the tokenizer and romanizer once per process, not on the first batch."""
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "server.settings")
    tokenize("今日")
    romaji("今日")


def analyze(texts):
    """[(index in texts, romaji, template, ids, word slots)] for the sentences that have something to learn, and the
    list of words they use: (text, reading) for a Word, (text, reading, lemma) for a KanaWord. `template` is the
    sentence's tokens as JSON (see Sentence.tokens) with a %s for each word id, `ids` the indexes into the word list
    in the order the %s appear, and `word slots` the indexes of the Words (not kana words) in it, each once.
    Sentences fugashi split without keeping every character are dropped, as are ones with no word to learn."""
    keys, kept = {}, []
    for index, text in enumerate(texts):
        if MARK in text:
            continue
        tokens = tokenize(text)
        if not any(token.word for token in tokens) or "".join(token.surface for token in tokens) != text:
            continue
        ids, slots, rows = [], {}, []
        for token in tokens:
            word = keys.setdefault((token.word, token.word_reading), len(keys)) if token.word else None
            kana = keys.setdefault(token.kana_word, len(keys)) if token.kana_word else None
            for slot in (word, kana):
                if slot is not None:
                    ids.append(slot)
            if word is not None:
                slots[word] = None
            rows.append([token.surface, token.reading, MARK if word is not None else None, MARK if kana is not None else None])
        template = json.dumps(rows, ensure_ascii=False).replace("%", "%%").replace(f'"{MARK}"', "%s")
        kept.append((index, romaji(text), template, tuple(ids), tuple(slots)))
    return list(keys), kept
