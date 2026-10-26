import csv
import random
import sys
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from django.db.models.functions import Coalesce

from type.corpus import is_joyo_only, jlpt_level, katakana_count, romaji, tokenize
from type.dictionary import cached_dictionary, fill_meanings
from type.models import KanaWord, Sentence, Word


BATCH_SIZE = 1000


def read_sentences(path, text_column, translation_column, score_column=None):
    """Yield (text, translation, score) from a .txt file (one sentence per line) or a .tsv/.csv with a header row.
    The old df_n1n5*.tsv exports work as they are: their sentence column is called "text".
    The file is streamed, so it can be gigabytes. The score (a float, or None if missing or invalid)
    is only read when a score column is given."""
    if path.suffix == ".txt":
        with open(path, encoding="utf-8") as file:
            for line in file:
                yield line.strip(), "", None
        return

    csv.field_size_limit(sys.maxsize)
    with open(path, encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file, delimiter="\t" if path.suffix == ".tsv" else ",")
        if text_column not in (reader.fieldnames or []):
            raise CommandError(f"No '{text_column}' column in {path.name}; columns are {reader.fieldnames}")
        if score_column and score_column not in reader.fieldnames:
            raise CommandError(f"No '{score_column}' column in {path.name}; columns are {reader.fieldnames}")
        for row in reader:
            score = parse_score(row[score_column]) if score_column else None
            yield (row[text_column] or "").strip(), (row.get(translation_column) or "").strip(), score


def parse_score(value):
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return None if score != score else score  # NaN is no score


class Command(BaseCommand):
    help = "Load sentences into the database, splitting each into words with readings (fugashi) and romaji (cutlet)."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path, help=".txt (one sentence per line), .tsv or .csv")
        parser.add_argument("--text-column", default="text")
        parser.add_argument("--translation-column", default="translation")
        parser.add_argument("--min-length", type=int, default=1, help="skip sentences shorter than this")
        parser.add_argument("--max-length", type=int, default=60, help="skip sentences longer than this")
        parser.add_argument("--min-score", type=float,
                            help="skip rows scoring below this, or with no valid score (see --score-column)")
        parser.add_argument("--score-column", default="score_ev_20230516", help="column read by --min-score")
        parser.add_argument("--sample", type=float, metavar="FRACTION",
                            help="keep each row that passes the filters with this probability, so --limit "
                                 "is spread over the whole file instead of its first rows")
        parser.add_argument("--seed", type=int, default=0, help="random seed for --sample")
        parser.add_argument("--joyo-only", action="store_true", help="skip sentences with any non-jōyō kanji")
        parser.add_argument("--max-katakana", type=int, metavar="N",
                            help="skip sentences with more than N katakana letters (names, loanwords); 0 = none")
        parser.add_argument("--limit", type=int, help="stop after loading this many sentences")
        parser.add_argument("--replace", action="store_true",
                            help="delete existing standalone sentences first (learners' progress is kept; "
                                 "sentences that are in passages stay, but only as passage text)")

    def handle(self, path, text_column, translation_column, min_length, max_length, min_score, score_column,
               sample, seed, joyo_only, max_katakana, limit, replace, **options):
        if not path.exists():
            raise CommandError(f"{path} does not exist")
        if sample is not None and not 0 < sample <= 1:
            raise CommandError("--sample must be a fraction between 0 and 1")
        if replace:
            self.clear_standalone()

        rng = random.Random(seed)

        word_ids = {(text, reading): id for id, text, reading in Word.objects.values_list("id", "text", "reading")}
        kana_ids = {(text, reading, lemma): id for id, text, reading, lemma in KanaWord.objects.values_list("id", "text", "reading", "lemma")}
        seen, batch, loaded = set(), [], 0
        for text, translation, score in read_sentences(path, text_column, translation_column,
                                                       score_column if min_score is not None else None):
            # Cheapest checks first: this runs on every row of a file with millions of them, tokenizing only on the survivors.
            if not text or not min_length <= len(text) <= max_length:
                continue
            if min_score is not None and (score is None or score < min_score):
                continue
            if sample is not None and rng.random() >= sample:  # before the kanji check: same odds, less work
                continue
            if max_katakana is not None and katakana_count(text) > max_katakana:
                continue
            if (joyo_only and not is_joyo_only(text)) or text in seen:
                continue
            seen.add(text)
            batch.append((text, translation))
            if len(batch) == BATCH_SIZE:
                loaded += self.load_batch(batch, word_ids, kana_ids)
                batch = []
                self.stdout.write(f"  {loaded} sentences loaded")
            if limit and loaded + len(batch) >= limit:
                break
        loaded += self.load_batch(batch, word_ids, kana_ids)

        self.update_frequencies()
        self.add_meanings()
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {loaded} sentences. Corpus now has {Sentence.objects.count()} sentences "
            f"and {Word.objects.filter(frequency__gt=0).count()} words."
        ))

    @transaction.atomic
    def load_batch(self, batch, word_ids, kana_ids):
        existing = set(Sentence.objects.filter(text__in=[text for text, _ in batch]).values_list("text", flat=True))
        # A sentence that was only in passages is now part of the corpus too: sentence mode may serve it.
        promoted = Sentence.objects.filter(text__in=existing, standalone=False).update(standalone=True) if existing else 0
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
        Word.objects.bulk_create([Word(text=text, reading=reading, jlpt=jlpt_level(text)) for text, reading in new_words])
        if new_words:
            for id, text, reading in Word.objects.filter(text__in={text for text, _ in new_words}).values_list("id", "text", "reading"):
                word_ids[(text, reading)] = id

        new_kana = {t.kana_word for _, _, tokens in rows for t in tokens if t.kana_word} - kana_ids.keys()
        KanaWord.objects.bulk_create([KanaWord(text=text, reading=reading, lemma=lemma) for text, reading, lemma in new_kana])
        if new_kana:
            for id, *key in KanaWord.objects.filter(text__in={text for text, _, _ in new_kana}).values_list("id", "text", "reading", "lemma"):
                kana_ids[tuple(key)] = id

        sentences = Sentence.objects.bulk_create([
            Sentence(
                text=text,
                translation=translation,
                romaji=romaji(text),
                tokens=[
                    [t.surface, t.reading, word_ids[(t.word, t.word_reading)] if t.word else None,
                     kana_ids[t.kana_word] if t.kana_word else None]
                    for t in tokens
                ],
            )
            for text, translation, tokens in rows
        ])

        through = Sentence.words.through
        through.objects.bulk_create([
            through(sentence_id=sentence.id, word_id=word_id)
            for sentence in sentences
            for word_id in {token[2] for token in sentence.tokens if token[2]}
        ])
        return len(sentences) + promoted

    def clear_standalone(self):
        """--replace: drop the sentence-mode corpus without touching passages. Standalone sentences that are in no
        passage are deleted; the ones a passage uses stay (so its PassageSentence rows survive) as passage-only text."""
        in_passage = Sentence.objects.filter(passages__isnull=False)
        Sentence.objects.filter(id__in=in_passage.values("id")).update(standalone=False)
        Sentence.objects.filter(standalone=True).delete()

    def add_meanings(self):
        """English meanings for words and kana words that have none yet, if the dictionary has already been downloaded."""
        dictionary = cached_dictionary()
        if dictionary is None:
            self.stdout.write("No dictionary yet: run `manage.py load_dictionary` to add English meanings.")
            return
        words, kana_words = Word.objects.filter(meanings=[]), KanaWord.objects.filter(meanings=[])
        if words.exists() or kana_words.exists():
            fill_meanings(words, dictionary, kana_words)

    def update_frequencies(self):
        counts = (
            Sentence.words.through.objects.filter(word=OuterRef("pk"), sentence__standalone=True)  # passages teach nothing
            .order_by().values("word").annotate(n=Count("*")).values("n")
        )
        Word.objects.update(frequency=Coalesce(Subquery(counts), 0))
