import csv
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce

from type.corpus import romaji, tokenize
from type.models import Sentence, Word


BATCH_SIZE = 1000


def read_sentences(path, text_column, translation_column):
    """Yield (text, translation) from a .txt file (one sentence per line) or a .tsv/.csv with a header row.
    The old df_n1n5*.tsv exports work as they are: their sentence column is called "text"."""
    if path.suffix == ".txt":
        with open(path, encoding="utf-8") as file:
            for line in file:
                yield line.strip(), ""
        return

    csv.field_size_limit(sys.maxsize)
    with open(path, encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, delimiter="\t" if path.suffix == ".tsv" else ",")
        if text_column not in (reader.fieldnames or []):
            raise CommandError(f"No '{text_column}' column in {path.name}; columns are {reader.fieldnames}")
        for row in reader:
            yield (row[text_column] or "").strip(), (row.get(translation_column) or "").strip()


class Command(BaseCommand):
    help = "Load sentences into the database, splitting each into words with readings (fugashi) and romaji (cutlet)."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path, help=".txt (one sentence per line), .tsv or .csv")
        parser.add_argument("--text-column", default="text")
        parser.add_argument("--translation-column", default="translation")
        parser.add_argument("--max-length", type=int, default=60, help="skip sentences longer than this")
        parser.add_argument("--limit", type=int, help="stop after loading this many sentences")
        parser.add_argument("--replace", action="store_true",
                            help="delete existing sentences first (learners' progress is kept)")

    def handle(self, path, text_column, translation_column, max_length, limit, replace, **options):
        if not path.exists():
            raise CommandError(f"{path} does not exist")
        if replace:
            Sentence.objects.all().delete()

        word_ids = {(text, reading): id for id, text, reading in Word.objects.values_list("id", "text", "reading")}
        seen, batch, loaded = set(), [], 0
        for text, translation in read_sentences(path, text_column, translation_column):
            if not text or len(text) > max_length or text in seen:
                continue
            seen.add(text)
            batch.append((text, translation))
            if len(batch) == BATCH_SIZE:
                loaded += self.load_batch(batch, word_ids)
                batch = []
                self.stdout.write(f"  {loaded} sentences loaded")
            if limit and loaded + len(batch) >= limit:
                break
        loaded += self.load_batch(batch, word_ids)

        self.update_frequencies()
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {loaded} sentences. Corpus now has {Sentence.objects.count()} sentences "
            f"and {Word.objects.filter(frequency__gt=0).count()} words."
        ))

    @transaction.atomic
    def load_batch(self, batch, word_ids):
        existing = set(Sentence.objects.filter(text__in=[text for text, _ in batch]).values_list("text", flat=True))
        rows = []
        for text, translation in batch:
            if text in existing:
                continue
            tokens = tokenize(text)
            # Only keep sentences that have something to learn and that fugashi split without losing characters.
            if not any(token.word for token in tokens) or "".join(token.surface for token in tokens) != text:
                continue
            rows.append((text, translation, tokens))

        new_words = {(t.word, t.word_reading) for _, _, tokens in rows for t in tokens if t.word} - word_ids.keys()
        Word.objects.bulk_create([Word(text=text, reading=reading) for text, reading in new_words])
        if new_words:
            for id, text, reading in Word.objects.filter(text__in={text for text, _ in new_words}).values_list("id", "text", "reading"):
                word_ids[(text, reading)] = id

        sentences = Sentence.objects.bulk_create([
            Sentence(
                text=text,
                translation=translation,
                romaji=romaji(text),
                tokens=[[t.surface, t.reading, word_ids[(t.word, t.word_reading)] if t.word else None] for t in tokens],
            )
            for text, translation, tokens in rows
        ])

        through = Sentence.words.through
        through.objects.bulk_create([
            through(sentence_id=sentence.id, word_id=word_id)
            for sentence in sentences
            for word_id in {token[2] for token in sentence.tokens if token[2]}
        ])
        return len(sentences)

    def update_frequencies(self):
        counts = (
            Sentence.words.through.objects.filter(word=OuterRef("pk"))
            .order_by().values("word").annotate(n=Count("*")).values("n")
        )
        Word.objects.update(frequency=Coalesce(Subquery(counts), 0))
