import csv
import multiprocessing
import os
import random
import sys
import time
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connection, transaction
from django.db.models import Max

from type.corpus import BEYOND, has_kanji, is_joyo_only, jlpt_level, kanji_rank, katakana_count, strip_invisible
from type.corpus_batch import analyze, init_worker
from type.dictionary import cached_dictionary, fill_meanings
from type.jlpt_list import apply_list_levels
from type.models import KanaWord, Sentence, Word, refresh_bookmarks


BATCH_SIZE = 2000       # sentences per task for the pool, and per transaction
CHUNK = 500             # texts per IN (...) query: stays under SQLite's variable limit
DELETE_CHUNK = 20000    # sentence ids per step when --replace deletes the old corpus
CACHE_KB = 4_000_000    # SQLite page cache during the build
PROGRESS_SECONDS = 10


def read_sentences(path, text_column, translation_column, score_column=None):
    """Yield (text, translation, score) from a .txt file (one sentence per line) or a .tsv/.csv with a header row.
    The old df_n1n5*.tsv exports work as they are: their sentence column is called "text".
    The file is streamed, so it can be gigabytes. The score (a float, or None if missing or invalid)
    is only read when a score column is given. Invisible characters (strip_invisible) are dropped from the text."""
    if path.suffix == ".txt":
        with open(path, encoding="utf-8") as file:
            for line in file:
                yield strip_invisible(line).strip(), "", None
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
            yield strip_invisible(row[text_column] or "").strip(), (row.get(translation_column) or "").strip(), score


def parse_score(value):
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return None if score != score else score  # NaN is no score


def in_rank_order(buckets):
    """(texts, translations, ranks) in batches of BATCH_SIZE, easiest rank first. Each bucket is let go once read."""
    texts, translations, ranks = [], [], []
    for rank in sorted(buckets):
        for text, translation in buckets.pop(rank).items():
            texts.append(text)
            translations.append(translation)
            ranks.append(rank)
            if len(texts) == BATCH_SIZE:
                yield texts, translations, ranks
                texts, translations, ranks = [], [], []
    if texts:
        yield texts, translations, ranks


def ordered_results(pool, tasks, ahead):
    """analyze() of each task in order, at most `ahead` of them in flight: the pool works while the caller writes,
    but a slow database never lets finished batches pile up in memory. Without a pool they run in this process."""
    pending = deque()
    for task in tasks:
        if pool is None:
            yield task, analyze(task[0])
            continue
        pending.append((task, pool.apply_async(analyze, (task[0],))))
        if len(pending) >= ahead:
            task, result = pending.popleft()
            yield task, result.get()
    while pending:
        task, result = pending.popleft()
        yield task, result.get()


def clock(seconds):
    return f"{int(seconds // 3600)}:{int(seconds % 3600 // 60):02d}:{int(seconds % 60):02d}"


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
        parser.add_argument("--jlpt-only", action="store_true",
                            help="skip sentences with any kanji that is not on the JLPT lists (N5 to N1)")
        parser.add_argument("--max-katakana", type=int, metavar="N",
                            help="skip sentences with more than N katakana letters (names, loanwords); 0 = none")
        parser.add_argument("--limit", type=int,
                            help="stop reading the file after this many sentences have passed the filters")
        parser.add_argument("--workers", type=int, default=max((os.cpu_count() or 2) - 1, 1),
                            help="processes that tokenize in parallel (default: CPU count - 1)")
        parser.add_argument("--drop-indexes", action="store_true",
                            help="drop the through table's word indexes during the load and rebuild them after: faster "
                                 "for a big load, but the indexes are missing until the build ends")
        parser.add_argument("--replace", action="store_true",
                            help="delete existing standalone sentences first (learners' progress is kept; "
                                 "sentences that are in passages stay, but only as passage text)")

    def handle(self, path, text_column, translation_column, min_length, max_length, min_score, score_column,
               sample, seed, joyo_only, jlpt_only, max_katakana, limit, workers, drop_indexes, replace, **options):
        if not path.exists():
            raise CommandError(f"{path} does not exist")
        if sample is not None and not 0 < sample <= 1:
            raise CommandError("--sample must be a fraction between 0 and 1")
        if workers < 1:
            raise CommandError("--workers must be at least 1")
        with self.fast_sqlite():
            if replace:
                self.clear_standalone()
            buckets = self.collect(path, text_column, translation_column, min_length, max_length, min_score, score_column,
                                   sample, seed, joyo_only, jlpt_only, max_katakana, limit)
            promoted = self.skip_existing(buckets)
            total = sum(len(bucket) for bucket in buckets.values())
            self.stdout.write(f"{total} new sentences to load ({promoted} already there as passage text, now standalone).")

            with self.without_word_indexes(drop_indexes):
                loaded = self.load(buckets, total, workers)
            with self.timed("Word frequencies"):
                self.update_frequencies()
            with self.timed("Bookmarks"):
                refresh_bookmarks()
            self.add_meanings()
            apply_list_levels(Word)  # only words on the JLPT list are taught
        self.stdout.write(self.style.SUCCESS(
            f"Loaded {loaded + promoted} sentences. Corpus now has {Sentence.objects.count()} sentences "
            f"and {Word.objects.filter(frequency__gt=0).count()} words."
        ))

    @contextmanager
    def timed(self, what):
        started = time.monotonic()
        yield
        self.stdout.write(f"{what}: {clock(time.monotonic() - started)}.")

    # ------------------------------------------------------------------ reading

    def collect(self, path, text_column, translation_column, min_length, max_length, min_score, score_column,
                sample, seed, joyo_only, jlpt_only, max_katakana, limit):
        """Read and filter the whole file, then bucket the sentences by rank: {rank: {text: translation}}.
        Texts are unique within a bucket, and a text only ever has one rank, so that is all the deduplicating needed."""
        rng = random.Random(seed)
        buckets, rows, collected, started = {}, 0, 0, time.monotonic()
        for text, translation, score in read_sentences(path, text_column, translation_column,
                                                       score_column if min_score is not None else None):
            rows += 1
            if rows % 1_000_000 == 0:
                self.stdout.write(f"  read {rows} rows, {collected} sentences kept ({rows / (time.monotonic() - started):.0f} rows/s)")
            # Cheapest checks first: this runs on every row of a file with millions of them, tokenizing only on the survivors.
            if not text or not min_length <= len(text) <= max_length:
                continue
            if min_score is not None and (score is None or score < min_score):
                continue
            if sample is not None and rng.random() >= sample:  # before the kanji check: same odds, less work
                continue
            if max_katakana is not None and katakana_count(text) > max_katakana:
                continue
            if not has_kanji(text) or (joyo_only and not is_joyo_only(text)):  # no kanji: nothing to learn
                continue
            rank = kanji_rank(text)
            if jlpt_only and rank == BEYOND:
                continue
            bucket = buckets.setdefault(rank, {})
            if text in bucket:
                continue
            bucket[text] = translation
            collected += 1
            if limit and collected >= limit:
                break
        self.stdout.write(f"Read {rows} rows in {clock(time.monotonic() - started)}: {collected} sentences kept.")
        return buckets

    def skip_existing(self, buckets):
        """Drop the sentences the database already has from the buckets. One that was only passage text is now part
        of the corpus too, so sentence mode may serve it: it becomes standalone. Returns how many did."""
        if not Sentence.objects.exists():
            return 0
        existing = set(Sentence.objects.values_list("text", flat=True).iterator(chunk_size=20000))
        promoted = 0
        for rank, bucket in buckets.items():
            found = [text for text in bucket if text in existing]
            for text in found:
                del bucket[text]
            for start in range(0, len(found), CHUNK):
                promoted += Sentence.objects.filter(text__in=found[start:start + CHUNK], standalone=False).update(standalone=True)
        return promoted

    # ------------------------------------------------------------------ loading

    def load(self, buckets, total, workers):
        """Tokenize in a pool and write in rank order: the ids the sentences get ascend with their rank, which is what
        the bookmarks rely on. Only this process touches the database."""
        word_ids = {(text, reading): id for id, text, reading in Word.objects.values_list("id", "text", "reading")}
        kana_ids = {(text, reading, lemma): id for id, text, reading, lemma in KanaWord.objects.values_list("id", "text", "reading", "lemma")}
        state = {
            "word_max": Word.objects.aggregate(Max("id"))["id__max"] or 0,
            "kana_max": KanaWord.objects.aggregate(Max("id"))["id__max"] or 0,
            "next_id": self.first_sentence_id(),
        }
        pool = None
        if workers > 1 and total > BATCH_SIZE:
            pool = multiprocessing.get_context("spawn").Pool(workers, initializer=init_worker)
        loaded, seen, started, shown = 0, 0, time.monotonic(), 0.0
        try:
            for task, result in ordered_results(pool, in_rank_order(buckets), workers * 2):
                loaded += self.load_batch(task, result, word_ids, kana_ids, state)
                seen += len(task[0])
                if time.monotonic() - shown >= PROGRESS_SECONDS:
                    shown = time.monotonic()
                    rate = seen / (shown - started)
                    self.stdout.write(f"  {seen}/{total} sentences ({loaded} loaded, {rate:.0f}/s, ETA {clock((total - seen) / rate)})")
        finally:
            if pool is not None:
                pool.terminate()
                pool.join()
        self.stdout.write(f"Loaded in {clock(time.monotonic() - started)}.")
        self.reset_sequences()
        return loaded

    def first_sentence_id(self):
        """The id for the next sentence: past every id ever used (SQLite's AUTOINCREMENT counter included), so a deleted
        sentence's id is never reused: a learner's `recent` list might still hold it."""
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT MAX(id) FROM {Sentence._meta.db_table}")
            top = cursor.fetchone()[0] or 0
            if connection.vendor == "sqlite":
                cursor.execute("SELECT seq FROM sqlite_sequence WHERE name = %s", [Sentence._meta.db_table])
                row = cursor.fetchone()
                top = max(top, row[0] if row else 0)
        return top + 1

    def reset_sequences(self):
        """Sentences get their ids from here, not from the database, so a database with sequences must catch up."""
        if connection.vendor == "sqlite":
            return
        through = Sentence.words.through
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [Sentence, through]):
                cursor.execute(sql)

    @transaction.atomic
    def load_batch(self, task, result, word_ids, kana_ids, state):
        """Write one batch: new words first, then the sentences with raw executemany (the ORM costs more than the
        tokenizing does) and their links to words. Returns how many sentences it stored."""
        _, translations, ranks = task
        keys, kept = result
        self.add_words(keys, word_ids, kana_ids, state)
        ids = [word_ids[key] if len(key) == 2 else kana_ids[key] for key in keys]

        sentences, links = [], []
        for index, romaji, template, slots, words in kept:
            id = state["next_id"] + len(sentences)
            tokens = template % tuple(map(ids.__getitem__, slots))
            sentences.append((id, task[0][index], translations[index], romaji, tokens, ranks[index]))
            links.extend((id, ids[word]) for word in words)
        state["next_id"] += len(sentences)

        sentence, through = Sentence._meta.db_table, Sentence.words.through._meta.db_table
        with connection.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {sentence} (id, text, translation, romaji, tokens, standalone, rank) VALUES (%s, %s, %s, %s, %s, 1, %s)",
                sentences)
            cursor.executemany(f"INSERT INTO {through} (sentence_id, word_id) VALUES (%s, %s)", links)
        return len(sentences)

    def add_words(self, keys, word_ids, kana_ids, state):
        """Create the Words and KanaWords a batch uses that the database lacks, and remember their ids."""
        new_words = [key for key in keys if len(key) == 2 and key not in word_ids]
        if new_words:
            Word.objects.bulk_create([Word(text=text, reading=reading, jlpt=jlpt_level(text), rank=kanji_rank(text))
                                      for text, reading in new_words], batch_size=500)
            for id, text, reading in Word.objects.filter(id__gt=state["word_max"]).values_list("id", "text", "reading"):
                word_ids[(text, reading)] = id
                state["word_max"] = max(state["word_max"], id)
        new_kana = [key for key in keys if len(key) == 3 and key not in kana_ids]
        if new_kana:
            KanaWord.objects.bulk_create([KanaWord(text=text, reading=reading, lemma=lemma)
                                          for text, reading, lemma in new_kana], batch_size=500)
            for id, text, reading, lemma in KanaWord.objects.filter(id__gt=state["kana_max"]).values_list("id", "text", "reading", "lemma"):
                kana_ids[(text, reading, lemma)] = id
                state["kana_max"] = max(state["kana_max"], id)

    @contextmanager
    def fast_sqlite(self):
        """SQLite settings for a big load: no fsync (a crash means rerunning the build, not a corrupt database, as
        long as the file is not the live one) and a big page cache. Put back afterwards."""
        if connection.vendor != "sqlite" or connection.in_atomic_block:
            yield
            return
        with connection.cursor() as cursor:
            old = {name: cursor.execute(f"PRAGMA {name}").fetchone()[0] for name in ("synchronous", "cache_size", "journal_mode")}
            cursor.execute("PRAGMA synchronous = OFF")
            cursor.execute(f"PRAGMA cache_size = -{CACHE_KB}")
            cursor.execute("PRAGMA journal_mode = WAL")  # commits append to the log, and a page the batches keep touching is written once
            cursor.execute("PRAGMA wal_autocheckpoint = 200000")
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                for name, value in old.items():
                    cursor.execute(f"PRAGMA {name} = {value}")

    @contextmanager
    def without_word_indexes(self, drop):
        """A big load goes much faster without the indexes on the through table that start with word_id (every row
        lands at a random place in them; the sentence_id ones just grow at the end). Dropped here, rebuilt in one
        sorted pass afterwards, with the same names."""
        if not drop or connection.vendor != "sqlite":
            yield
            return
        table = Sentence.words.through._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = %s AND sql IS NOT NULL", [table])
            indexes = [(name, sql) for name, sql in cursor.fetchall()
                       if cursor.execute(f'PRAGMA index_info("{name}")').fetchall()[0][2] == "word_id"]
            for name, _ in indexes:
                cursor.execute(f'DROP INDEX "{name}"')
        try:
            yield
        finally:
            for name, sql in indexes:
                with self.timed(f"Rebuilt index {name}"), connection.cursor() as cursor:
                    cursor.execute(sql)

    # ------------------------------------------------------------------ replacing

    def clear_standalone(self):
        """--replace: drop the sentence-mode corpus without touching passages. Standalone sentences that are in no
        passage are deleted; the ones a passage uses stay (so its PassageSentence rows survive) as passage-only text.
        Raw SQL in id windows, links first, a transaction each: the ORM would collect every row before deleting."""
        sentence, through = Sentence._meta.db_table, Sentence.words.through._meta.db_table
        passages = Sentence.passages.through._meta.db_table
        with connection.cursor() as cursor, transaction.atomic():
            cursor.execute(f"UPDATE {sentence} SET standalone = %s WHERE id IN (SELECT sentence_id FROM {passages})", [False])
            cursor.execute(f"SELECT MIN(id), MAX(id) FROM {sentence} WHERE standalone = %s", [True])
            low, high = cursor.fetchone()
        deleted, started = 0, time.monotonic()
        for start in range(low or 0, (high or -1) + 1, DELETE_CHUNK):
            window = [start, start + DELETE_CHUNK - 1, True]
            with connection.cursor() as cursor, transaction.atomic():
                cursor.execute(f"DELETE FROM {through} WHERE sentence_id IN "
                               f"(SELECT id FROM {sentence} WHERE id BETWEEN %s AND %s AND standalone = %s)", window)
                cursor.execute(f"DELETE FROM {sentence} WHERE id BETWEEN %s AND %s AND standalone = %s", window)
                deleted += cursor.rowcount
            if time.monotonic() - started >= PROGRESS_SECONDS * 3 and (start - low) // DELETE_CHUNK % 50 == 0:
                self.stdout.write(f"  {deleted} old sentences deleted")
        self.stdout.write(f"Replaced: deleted {deleted} standalone sentences.")

    # ------------------------------------------------------------------ afterwards

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
        """Each word's frequency: the standalone sentences it is in (passages teach nothing). Two passes the database
        does well: count every link by word (an index-only scan), then take off the links of the few passage-only
        sentences. Nothing is looked up per word or per sentence."""
        through, sentence = Sentence.words.through._meta.db_table, Sentence._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT word_id, COUNT(*) FROM {through} GROUP BY word_id")
            counts = dict(cursor.fetchall())
            cursor.execute(f"SELECT word_id, COUNT(*) FROM {through} WHERE sentence_id IN "
                           f"(SELECT id FROM {sentence} WHERE standalone = %s) GROUP BY word_id", [False])
            for word_id, count in cursor.fetchall():
                counts[word_id] -= count
            changes = [(counts.get(id, 0), id) for id, frequency in Word.objects.values_list("id", "frequency")
                       if counts.get(id, 0) != frequency]
            with transaction.atomic():
                cursor.executemany(f"UPDATE {Word._meta.db_table} SET frequency = %s WHERE id = %s", changes)
