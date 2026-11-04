import hashlib
import sqlite3
import tempfile
from datetime import datetime, timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase, override_settings

from .corpus import BEYOND, kanji_rank
from .management.commands.build_corpus import ordered_results
from .models import Bookmark, KanaWord, Learner, Passage, PassageSentence, ReviewLog, Sentence, UserWord, Word, bookmark


# No test may find (or download) the real dictionary in data/.
NO_DICTIONARY = override_settings(DATA_DIR=Path(tempfile.gettempdir()) / "kanjikyoushi-no-dictionary")

# Easiest first: 今日は雨です (N5 only), 会議は長い (議 is N3), 剣が光る (剣 is N1), 薔薇が咲いた (not on the JLPT lists).
EASY, MEDIUM, HARD, OFF_LIST = "今日は雨です。", "会議は長い。", "剣が光る。", "薔薇が咲いた。"


def build(folder, lines, **options):
    path = Path(folder) / "corpus.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with open(Path(folder) / "log.txt", "w") as log:
        call_command("build_corpus", str(path), stdout=log, **options)


def corpus_snapshot():
    """Everything a build stores, with word ids replaced by what they stand for, in sentence id order."""
    words = {id: (text, reading) for id, text, reading in Word.objects.values_list("id", "text", "reading")}
    kana = {id: (text, reading, lemma) for id, text, reading, lemma in KanaWord.objects.values_list("id", "text", "reading", "lemma")}
    sentences = [
        (s.text, s.rank, s.romaji, s.standalone,
         [[surface, reading, words.get(word), kana.get(k[0]) if k else None] for surface, reading, word, *k in s.tokens],
         sorted(words[id] for id in s.words.values_list("id", flat=True)))
        for s in Sentence.objects.order_by("id")
    ]
    return sentences, sorted((text, reading, rank, frequency, jlpt) for text, reading, rank, frequency, jlpt in
                             Word.objects.values_list("text", "reading", "rank", "frequency", "jlpt")), sorted(kana.values())


@NO_DICTIONARY
class RankOrderTests(TestCase):
    def test_the_ranks_this_relies_on(self):
        self.assertTrue(0 < kanji_rank(EASY) < kanji_rank(MEDIUM) < kanji_rank(HARD) < BEYOND)
        self.assertEqual(kanji_rank(OFF_LIST), BEYOND)

    def test_ids_ascend_with_rank_whatever_the_file_order(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [HARD, MEDIUM, EASY, "先生が来た。", OFF_LIST])
        sentences = list(Sentence.objects.order_by("id"))
        self.assertEqual(len(sentences), 5)
        ranks = [sentence.rank for sentence in sentences]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(sentences[-1].text, OFF_LIST)
        self.assertEqual(sentences[-2].text, HARD)
        for sentence in sentences:
            self.assertEqual(sentence.rank, kanji_rank(sentence.text))
        self.assertTrue(Sentence.objects.get(text=EASY).standalone)

    def test_words_get_their_rank_and_tokens_point_at_them(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [HARD])
        word = Word.objects.get(text="剣")
        self.assertEqual(word.rank, kanji_rank("剣"))
        self.assertEqual(word.jlpt, 1)
        sentence = Sentence.objects.get()
        self.assertEqual([token[:3] for token in sentence.tokens][:2], [["剣", "けん", word.id], ["が", "", None]])
        self.assertEqual(list(sentence.words.values_list("text", flat=True)), ["剣", "光る"])
        self.assertEqual(sentence.romaji, "Ken ga hikaru.")

    def test_a_percent_sign_in_a_sentence_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, ["雨は100%です。"])
        self.assertEqual("".join(token[0] for token in Sentence.objects.get().tokens), "雨は100%です。")

    def test_later_builds_continue_after_the_last_id(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [HARD])
            first = Sentence.objects.get().id
            build(folder, [HARD, EASY])
        self.assertEqual(list(Sentence.objects.order_by("id").values_list("text", flat=True)), [HARD, EASY])
        self.assertEqual(Sentence.objects.get(text=EASY).id, first + 1)

    def test_deleted_ids_are_not_reused(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [HARD, EASY])
            top = Sentence.objects.order_by("id").last().id
            build(folder, [MEDIUM], replace=True)
        self.assertGreater(Sentence.objects.get().id, top)


@NO_DICTIONARY
class FilterTests(TestCase):
    def test_jlpt_only_skips_kanji_off_the_lists(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [EASY, HARD, OFF_LIST], jlpt_only=True)
        self.assertEqual(set(Sentence.objects.values_list("text", flat=True)), {EASY, HARD})

    def test_without_it_they_are_loaded_last(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [OFF_LIST, EASY])
        self.assertEqual(Sentence.objects.order_by("id").last().rank, BEYOND)

    def test_jlpt_only_combines_with_the_other_filters(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [EASY, MEDIUM, HARD, OFF_LIST], jlpt_only=True, max_length=len(HARD), limit=5)
        self.assertEqual(set(Sentence.objects.values_list("text", flat=True)), {HARD})

    def test_min_score_works_with_jlpt_only_and_rank_order(self):
        rows = [(HARD, 2.0), (OFF_LIST, 2.0), (MEDIUM, 0.5), (EASY, 1.5), ("先生が来た。", "")]
        lines = ["id\ttext\tscore"] + [f"{i}\t{text}\t{score}" for i, (text, score) in enumerate(rows)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "corpus.tsv"
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            call_command("build_corpus", str(path), min_score=1.46, score_column="score", jlpt_only=True, stdout=StringIO())
        self.assertEqual(list(Sentence.objects.order_by("id").values_list("text", flat=True)), [EASY, HARD])

    def test_limit_stops_reading_the_file(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [HARD, MEDIUM, EASY], limit=2)
        self.assertEqual(set(Sentence.objects.values_list("text", flat=True)), {HARD, MEDIUM})

    def test_the_same_sentence_twice_is_loaded_once(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [EASY, EASY, EASY])
        self.assertEqual(Sentence.objects.count(), 1)

    def test_workers_must_be_positive(self):
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(CommandError):
            build(folder, [EASY], workers=0)


@NO_DICTIONARY
class BookmarkTests(TestCase):
    def test_bookmarks_are_refreshed_after_a_build(self):
        Bookmark.objects.create(rank=5, last_sentence=999)  # stale
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [HARD, MEDIUM, EASY, "先生が来た。"])
        self.assertFalse(Bookmark.objects.filter(last_sentence=999).exists())
        sentences = list(Sentence.objects.values_list("rank", "id"))
        for rank, last in Bookmark.objects.values_list("rank", "last_sentence"):
            self.assertEqual(last, max(id for r, id in sentences if r <= rank))
        self.assertEqual(Bookmark.objects.count(), len({rank for rank, _ in sentences}))
        # The cached copy was cleared too: the picker sees the new corpus straight away.
        self.assertEqual(bookmark(kanji_rank(EASY)), Sentence.objects.filter(rank__lte=kanji_rank(EASY)).order_by("id").last().id)
        self.assertEqual(bookmark(BEYOND), Sentence.objects.order_by("id").last().id)


@NO_DICTIONARY
class WorkerTests(TestCase):
    LINES = [EASY, MEDIUM, HARD, "先生が来た。", "今日は先生と会議があります。", "田中さんは四つ食べた。", "テントが一つしかない。",
             "雨は100%です。", "本を読んで、お茶を飲んだ。"]

    def build_with(self, workers):
        module = "type.management.commands.build_corpus"
        with tempfile.TemporaryDirectory() as folder, patch(f"{module}.BATCH_SIZE", 2), \
                patch(f"{module}.ordered_results", wraps=ordered_results) as run:
            build(folder, self.LINES, workers=workers)
        self.assertEqual(run.call_args.args[0] is not None, workers > 1)  # a pool only when asked for
        return corpus_snapshot()

    def test_a_pool_stores_exactly_what_one_process_does(self):
        one = self.build_with(1)
        self.assertEqual(len(one[0]), 8)  # テントが一つしかない has nothing to learn
        Sentence.objects.all().delete()
        Word.objects.all().delete()
        KanaWord.objects.all().delete()
        self.assertEqual(self.build_with(2), one)

    def test_frequencies_count_standalone_sentences(self):
        one = self.build_with(1)
        word = dict((text, frequency) for text, _, _, frequency, _ in one[1])
        self.assertEqual(word["先生"], 2)
        self.assertEqual(word["雨"], 2)


@NO_DICTIONARY
class ReplaceTests(TestCase):
    def setUp(self):
        self.word = Word.objects.create(text="雨", reading="あめ")
        through = Sentence.words.through
        self.old = [Sentence.objects.create(text=f"雨が{'降' * n}る。", tokens=[]) for n in range(1, 6)]
        for sentence in self.old:
            through.objects.create(sentence=sentence, word=self.word)
        self.passage_only = Sentence.objects.create(text="雨だ。", tokens=[], standalone=False)
        self.in_passage = self.old[2]  # a standalone sentence that a passage also uses
        through.objects.create(sentence=self.passage_only, word=self.word)
        passage = Passage.objects.create(source="aozora", work_id="1", position=0, title="t", author="a", licence="l")
        PassageSentence.objects.create(passage=passage, sentence=self.passage_only, position=0)
        PassageSentence.objects.create(passage=passage, sentence=self.in_passage, position=1)

    def test_replace_deletes_in_windows_and_keeps_passage_sentences(self):
        with tempfile.TemporaryDirectory() as folder, patch("type.management.commands.build_corpus.DELETE_CHUNK", 2):
            build(folder, [EASY], replace=True)
        self.assertEqual(set(Sentence.objects.values_list("text", flat=True)), {EASY, "雨だ。", self.in_passage.text})
        self.assertEqual(PassageSentence.objects.count(), 2)
        self.in_passage.refresh_from_db()
        self.assertFalse(self.in_passage.standalone)
        # Their word links stay; the deleted sentences' links are gone with them.
        links = Sentence.words.through.objects
        self.assertEqual(set(links.values_list("sentence__text", flat=True)), {EASY, "雨だ。", self.in_passage.text})
        self.assertFalse(links.filter(sentence__in=[s.id for s in self.old if s.id != self.in_passage.id]).exists())
        self.assertEqual(Word.objects.get(text="雨").frequency, 1)  # only EASY counts: passage text teaches nothing

    def test_new_sentences_are_ranked_and_passage_text_keeps_its_ids(self):
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [HARD, EASY], replace=True)
        ids = {text: id for text, id in Sentence.objects.values_list("text", "id")}
        self.assertGreater(ids[EASY], max(s.id for s in self.old))
        self.assertLess(ids[EASY], ids[HARD])
        self.assertEqual(Bookmark.objects.get(rank=kanji_rank(HARD)).last_sentence, ids[HARD])


@NO_DICTIONARY
class IndexTests(TestCase):
    NAMES = ["type_sentence_words_word_sentence"]

    def index_names(self):
        with connection.cursor() as cursor:
            return {row[1] for row in cursor.execute("PRAGMA index_list(type_sentence_words)").fetchall()}

    def test_by_default_the_indexes_stay(self):
        with tempfile.TemporaryDirectory() as folder, patch("type.management.commands.build_corpus.Command.without_word_indexes") as drop:
            build(folder, [EASY])
        drop.assert_called_once_with(False)

    def test_dropping_them_rebuilds_the_word_indexes(self):
        before = self.index_names()
        self.assertIn("type_sentence_words_word_sentence", before)
        self.assertEqual(len(before), 4)  # unique pair, sentence, word, word + sentence
        with tempfile.TemporaryDirectory() as folder:
            build(folder, [EASY, HARD], drop_indexes=True)
        self.assertEqual(self.index_names(), before)
        with connection.cursor() as cursor:
            plan = cursor.execute("EXPLAIN QUERY PLAN SELECT sentence_id FROM type_sentence_words WHERE word_id = 1 AND sentence_id <= 5").fetchall()
        self.assertIn("type_sentence_words_word_sentence", str(plan))

    def test_they_are_rebuilt_even_when_the_load_fails(self):
        before = self.index_names()
        with tempfile.TemporaryDirectory() as folder, \
                patch("type.management.commands.build_corpus.Command.load_batch", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                build(folder, [EASY], drop_indexes=True)
        self.assertEqual(self.index_names(), before)


def copy_database(destination):
    """Copy the (in-memory) test database into a sqlite3 connection: tables, indexes and rows, and AUTOINCREMENT's counters."""
    with connection.cursor() as cursor:
        schema = cursor.execute("SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type = 'index'").fetchall()
        for _, _, sql in schema:
            destination.execute(sql)
        for kind, name, _ in schema:
            if kind == "table":
                rows = cursor.execute(f'SELECT * FROM "{name}"').fetchall()
                if rows:
                    destination.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(rows[0]))})', rows)
        destination.execute("DELETE FROM sqlite_sequence")
        destination.executemany("INSERT INTO sqlite_sequence VALUES (?, ?)", cursor.execute("SELECT name, seq FROM sqlite_sequence").fetchall())
    destination.commit()


class AdoptTests(TestCase):
    """The test database plays the freshly built one; the "live" database is a copy of it taken beforehand and then
    changed, as the phone's would have been while the new one was being built."""

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.live_path = Path(self.folder.name) / "live.sqlite3"
        self.user = User.objects.create_user("taro", "taro@example.com", "a-long-password")
        self.learner = Learner.objects.create(user=self.user, current={"sentence": 5}, recent=[5, 6])
        self.word = Word.objects.create(text="雨", reading="あめ")
        self.other = Word.objects.create(text="先生", reading="せんせい")
        self.sentence = Sentence.objects.create(text="雨です。", tokens=[])
        Passage.objects.create(source="aozora", work_id="1", position=0, title="live passage", author="a", licence="l")

        live = sqlite3.connect(self.live_path)
        copy_database(live)
        self.live = live
        # What happened on the phone since: progress, a new user, and a deleted row (so the sequence is ahead).
        now = datetime(2026, 11, 1, tzinfo=dt_timezone.utc)
        self.progress(now)
        live.commit()

        # The new database: rebuilt corpus (the word ids are the same), and none of that progress yet.
        Sentence.objects.all().delete()
        Sentence.objects.create(text="新しい文。", tokens=[])
        Learner.objects.update(current={}, recent=[])
        Passage.objects.update(title="new passage")

    def progress(self, now):
        live = self.live
        live.execute("UPDATE type_learner SET current = '{\"sentence\": 7}', recent = '[7, 8]', known_kanji = '雨'")
        live.execute("INSERT INTO type_userword (learner_id, word_id, status, sightings) VALUES (?, ?, 'learned', 3)",
                     [self.learner.id, self.word.id])
        live.execute("INSERT INTO type_reviewlog (learner_id, word_id, rating, at) VALUES (?, ?, 3, ?)",
                     [self.learner.id, self.word.id, now.isoformat(sep=" ")])
        live.execute("INSERT INTO type_sentencelog (learner_id, at) VALUES (?, ?)", [self.learner.id, now.isoformat(sep=" ")])
        live.execute("INSERT INTO auth_user (id, password, is_superuser, username, first_name, last_name, email, is_staff, "
                     "is_active, date_joined) VALUES (50, 'x', 0, 'hanako', '', '', '', 0, 1, ?)", [now.isoformat(sep=" ")])
        live.execute("INSERT INTO type_sentencelog (learner_id, at) VALUES (?, ?)", [self.learner.id, now.isoformat(sep=" ")])
        live.execute("DELETE FROM type_sentencelog WHERE id = (SELECT MAX(id) FROM type_sentencelog)")

    def run_adopt(self, *args):
        out = StringIO()
        call_command("adopt_user_data", "--from", str(self.live_path), *args, stdout=out)
        return out.getvalue()

    def live_digest(self):
        return hashlib.sha256(self.live_path.read_bytes()).hexdigest()

    def test_it_copies_user_data_and_resets_what_was_on_screen(self):
        self.live.close()
        digest = self.live_digest()
        output = self.run_adopt()

        self.assertIn("Done.", output)
        self.assertEqual(UserWord.objects.get().word_id, self.word.id)
        self.assertEqual(ReviewLog.objects.get().rating, 3)
        self.assertEqual(User.objects.filter(username="hanako").count(), 1)
        learner = Learner.objects.get()
        self.assertEqual((learner.current, learner.recent, learner.known_kanji), ({}, [], "雨"))  # kanji came across
        self.assertEqual(connection.cursor().execute("SELECT COUNT(*) FROM type_sentencelog").fetchone()[0], 1)
        # AUTOINCREMENT's high-water mark came across: the deleted sentence log's id is not handed out again.
        with connection.cursor() as cursor:
            seq = cursor.execute("SELECT seq FROM sqlite_sequence WHERE name = 'type_sentencelog'").fetchone()[0]
        self.assertEqual(seq, 2)
        # The corpus is the new one, and the live database was not touched.
        self.assertEqual(Sentence.objects.get().text, "新しい文。")
        self.assertEqual(Passage.objects.get().title, "new passage")
        self.assertEqual(self.live_digest(), digest)

    def test_dry_run_checks_and_writes_nothing(self):
        self.live.close()
        output = self.run_adopt("--dry-run")
        self.assertIn("--dry-run", output)
        self.assertIn("type_userword: 1 rows", output)
        self.assertFalse(UserWord.objects.exists())
        self.assertEqual(Learner.objects.get().known_kanji, "")
        self.assertFalse(User.objects.filter(username="hanako").exists())

    def test_a_word_id_that_means_another_word_rolls_everything_back(self):
        self.live.execute("UPDATE type_word SET reading = 'う' WHERE id = ?", [self.word.id])  # not the same word any more
        self.live.commit()
        self.live.close()
        with self.assertRaises(CommandError) as raised:
            self.run_adopt()
        self.assertIn(f"word id {self.word.id}", str(raised.exception))
        self.assertIn("あめ", str(raised.exception))
        self.assertFalse(UserWord.objects.exists())
        self.assertFalse(ReviewLog.objects.exists())
        self.assertFalse(User.objects.filter(username="hanako").exists())

    def test_a_word_the_new_database_lacks_rolls_everything_back(self):
        Word.objects.filter(id=self.word.id).delete()
        self.live.close()
        with self.assertRaises(CommandError) as raised:
            self.run_adopt()
        self.assertIn(f"word id {self.word.id}", str(raised.exception))
        self.assertFalse(UserWord.objects.exists())
        self.assertEqual(Learner.objects.get().known_kanji, "")

    def test_a_permission_id_that_means_another_permission_rolls_back(self):
        permission = connection.cursor().execute("SELECT id FROM auth_permission ORDER BY id LIMIT 1").fetchone()[0]
        self.live.execute("INSERT INTO auth_user_user_permissions (user_id, permission_id) VALUES (50, ?)", [permission])
        self.live.execute("UPDATE auth_permission SET codename = 'something_else' WHERE id = ?", [permission])
        self.live.commit()
        self.live.close()
        with self.assertRaises(CommandError) as raised:
            self.run_adopt()
        self.assertIn(f"permission id {permission}", str(raised.exception))
        self.assertFalse(User.objects.filter(username="hanako").exists())

    def test_it_refuses_its_own_database_and_missing_files(self):
        self.live.close()
        with self.assertRaises(CommandError):
            call_command("adopt_user_data", "--from", str(Path(self.folder.name) / "missing.sqlite3"))
