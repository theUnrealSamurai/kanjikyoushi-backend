"""Load a passages file (made by `build_passages`) into the database.

Pure Python and Django: no fugashi, cutlet or pandas, so it runs on the phone as well as on the Mac.
Everything is batched (bulk_create, one transaction per batch) so ~150k sentences load in minutes on SQLite.
"""
import gzip
import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from type.corpus import jlpt_level
from type.models import KanaWord, Passage, PassageSentence, Sentence, Word


BATCH_PASSAGES = 500   # passages per transaction
CHUNK = 500            # ids or texts per IN (...) query: stays under SQLite's variable limit
DELETE_CHUNK = 2000


def chunks(items, size):
    items = list(items)
    for start in range(0, len(items), size):
        yield items[start:start + size]


def read_file(path):
    """(header, dictionary, passage iterator) from a passages .jsonl.gz (or plain .jsonl)."""
    opener = gzip.open if path.suffix == ".gz" else open
    file = opener(path, "rt", encoding="utf-8")
    try:
        header, dictionary = json.loads(file.readline()), json.loads(file.readline())
    except json.JSONDecodeError as error:
        file.close()
        raise CommandError(f"{path} is not a passages file: {error}")
    if header.get("kind") != "header" or dictionary.get("kind") != "dictionary":
        file.close()
        raise CommandError(f"{path} is not a passages file: it should start with a header line and a dictionary line.")

    def passages():
        with file:
            for line in file:
                if line.strip():
                    yield json.loads(line)

    return header, dictionary, passages()


class Command(BaseCommand):
    help = "Load passages (paragraph mode) from a file made by build_passages."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path, help="passages .jsonl.gz from build_passages")
        parser.add_argument("--replace", action="store_true",
                            help="delete this source's passages first, and the passage-only sentences left without one")

    def handle(self, path, replace, **options):
        if not path.exists():
            raise CommandError(f"{path} does not exist")
        header, dictionary, passages = read_file(path)
        source = header["source"]
        counts = header.get("counts", {})
        self.stdout.write(f"{path.name}: {counts.get('passages', '?')} passages, {counts.get('sentences', '?')} sentences from {source}.")

        if replace:
            self.replace(source)

        word_ids, kana_ids, created_words, created_kana = self.load_words(dictionary)
        self.stdout.write(f"Words: {created_words} new of {len(dictionary['words'])}; kana words: {created_kana} new of {len(dictionary['kana_words'])}.")

        done = set(Passage.objects.filter(source=source).values_list("work_id", "position"))
        sentence_ids = {}  # text -> id for sentences seen in this run
        totals = {"passages": 0, "skipped": 0, "sentences_new": 0, "sentences_reused": 0}
        batch = []
        for passage in passages:
            if (passage["work_id"], passage["position"]) in done:
                totals["skipped"] += 1
                continue
            done.add((passage["work_id"], passage["position"]))
            batch.append(passage)
            if len(batch) == BATCH_PASSAGES:
                self.load_batch(batch, word_ids, kana_ids, sentence_ids, totals)
                batch = []
                self.stdout.write(f"  {totals['passages']} passages loaded ({totals['sentences_new']} new sentences)")
        if batch:
            self.load_batch(batch, word_ids, kana_ids, sentence_ids, totals)

        self.stdout.write(self.style.SUCCESS(
            f"Loaded {totals['passages']} passages ({totals['skipped']} already there). "
            f"Sentences: {totals['sentences_new']} new (not standalone), {totals['sentences_reused']} reused from the corpus."
        ))

    # ------------------------------------------------------------------ replace

    def replace(self, source):
        passages = Passage.objects.filter(source=source)
        count = passages.count()
        for ids in chunks(passages.values_list("id", flat=True), DELETE_CHUNK):
            with transaction.atomic():
                Passage.objects.filter(id__in=ids).delete()  # their PassageSentence rows go with them
        orphans = list(Sentence.objects.filter(standalone=False, passages__isnull=True).values_list("id", flat=True))
        for ids in chunks(orphans, DELETE_CHUNK):
            with transaction.atomic():
                Sentence.objects.filter(id__in=ids).delete()  # and their word links
        self.stdout.write(f"Replaced: deleted {count} {source} passages and {len(orphans)} sentences that were only in passages.")

    # ------------------------------------------------------------------ words

    def load_words(self, dictionary):
        """Create the Word and KanaWord rows the file needs but the database lacks (frequency 0), and return
        {(text, reading): id} and {(text, reading, lemma): id}. Existing rows, and their frequency, are left alone."""
        with transaction.atomic():
            word_ids = {(text, reading): id for id, text, reading in Word.objects.values_list("id", "text", "reading")}
            new = [row for row in dictionary["words"] if (row[0], row[1]) not in word_ids]
            Word.objects.bulk_create(
                [Word(text=text, reading=reading, jlpt=jlpt, meanings=meanings) for text, reading, jlpt, meanings in new],
                batch_size=1000, ignore_conflicts=True,
            )
            kana_ids = {(text, reading, lemma): id
                        for id, text, reading, lemma in KanaWord.objects.values_list("id", "text", "reading", "lemma")}
            new_kana = [row for row in dictionary["kana_words"] if (row[0], row[1], row[2]) not in kana_ids]
            KanaWord.objects.bulk_create(
                [KanaWord(text=text, reading=reading, lemma=lemma, meanings=meanings)
                 for text, reading, lemma, meanings in new_kana],
                batch_size=1000, ignore_conflicts=True,
            )
            if new:
                word_ids = {(text, reading): id for id, text, reading in Word.objects.values_list("id", "text", "reading")}
            if new_kana:
                kana_ids = {(text, reading, lemma): id
                            for id, text, reading, lemma in KanaWord.objects.values_list("id", "text", "reading", "lemma")}
        return word_ids, kana_ids, len(new), len(new_kana)

    def word_id(self, key, word_ids):
        """The id for a word key; a word the dictionary line missed is created on the spot."""
        key = tuple(key)
        if key not in word_ids:
            word_ids[key] = Word.objects.get_or_create(text=key[0], reading=key[1], defaults={"jlpt": jlpt_level(key[0])})[0].id
        return word_ids[key]

    def kana_id(self, key, kana_ids):
        key = tuple(key)
        if key not in kana_ids:
            kana_ids[key] = KanaWord.objects.get_or_create(text=key[0], reading=key[1], lemma=key[2])[0].id
        return kana_ids[key]

    # ------------------------------------------------------------------ passages

    @transaction.atomic
    def load_batch(self, passages, word_ids, kana_ids, sentence_ids, totals):
        # Sentences: reuse any row whose text exists (leaving `standalone` alone), create the rest as passage-only.
        wanted = {s["text"]: s for passage in passages for s in passage["sentences"]}
        missing = [text for text in wanted if text not in sentence_ids]
        for texts in chunks(missing, CHUNK):
            for id, text in Sentence.objects.filter(text__in=texts).values_list("id", "text"):
                sentence_ids[text] = id
                totals["sentences_reused"] += 1
        to_create = [text for text in missing if text not in sentence_ids]
        rows = [
            Sentence(
                text=text, romaji=wanted[text].get("romaji", ""), standalone=False,
                tokens=[
                    [surface, reading, self.word_id(word, word_ids) if word else None,
                     self.kana_id(kana, kana_ids) if kana else None]
                    for surface, reading, word, kana in wanted[text]["tokens"]
                ],
            )
            for text in to_create
        ]
        created = Sentence.objects.bulk_create(rows, batch_size=1000)
        through = Sentence.words.through
        links = []
        for sentence in created:
            sentence_ids[sentence.text] = sentence.id
            links.extend(through(sentence_id=sentence.id, word_id=word_id)
                         for word_id in {token[2] for token in sentence.tokens if token[2]})
        through.objects.bulk_create(links, batch_size=1000)
        totals["sentences_new"] += len(created)

        # Passages and their ordered sentences.
        made = Passage.objects.bulk_create([
            Passage(source=p["source"], work_id=p["work_id"], position=p["position"], title=p["title"],
                    author=p["author"], url=p.get("url", ""), licence=p["licence"], readability=p.get("readability"))
            for p in passages
        ], batch_size=1000)
        PassageSentence.objects.bulk_create([
            PassageSentence(passage_id=passage.id, sentence_id=sentence_ids[s["text"]], position=position)
            for passage, source in zip(made, passages)
            for position, s in enumerate(source["sentences"])
        ], batch_size=1000)
        totals["passages"] += len(made)
