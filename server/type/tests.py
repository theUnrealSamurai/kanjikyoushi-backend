import importlib
import json
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .corpus import (
    furigana, jlpt_kanji, kanji_dictionary, is_joyo_only, jlpt_level, people_romaji, same_text, strip_invisible, tokenize,
)
from .dictionary import cached_dictionary, find_kana_meanings, find_meanings, index_by_kana, index_by_kanji
from . import models
from .models import WARMUP_SIGHTINGS, KanaWord, Learner, ReviewLog, Sentence, SentenceLog, UserWord, Word


SENTENCES = [
    "今日は先生と会議があります。",
    "先生は会議室で食べました。",
    "今日は雨です。",
    "先生が来た。",
    "会議は長い。",
    "田中さんは四つ食べた。",
    "テントが一つしかない。",
]


# No test may find (or download) the real dictionary in data/.
NO_DICTIONARY = override_settings(DATA_DIR=Path(tempfile.gettempdir()) / "kanjikyoushi-no-dictionary")


def build_corpus(path, contents, **options):
    path.write_text(contents, encoding="utf-8")
    with open(path.parent / "log.txt", "w") as log:
        call_command("build_corpus", str(path), stdout=log, **options)


def gloss(*texts):
    return [{"lang": "eng", "text": text} for text in texts]


def sense(*glosses, kanji=("*",), kana=("*",)):
    return {"appliesToKanji": list(kanji), "appliesToKana": list(kana), "gloss": gloss(*glosses)}


def entry(kanji, kana, *senses):
    """A JMdict-shaped entry; kanji and kana are lists of (text, common)."""
    return {
        "kanji": [{"text": text, "common": common} for text, common in kanji],
        "kana": [{"text": text, "common": common, "appliesToKanji": ["*"]} for text, common in kana],
        "sense": list(senses),
    }


JMDICT = {"words": [
    entry([("食べる", True), ("喰べる", False)], [("たべる", True)],
          sense("to eat"), sense("to live on (e.g. a salary)", "to live off", "to subsist on"),
          sense("third"), sense("fourth, left out")),
    entry([("方", True)], [("かた", True)], sense("person (honorific)")),
    entry([("方", True)], [("ほう", True)], sense("direction"), sense("side", kana=("かた",))),
    entry([("男", False)], [("おとこ", False)], sense("rare man")),
    entry([("男", True)], [("おとこ", True), ("おっこ", False)], sense("man; male")),
    entry([("雨", True), ("雫", False)], [("あま", True)], sense("rain"), sense("drop", kanji=("雫",))),
    entry([("噤む", False)], [("つぐむ", False)], sense("to shut one's mouth")),
    entry([("欠ける", True)], [("かける", True)], sense("to be chipped")),
    entry([("掛ける", True)], [("かける", True)], sense("to hang")),
    entry([("混な", False)], [("こんな", False)], sense("wrong one")),
    entry([], [("こんな", True)], sense("such; like this")),
]}


def write_dictionary(folder, name="jmdict-eng-3.0.0+20260101.json.zip"):
    path = Path(folder) / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("jmdict-eng-3.0.0.json", json.dumps(JMDICT))
    return path


@NO_DICTIONARY
class CorpusTests(TestCase):
    def test_furigana_puts_readings_over_kanji_only(self):
        self.assertEqual(furigana("食べ", "たべ"), [["食", "た"], ["べ", ""]])
        self.assertEqual(furigana("取り扱い", "とりあつかい"), [["取", "と"], ["り", ""], ["扱", "あつか"], ["い", ""]])
        self.assertEqual(furigana("今日", "きょう"), [["今日", "きょう"]])
        self.assertEqual(furigana("テント", ""), [["テント", ""]])

    def test_tokens_rebuild_the_sentence_and_name_dictionary_forms(self):
        tokens = tokenize("先生は会議室で食べました。")
        self.assertEqual("".join(t.surface for t in tokens), "先生は会議室で食べました。")
        words = {(t.word, t.word_reading) for t in tokens if t.word}
        self.assertIn(("食べる", "たべる"), words)
        self.assertIn(("先生", "せんせい"), words)

    def test_names_numbers_and_counters_are_not_graded(self):
        tokens = tokenize("田中さんは四つ食べた。")
        self.assertEqual([t.word for t in tokens if t.word], ["食べる"])
        self.assertEqual(next(t.reading for t in tokens if t.surface == "田中"), "たなか")
        self.assertEqual([t.word for t in tokenize("六日ほど三本の木を見た") if t.word], ["木", "見る"])

    def test_one_and_two_people_read_hitori_and_futari(self):
        def readings(sentence):
            return {t.surface: t.reading for t in tokenize(sentence) if t.reading}
        self.assertEqual(readings("2人きりで立つ。")["2人"], "ふたり")
        self.assertEqual(readings("1人1人に聞く。")["1人"], "ひとり")
        self.assertEqual(readings("12人で行く。")["人"], "にん")  # じゅうににん
        self.assertEqual(readings("3人で行く。")["人"], "にん")
        self.assertEqual(readings("1人前の料理。")["人"], "にん")  # いちにんまえ, a serving
        self.assertIsNone(next(t.word for t in tokenize("2人で行く。") if t.surface == "2人"))  # numbers aren't graded
        self.assertEqual(people_romaji("2nin kiri de tatsu."), "Futari kiri de tatsu.")
        self.assertEqual(people_romaji("Itta. 1nin de iku."), "Itta. Hitori de iku.")
        self.assertEqual(people_romaji("Kare wa 2nin wo mita."), "Kare wa futari wo mita.")
        self.assertEqual(people_romaji("12nin to 1nin mae."), "12nin to 1nin mae.")

    def test_invisible_characters_are_dropped(self):
        self.assertEqual(strip_invisible("\u202c2人きり\u200b。\ufeff"), "2人きり。")
        self.assertEqual(strip_invisible("言語\u3000です"), "言語\u3000です")  # spaces can be typed

    def test_migration_fixes_sentences_built_before(self):
        fix_sentences = importlib.import_module("type.migrations.0008_typable_text").fix_sentences
        hidden = Sentence.objects.create(text="\u202c2人きり。", romaji="? 2nin kiri.", tokens=[
            ["\u202c", "", None, None], ["2", "", None, None], ["人", "にん", None, None], ["きり。", "", None, None]])
        copy = Sentence.objects.create(text="\u202c雨です。", tokens=[["\u202c", "", None, None], ["雨", "あめ", None, None], ["です。", "", None, None]])
        Sentence.objects.create(text="雨です。", tokens=[["雨", "あめ", None, None], ["です。", "", None, None]])
        crowd = Sentence.objects.create(text="12人で。", romaji="12nin de.", tokens=[
            ["12", "", None, None], ["人", "にん", None, None], ["で。", "", None, None]])

        fix_sentences(apps, None)
        hidden.refresh_from_db()
        crowd.refresh_from_db()
        self.assertEqual(hidden.text, "2人きり。")
        self.assertEqual(hidden.tokens, [["2人", "ふたり", None, None], ["きり。", "", None, None]])
        self.assertEqual(hidden.romaji, "Futari kiri.")
        self.assertFalse(Sentence.objects.filter(id=copy.id).exists())  # would be a second 雨です。
        self.assertEqual((crowd.tokens[1], crowd.romaji), (["人", "にん", None, None], "12nin de."))

    def test_kana_only_content_words_can_be_looked_up(self):
        tokens = {t.surface: t.kana_word for t in tokenize("その言葉に思わず僕は口をつぐんでしまった。")}
        self.assertEqual(tokens["つぐん"], ("つぐむ", "つぐむ", "噤む"))
        self.assertIsNone(tokens["に"])  # particles are grammar, not words to look up
        self.assertIsNone(tokens["言葉"])  # kanji words are learned instead

    def test_typed_text_comparison(self):
        self.assertTrue(same_text("今日は雨です", "今日は雨です。"))
        self.assertTrue(same_text("４ドル", "4ドル"))
        self.assertFalse(same_text("今日は飴です。", "今日は雨です。"))

    def test_jlpt_level_is_the_hardest_kanji(self):
        self.assertEqual(jlpt_level("食べる"), 5)
        self.assertEqual(jlpt_level("部屋"), 3)  # 屋 is N4 but 部 is N3
        self.assertEqual(jlpt_level("剣"), 1)
        self.assertEqual(jlpt_level("侏儒"), 0)  # beyond N1
        self.assertEqual(jlpt_level("人々"), 5)  # 々 isn't a kanji to learn

    def test_build_corpus_loads_sentences_and_counts_words(self):
        rows = ["index\ttext\ttranslation"] + [f"{i}\t{text}\tEnglish {i}" for i, text in enumerate(SENTENCES)]
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.tsv", "\n".join(rows) + "\n")

        self.assertEqual(Sentence.objects.count(), 6)  # テントが一つしかない has nothing to learn
        self.assertEqual(Sentence.objects.get(text="今日は雨です。").translation, "English 2")
        self.assertEqual(Word.objects.get(text="先生").frequency, 3)
        self.assertEqual(Word.objects.get(text="先生").jlpt, 5)


@NO_DICTIONARY
class FilterTests(TestCase):
    def load(self, rows, **options):
        lines = ["id\ttext\tscore"] + [f"{i}\t{text}\t{score}" for i, (text, score) in enumerate(rows)]
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.tsv", "\n".join(lines) + "\n", **options)
        return set(Sentence.objects.values_list("text", flat=True))

    def test_length_limits(self):
        rows = [("先生が来た。", 1), ("今日は先生と会議があります。", 1)]
        self.assertEqual(self.load(rows, min_length=8), {"今日は先生と会議があります。"})
        Sentence.objects.all().delete()
        self.assertEqual(self.load(rows, max_length=8), {"先生が来た。"})

    def test_min_score_skips_low_missing_and_invalid_scores(self):
        rows = [("先生が来た。", 1.5), ("会議は長い。", 0.5), ("今日は雨です。", ""), ("先生は食べた。", "nan?")]
        self.assertEqual(self.load(rows, min_score=1.0, score_column="score"), {"先生が来た。"})
        Sentence.objects.all().delete()
        self.assertEqual(len(self.load(rows)), 4)  # no --min-score: scores are ignored

    def test_joyo_only_skips_other_kanji(self):
        self.assertTrue(is_joyo_only("先生は食べた。々"))
        self.assertFalse(is_joyo_only("薔薇が咲いた。"))
        self.assertTrue(is_joyo_only("憂鬱"))  # added to the jōyō list in 2010
        self.assertFalse(is_joyo_only("亞"))  # an old form, not jōyō
        rows = [("先生が来た。", 1), ("薔薇が咲いた。", 1)]
        self.assertEqual(self.load(rows, joyo_only=True), {"先生が来た。"})
        Sentence.objects.all().delete()
        self.assertEqual(len(self.load(rows)), 2)

    def test_sample_is_seeded_and_spread_over_the_file(self):
        rows = [(f"先生が{n}人来た。", 1) for n in range(100)]
        first = self.load(rows, sample=0.3, seed=7)
        self.assertTrue(10 < len(first) < 60)
        self.assertTrue(any(f"先生が{n}人来た。" in first for n in range(50, 100)))  # not just the first rows

        Sentence.objects.all().delete()
        self.assertEqual(self.load(rows, sample=0.3, seed=7), first)
        Sentence.objects.all().delete()
        self.assertNotEqual(self.load(rows, sample=0.3, seed=8), first)

    def test_max_katakana_skips_names_and_loanwords(self):
        rows = [("先生が来た。", 1), ("アリスが来た。", 1), ("ラーメンの先生。", 1), ("一ヶ月の先生。", 1)]
        self.assertEqual(self.load(rows, max_katakana=0), {"先生が来た。", "一ヶ月の先生。"})  # ヶ is a counter
        Sentence.objects.all().delete()
        self.assertEqual(len(self.load(rows, max_katakana=3)), 4)  # ラーメン is 3 letters: ー doesn't count

    def test_invisible_characters_are_dropped_from_the_text(self):
        self.assertEqual(self.load([("\u202c先生が来た。", 1)]), {"先生が来た。"})

    def test_sample_must_be_a_fraction(self):
        with self.assertRaises(CommandError):
            self.load([("先生が来た。", 1)], sample=30)


@NO_DICTIONARY
class DictionaryTests(TestCase):
    def index(self, text):
        return index_by_kanji(JMDICT["words"], {text})

    def test_matches_text_and_reading_and_caps_senses(self):
        self.assertEqual(find_meanings(self.index("食べる"), "食べる", "たべる"), [
            "to eat", "to live on (e.g. a salary); to live off; to subsist on", "third",
        ])

    def test_the_reading_picks_between_entries_with_the_same_text(self):
        self.assertEqual(find_meanings(self.index("方"), "方", "かた"), ["person (honorific)"])
        self.assertEqual(find_meanings(self.index("方"), "方", "ほう"), ["direction"])  # the かた-only sense is skipped

    def test_common_entry_wins(self):
        self.assertEqual(find_meanings(self.index("男"), "男", "おとこ"), ["man; male"])

    def test_falls_back_to_text_alone_and_respects_applies_to_kanji(self):
        self.assertEqual(find_meanings(self.index("雨"), "雨", "あめ"), ["rain"])
        self.assertEqual(find_meanings(self.index("雫"), "雫", "しずく"), ["rain", "drop"])
        self.assertEqual(find_meanings(self.index("雨"), "雨", "あま"), ["rain"])  # drop is for 雫 only
        self.assertEqual(find_meanings(self.index("猫"), "猫", "ねこ"), [])

    def test_kana_words_use_the_lemma_spelling_then_kana_entries(self):
        entries = JMDICT["words"]
        kanji_index = index_by_kanji(entries, {"噤む", "掛ける", "こんな"})
        kana_index = index_by_kana(entries, {"つぐむ", "かける", "こんな"})
        find = lambda text, lemma: find_kana_meanings(kanji_index, kana_index, text, text, lemma)
        self.assertEqual(find("つぐむ", "噤む"), ["to shut one's mouth"])
        self.assertEqual(find("かける", "掛ける"), ["to hang"])  # the lemma picks 掛ける, not 欠ける
        self.assertEqual(find("こんな", "こんな"), ["such; like this"])  # no kanji lemma: the kana-only entry wins

    def test_load_dictionary_fills_every_word_from_a_zip_or_json(self):
        Word.objects.create(text="食べる", reading="たべる")
        Word.objects.create(text="猫", reading="ねこ")
        out = StringIO()
        with tempfile.TemporaryDirectory() as folder:
            call_command("load_dictionary", file=write_dictionary(folder), stdout=out)
            self.assertIn("1 of 2 words and 0 of 0 kana words have meanings", out.getvalue())
            self.assertEqual(Word.objects.get(text="食べる").meanings[0], "to eat")
            self.assertEqual(Word.objects.get(text="猫").meanings, [])

            Word.objects.update(meanings=[])
            plain = Path(folder) / "jmdict.json"
            plain.write_text(json.dumps(JMDICT), encoding="utf-8")
            call_command("load_dictionary", file=plain, stdout=out)
            self.assertEqual(Word.objects.get(text="食べる").meanings[0], "to eat")

    def test_build_corpus_adds_meanings_only_when_the_dictionary_is_cached(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(DATA_DIR=Path(folder) / "data"):
            out = StringIO()
            path = Path(folder) / "corpus.txt"
            path.write_text("先生は食べた。\n", encoding="utf-8")
            call_command("build_corpus", str(path), stdout=out)
            self.assertIn("load_dictionary", out.getvalue())
            self.assertEqual(Word.objects.get(text="食べる").meanings, [])

            (Path(folder) / "data").mkdir()
            write_dictionary(Path(folder) / "data")
            write_dictionary(Path(folder) / "data", "jmdict-eng-common-3.0.0+20260101.json.zip")
            self.assertEqual(cached_dictionary().name, "jmdict-eng-3.0.0+20260101.json.zip")
            path.write_text("先生は食べた。\n会議で食べる。\n", encoding="utf-8")
            call_command("build_corpus", str(path), stdout=out)
            self.assertEqual(Word.objects.get(text="食べる").meanings[0], "to eat")


@NO_DICTIONARY
class LearningFlowTests(TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.txt", "\n".join(SENTENCES))
        self.user = User.objects.create_user("taro", "taro@example.com", "a-long-password")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def onboard(self, kanji_list=""):
        self.client.post("/type/onboard", {"kanji_list": kanji_list}, format="json")
        return Learner.objects.get(user=self.user)

    def next(self):
        return self.client.post("/type/next").json()

    def submit(self, payload, **extra):
        body = {"sentence_id": payload["sentence_id"], "typed": payload["text"], **extra}
        return self.client.post("/type/submit", body, format="json")

    def word_ids(self, payload, role):
        return [t["word"] for t in payload["tokens"] if t["role"] == role]

    def test_must_onboard_first(self):
        self.assertEqual(self.client.post("/type/next").status_code, 409)

    def test_known_kanji_words_are_never_taught(self):
        learner = self.onboard("雨")
        self.assertEqual(learner.words.get(word__text="雨").status, UserWord.KNOWN)

    def test_repeat_marks_dont_make_a_word_new(self):
        Word.objects.create(text="人々", reading="ひとびと", jlpt=5, list_level=5, frequency=1)
        learner = self.onboard("人日時")
        self.assertEqual(learner.words.get(word__text="人々").status, UserWord.KNOWN)  # 々 isn't a kanji to know
        # Words met later (added to the corpus, or left unmet by an older onboarding) become known on the way.
        days = Word.objects.create(text="日々", reading="ひび", jlpt=5, list_level=5, frequency=99)  # first in the learning order
        self.assertNotIn(days.id, learner._new_word_window())
        self.assertEqual(learner.words.get(word=days).status, UserWord.KNOWN)
        times = Word.objects.create(text="時々", reading="ときどき", jlpt=5, list_level=5, frequency=99)
        learner._fill_learning_pool()
        self.assertEqual(learner.words.get(word=times).status, UserWord.KNOWN)

    def test_new_words_come_in_list_order_then_by_frequency_and_only_from_the_list(self):
        learner = self.onboard()
        learner.words.all().delete()
        Word.objects.update(frequency=0)  # leave only the words below in play
        for text, reading, level, frequency in [
            ("剣", "けん", 1, 900), ("剣士", "けんし", 1, 100), ("魔物", "まもの", None, 999), ("侏儒", "しゅじゅ", None, 999),
            ("部屋", "へや", 5, 50), ("頭", "あたま", 4, 70), ("持つ", "もつ", 4, 20), ("犬", "いぬ", 5, 10),
        ]:
            Word.objects.create(text=text, reading=reading, jlpt=3, list_level=level, frequency=frequency)

        with patch.object(models, "LEARNING_POOL", 5):
            learner._fill_learning_pool()
        pool = set(learner.words.filter(status=UserWord.LEARNING).values_list("word__text", flat=True))
        self.assertEqual(pool, {"部屋", "犬", "頭", "持つ", "剣"})  # 剣 beats 剣士 on frequency
        learner.words.all().delete()
        for _ in range(3):
            learner._fill_learning_pool()
            learner.words.filter(status=UserWord.LEARNING).update(status=UserWord.REVIEW)
        self.assertFalse(learner.words.filter(word__text__in=("魔物", "侏儒")).exists())  # not on the list: never taught

    def test_new_words_warm_up_then_get_reviewed(self):
        self.onboard()
        payload = self.next()
        self.assertTrue(self.word_ids(payload, "test"))

        for _ in range(30):
            response = self.submit(payload, revealed=self.word_ids(payload, "test"))  # every test needs Tab: full warm-up
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            if payload.get("done"):
                break
        learner = Learner.objects.get(user=self.user)
        self.assertTrue(learner.words.filter(status=UserWord.REVIEW, sightings__gte=WARMUP_SIGHTINGS).exists())

    def test_a_new_word_typed_without_help_skips_the_warm_up(self):
        learner = self.onboard()
        payload = self.next()
        tested = list(dict.fromkeys(self.word_ids(payload, "test")))
        self.assertTrue(tested)
        self.assertEqual(self.word_ids(payload, "learning"), [])  # every new word starts with its test
        self.assertEqual(self.submit(payload, revealed=tested[1:]).status_code, 200)

        passed = learner.words.get(word_id=tested[0])
        self.assertEqual((passed.status, passed.sightings), (UserWord.REVIEW, 1))
        self.assertIsNotNone(passed.card)
        self.assertIsNotNone(passed.learned_at)
        for word_id in tested[1:]:  # Tab on the test: the warm-up goes on
            self.assertEqual(learner.words.get(word_id=word_id).sightings, 1)

    def test_needing_help_on_the_test_means_the_warm_up_with_furigana(self):
        learner = self.onboard()
        payload = self.next()
        word_id = self.word_ids(payload, "test")[0]
        self.submit(payload, skipped=[word_id])  # ⌥Tab showed how to type it
        self.assertEqual((learner.words.get(word_id=word_id).status, learner.words.get(word_id=word_id).sightings),
                         (UserWord.LEARNING, 1))

        sentence = Sentence.objects.filter(words=word_id).first()
        learner.refresh_from_db()
        learner.current = {"sentence": sentence.id, "review": [], "learning": [word_id]}
        roles = {t["word"]: t["role"] for t in learner.sentence_payload(sentence, {})["tokens"]}
        self.assertEqual(roles[word_id], "learning")  # furigana shown from now on

    def test_review_ratings_come_from_tab_only_never_typos(self):
        learner = self.onboard()
        learner.words.all().delete()
        past = timezone.now() - timedelta(days=1)
        teacher, meeting = Word.objects.get(text="先生"), Word.objects.get(text="会議")
        for word in (teacher, meeting):
            UserWord.objects.create(learner=learner, word=word, status=UserWord.REVIEW, due=past)
        # Mark everything else known, so the sentence picked has to be one with both due words.
        UserWord.objects.bulk_create([
            UserWord(learner=learner, word=word, status=UserWord.KNOWN)
            for word in Word.objects.exclude(id__in=(teacher.id, meeting.id))
        ])

        payload = self.next()
        self.assertIn(payload["text"], ("今日は先生と会議があります。", "先生は会議室で食べました。"))
        self.assertEqual(sorted(self.word_ids(payload, "review")), sorted([teacher.id, meeting.id]))
        self.assertEqual(set(payload["words"]), {str(t["word"]) for t in payload["tokens"] if t["word"]})

        self.assertEqual(self.submit(payload, revealed=[teacher.id], missed=[meeting.id]).status_code, 200)  # missed: ignored
        cards = {w.word_id: w.card for w in learner.words.filter(status=UserWord.REVIEW)}
        self.assertGreater(cards[meeting.id]["stability"], cards[teacher.id]["stability"])  # typo ignored: Good beats Hard

    def test_payload_has_dictionary_data_for_learning_and_review_words(self):
        learner = self.onboard()
        learner.words.all().delete()
        Word.objects.filter(text="先生").update(meanings=["teacher; master"])
        teacher, meeting, today = (Word.objects.get(text=t) for t in ("先生", "会議", "今日"))
        past = timezone.now() - timedelta(days=1)
        UserWord.objects.create(learner=learner, word=teacher, status=UserWord.REVIEW, due=past)
        UserWord.objects.create(learner=learner, word=meeting, status=UserWord.LEARNING)
        UserWord.objects.create(learner=learner, word=today, status=UserWord.KNOWN)
        UserWord.objects.bulk_create([
            UserWord(learner=learner, word=word, status=UserWord.KNOWN)
            for word in Word.objects.exclude(id__in=(teacher.id, meeting.id, today.id))
        ])

        payload = self.next()
        self.assertNotIn("kanji", payload)
        self.assertEqual(self.word_ids(payload, "review"), [teacher.id])
        self.assertEqual(self.word_ids(payload, "test"), [meeting.id])  # a new word's first sighting is its test
        with_cards = {str(t["word"]) for t in payload["tokens"] if t["word"]}
        self.assertEqual(set(payload["words"]), with_cards)  # string keys; every word, so Tab can open any of them
        self.assertTrue({str(teacher.id), str(meeting.id)} <= set(payload["words"]))
        self.assertEqual(payload["words"][str(teacher.id)]["text"], "先生")
        self.assertEqual(payload["words"][str(teacher.id)]["reading"], "せんせい")
        self.assertEqual(payload["words"][str(teacher.id)]["meanings"], ["teacher; master"])
        self.assertEqual(payload["words"][str(meeting.id)]["meanings"], [])
        self.assertEqual([k["kanji"] for k in payload["words"][str(teacher.id)]["kanji"]], ["先", "生"])
        self.assertEqual(set(payload["words"][str(meeting.id)]["kanji"][0]),
                         {"kanji", "on_readings", "kun_readings", "meanings"})
        self.assertEqual(
            set(payload),
            {"sentence_id", "text", "translation", "romaji", "tokens", "words", "kana_words", "extra_words", "stats", "passage"},
        )
        self.assertIsNone(payload["passage"])  # sentence mode

    def test_skipped_review_words_are_not_graded(self):
        learner = self.onboard()
        learner.words.all().delete()
        past = timezone.now() - timedelta(days=1)
        teacher, meeting = Word.objects.get(text="先生"), Word.objects.get(text="会議")
        for word in (teacher, meeting):
            UserWord.objects.create(learner=learner, word=word, status=UserWord.REVIEW, due=past)
        UserWord.objects.bulk_create([
            UserWord(learner=learner, word=word, status=UserWord.KNOWN)
            for word in Word.objects.exclude(id__in=(teacher.id, meeting.id))
        ])
        payload = self.next()
        self.assertEqual(sorted(self.word_ids(payload, "review")), sorted([teacher.id, meeting.id]))

        # Asked how to type both (and typed one wrong); revealed the reading of one of them first.
        response = self.submit(payload, skipped=[teacher.id, meeting.id], revealed=[meeting.id])
        self.assertEqual(response.status_code, 200)
        skipped = learner.words.get(word=teacher)
        self.assertIsNone(skipped.card)  # untouched: no FSRS review at all
        self.assertEqual(skipped.due, past)  # still due, so it comes back
        self.assertIsNotNone(learner.words.get(word=meeting).card)  # revealed first: Hard stands

    def test_looking_up_a_word_you_were_assumed_to_know(self):
        learner = self.onboard()
        learner.words.all().delete()
        teacher, meeting = Word.objects.get(text="先生"), Word.objects.get(text="会議")
        later = timezone.now() + timedelta(days=10)
        UserWord.objects.create(learner=learner, word=teacher, status=UserWord.REVIEW, due=timezone.now() - timedelta(days=1))
        UserWord.objects.create(learner=learner, word=meeting, status=UserWord.REVIEW, due=later)  # learned, not due
        UserWord.objects.bulk_create([
            UserWord(learner=learner, word=word, status=UserWord.KNOWN)
            for word in Word.objects.exclude(id__in=(teacher.id, meeting.id))
        ])
        # Steer the picker to a sentence with both words: ones with only 先生 count as just typed.
        learner.recent = list(Sentence.objects.filter(words=teacher).exclude(words=meeting).values_list("id", flat=True))
        learner.save()

        payload = self.next()
        roles = {t["word"]: t["role"] for t in payload["tokens"] if t["word"]}
        self.assertEqual(roles[meeting.id], "known")
        known = next(word_id for word_id, role in roles.items() if role == "known" and word_id != meeting.id)
        elsewhere = Word.objects.exclude(id__in=roles).first()

        self.assertEqual(self.submit(payload, revealed=[meeting.id, known, elsewhere.id]).status_code, 200)
        self.assertEqual(learner.words.get(word_id=known).status, UserWord.LEARNING)  # back to the warm-up
        self.assertEqual(learner.words.get(word_id=known).sightings, 1)  # the look-up was its first sighting: no test
        self.assertLess(learner.words.get(word_id=meeting.id).due, later)  # reviewed as Hard: comes back sooner
        self.assertEqual(learner.words.get(word_id=elsewhere.id).status, UserWord.KNOWN)  # not in the sentence

    def test_kana_words_come_with_meanings_but_are_never_learned(self):
        learner = self.onboard()
        sentence = Sentence.objects.get(text="今日は先生と会議があります。")
        kana = KanaWord.objects.get(text="ある")
        kana.meanings = ["to be; to exist"]
        kana.save()
        learner.current = {"sentence": sentence.id, "review": [], "learning": []}

        payload = learner.sentence_payload(sentence, {})
        lookups = {t["text"]: t["lookup"] for t in payload["tokens"]}
        self.assertEqual(lookups["あり"], kana.id)
        self.assertIsNone(lookups["は"])
        self.assertEqual(payload["kana_words"], {
            str(kana.id): {"text": "ある", "reading": "ある", "meanings": ["to be; to exist"], "kanji": []},
        })
        self.assertFalse(Word.objects.filter(text="ある").exists())  # never a learnable, gradable word

    def test_names_and_numbers_get_a_card_for_tab_but_are_never_graded(self):
        learner = self.onboard()
        trees = Word.objects.create(text="木々", reading="きぎ")
        sentence = Sentence.objects.create(text="木々が一振りに田中さん。", tokens=[
            ["木々", "きぎ", trees.id, None], ["が", "", None, None], ["一", "いち", None, None],
            ["振り", "ふり", None, None], ["に", "", None, None], ["田中", "たなか", None, None],
            ["さん", "", None, None], ["。", "", None, None],
        ])
        learner.current = {"sentence": sentence.id, "review": [], "learning": []}

        payload = learner.sentence_payload(sentence, {})
        self.assertEqual([t["extra"] for t in payload["tokens"]], [None, None, 2, 2, None, 5, None, None])
        self.assertEqual({key: (card["text"], card["reading"]) for key, card in payload["extra_words"].items()},
                         {"2": ("一振り", "いちふり"), "5": ("田中", "たなか")})
        self.assertEqual(payload["extra_words"]["2"]["meanings"], [])
        self.assertEqual([k["kanji"] for k in payload["extra_words"]["2"]["kanji"]], ["一", "振"])
        self.assertEqual(set(payload["words"]), {str(trees.id)})  # nothing to grade beyond the real word

    def test_submit_rejects_wrong_text_and_stale_sentences(self):
        self.onboard()
        payload = self.next()
        wrong = self.client.post("/type/submit", {"sentence_id": payload["sentence_id"], "typed": "ちがう"}, format="json")
        self.assertEqual(wrong.status_code, 400)
        self.assertEqual(self.submit(payload).status_code, 200)
        self.assertEqual(self.submit(payload).status_code, 400)  # already graded

    def test_reloading_returns_the_sentence_still_on_screen(self):
        self.onboard()
        first = self.next()
        self.assertEqual(self.next()["sentence_id"], first["sentence_id"])
        self.assertEqual(self.client.post("/type/next").json()["tokens"], first["tokens"])

    def give_due(self, learner, days_overdue):
        """Reset progress to just these review words, each due that many days ago: {text: days}."""
        learner.words.all().delete()
        now = timezone.now()
        for text, days in days_overdue.items():
            UserWord.objects.create(
                learner=learner, word=Word.objects.get(text=text), status=UserWord.REVIEW, due=now - timedelta(days=days)
            )

    def set_mode(self, learn_new):
        return self.client.post("/type/mode", {"learn_new": learn_new}, format="json")

    def test_a_sentence_grades_every_due_word_in_it(self):
        learner = self.onboard()
        self.give_due(learner, {"今日": 1, "先生": 2, "会議": 3})
        payload = self.set_mode(False).json()
        self.assertEqual(payload["text"], "今日は先生と会議があります。")  # the one sentence with all three
        due = set(Word.objects.filter(text__in=("今日", "先生", "会議")).values_list("id", flat=True))
        self.assertEqual(set(self.word_ids(payload, "review")), due)
        self.submit(payload)
        self.assertEqual(set(ReviewLog.objects.filter(learner=learner).values_list("word_id", flat=True)), due)

    def test_a_sentence_grades_at_most_two_new_words(self):
        learner = self.onboard()
        learner.words.all().delete()
        UserWord.objects.bulk_create(
            UserWord(learner=learner, word=word, status=UserWord.LEARNING if word.text in ("今日", "先生", "会議") else UserWord.KNOWN)
            for word in Word.objects.all()
        )
        payload = self.next()
        self.assertEqual(len(set(self.word_ids(payload, "test"))), 2)  # a sentence with two of them wins
        self.submit(payload)
        self.assertEqual(learner.words.filter(status=UserWord.REVIEW).count(), 2)  # both passed their test

    def test_migration_drops_progress_on_words_off_the_list(self):
        teach_the_list = importlib.import_module("type.migrations.0010_teach_the_list").teach_the_list
        learner = self.onboard()
        monster = Word.objects.create(text="魔物", reading="まもの", frequency=5)
        teacher = Word.objects.get(text="先生")
        UserWord.objects.create(learner=learner, word=monster, status=UserWord.REVIEW)
        UserWord.objects.update_or_create(learner=learner, word=teacher, defaults={"status": UserWord.REVIEW})
        learner.current = {"sentence": Sentence.objects.first().id, "review": [monster.id], "learning": []}
        learner.save()

        teach_the_list(apps, None)
        learner.refresh_from_db()
        self.assertEqual((Word.objects.get(id=monster.id).list_level, Word.objects.get(id=teacher.id).list_level), (None, 5))
        self.assertFalse(learner.words.filter(word=monster).exists())  # never reviewed or taught again
        self.assertTrue(learner.words.filter(word=teacher).exists())
        self.assertEqual(learner.current, {})

    def test_review_mode_brings_in_no_new_words_and_grades_only_due_words(self):
        learner = self.onboard()
        self.give_due(learner, {"先生": 1})
        teacher, meeting = Word.objects.get(text="先生"), Word.objects.get(text="会議")
        UserWord.objects.create(learner=learner, word=meeting, status=UserWord.LEARNING)  # being warmed up
        # Steer the picker to the sentence with both words.
        learner.recent = list(Sentence.objects.filter(words=teacher).exclude(words=meeting).values_list("id", flat=True))
        learner.save()

        response = self.set_mode(False)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["text"], "今日は先生と会議があります。")
        self.assertEqual(self.word_ids(payload, "review"), [teacher.id])
        self.assertEqual(self.word_ids(payload, "learning"), [])
        self.assertEqual(self.word_ids(payload, "unseen").count(meeting.id), 1)  # a warm-up word left alone: furigana shown
        self.assertEqual(learner.words.count(), 2)  # no new words were added

        done = self.submit(payload).json()
        self.assertTrue(done["done"])  # nothing else is due
        self.assertIsNotNone(learner.words.get(word=teacher).card)
        self.assertEqual(learner.words.get(word=meeting).sightings, 0)
        self.assertEqual(learner.words.get(word=meeting).status, UserWord.LEARNING)

    def test_review_mode_with_nothing_due_is_done_and_learning_resumes_when_switched_back(self):
        self.onboard()
        done = self.set_mode(False).json()
        self.assertTrue(done["done"])
        self.assertFalse(done["stats"]["learn_new"])
        self.assertEqual(self.next(), done)
        self.assertEqual(self.client.post("/type/due").json()["total_words"], 0)

        payload = self.set_mode(True).json()
        self.assertTrue(payload["stats"]["learn_new"])
        self.assertTrue(self.word_ids(payload, "test"))

    def test_switching_mode_drops_the_pending_sentence(self):
        learner = self.onboard()
        self.next()
        self.assertTrue(Learner.objects.get(user=self.user).current)
        self.set_mode(False)
        learner.refresh_from_db()
        self.assertEqual(learner.current, {})
        self.assertFalse(learner.learn_new)

    def test_mode_needs_a_real_boolean_and_onboarding(self):
        learner = self.onboard()
        for body in ({}, {"learn_new": "false"}, {"learn_new": 0}, {"learn_new": None}):
            response = self.client.post("/type/mode", body, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json(), {"error": "Expected 'learn_new': true or false."})
        learner.refresh_from_db()
        self.assertTrue(learner.learn_new)
        self.client.force_authenticate(User.objects.create_user("jiro", password="a-long-password"))
        self.assertEqual(self.set_mode(False).status_code, 409)  # not onboarded

    def test_due_list_has_most_overdue_words_first_and_distinct_kanji_with_counts(self):
        learner = self.onboard()
        Word.objects.create(text="学生", reading="がくせい", frequency=1)  # shares 生 with 先生
        self.give_due(learner, {"食べる": 1, "学生": 2, "先生": 3, "室": 4})
        UserWord.objects.create(
            learner=learner, word=Word.objects.get(text="雨"), status=UserWord.REVIEW, due=timezone.now() + timedelta(days=5)
        )  # learned but not due
        Word.objects.filter(text="食べる").update(meanings=["to eat"])

        data = self.client.post("/type/due").json()
        self.assertEqual([w["text"] for w in data["words"]], ["室", "先生", "学生", "食べる"])
        self.assertEqual(set(data["words"][3]), {"id", "text", "reading", "meanings", "due"})
        self.assertEqual(data["words"][3]["id"], Word.objects.get(text="食べる").id)
        self.assertEqual(data["words"][3]["reading"], "たべる")
        self.assertEqual(data["words"][3]["meanings"], ["to eat"])
        self.assertEqual(data["words"][0]["meanings"], [])
        self.assertEqual([(k["kanji"], k["words"]) for k in data["kanji"]],
                         [("室", 1), ("先", 1), ("生", 2), ("学", 1), ("食", 1)])
        self.assertEqual(data["kanji"][0]["meanings"], kanji_dictionary()["室"]["meanings"])
        self.assertEqual((data["total_words"], data["total_kanji"]), (4, 5))

        with patch.object(models, "DUE_LIST_LIMIT", 2):  # the cap shortens the word list only
            capped = self.client.post("/type/due").json()
        self.assertEqual([w["text"] for w in capped["words"]], ["室", "先生"])
        self.assertEqual(capped["kanji"], data["kanji"])
        self.assertEqual((capped["total_words"], capped["total_kanji"]), (4, 5))

    def test_stats_count_due_words_and_kanji(self):
        learner = self.onboard()
        self.give_due(learner, {"会議": 2, "先生": 1, "雨": 1})
        stats = learner.stats()
        self.assertEqual((stats["due"], stats["due_kanji"], stats["learn_new"]), (3, 5, True))  # 会 議 先 生 雨
        self.assertEqual(self.client.post("/type/due").json()["total_kanji"], stats["due_kanji"])

    def at(self, *args):
        """A UTC moment: at(2026, 3, 10, 3) is 2026-03-10 03:00 UTC."""
        return datetime(*args, tzinfo=dt_timezone.utc)

    def warm_up(self, learner, *texts):
        """Reset progress: these words are one sighting from finishing the warm-up, every other word is known."""
        learner.words.all().delete()
        UserWord.objects.bulk_create(
            UserWord(learner=learner, word=word, status=UserWord.LEARNING if word.text in texts else UserWord.KNOWN,
                     sightings=WARMUP_SIGHTINGS - 1)
            for word in Word.objects.all()
        )

    def test_every_sentence_has_a_new_word_while_learning_is_on(self):
        learner = self.onboard()
        self.give_due(learner, {"先生": 1, "会議": 1, "食べる": 1})
        served = 0
        for _ in range(8):
            payload = self.next()
            if payload.get("done"):
                break
            self.assertTrue(self.word_ids(payload, "learning") + self.word_ids(payload, "test"), payload["text"])
            self.assertEqual(self.submit(payload).status_code, 200)
            served += 1
        self.assertGreaterEqual(served, 3)

    def test_learned_at_is_set_once_when_the_warm_up_finishes(self):
        learner = self.onboard()
        self.warm_up(learner, "先生")
        first, later = self.at(2026, 3, 10, 3), self.at(2026, 3, 20, 3)
        with patch.object(timezone, "now", return_value=first):
            self.submit(self.next())
        teacher = learner.words.get(word__text="先生")
        self.assertEqual((teacher.status, teacher.learned_at), (UserWord.REVIEW, first))
        self.assertFalse(ReviewLog.objects.exists())  # the graduation review isn't logged
        self.assertEqual(SentenceLog.objects.filter(learner=learner, at=first).count(), 1)

        teacher.status, teacher.sightings = UserWord.LEARNING, WARMUP_SIGHTINGS - 1  # sent back, then graduates again
        teacher.save()
        learner.refresh_from_db()
        with patch.object(timezone, "now", return_value=later):
            self.submit(self.next())
        self.assertEqual(learner.words.get(word__text="先生").learned_at, first)
        self.assertEqual(SentenceLog.objects.filter(learner=learner).count(), 2)

    def test_reviews_are_logged_for_graded_due_words_and_looked_up_learned_words(self):
        learner = self.onboard()
        learner.words.all().delete()
        teacher, meeting = Word.objects.get(text="先生"), Word.objects.get(text="会議")
        UserWord.objects.create(learner=learner, word=teacher, status=UserWord.REVIEW, due=timezone.now() - timedelta(days=1))
        UserWord.objects.create(learner=learner, word=meeting, status=UserWord.REVIEW, due=timezone.now() + timedelta(days=10))
        UserWord.objects.bulk_create([
            UserWord(learner=learner, word=word, status=UserWord.KNOWN)
            for word in Word.objects.exclude(id__in=(teacher.id, meeting.id))
        ])
        learner.recent = list(Sentence.objects.filter(words=teacher).exclude(words=meeting).values_list("id", flat=True))
        learner.save()

        self.submit(self.next(), revealed=[meeting.id])  # 先生 is due and graded; 会議 is learned, looked up
        ratings = dict(ReviewLog.objects.filter(learner=learner).values_list("word_id", "rating"))
        self.assertEqual(ratings, {teacher.id: 3, meeting.id: 2})  # Good, Hard
        self.assertEqual(SentenceLog.objects.filter(learner=learner).count(), 1)

    def test_onboarding_again_clears_the_history(self):
        learner = self.onboard()
        ReviewLog.objects.create(learner=learner, word=Word.objects.first(), rating=3, at=timezone.now())
        SentenceLog.objects.create(learner=learner, at=timezone.now())
        self.onboard()
        self.assertEqual((ReviewLog.objects.count(), SentenceLog.objects.count()), (0, 0))

    def test_learned_today_uses_the_timezone_from_the_header(self):
        learner = self.onboard()
        UserWord.objects.update_or_create(
            learner=learner, word=Word.objects.get(text="雨"),
            defaults={"status": UserWord.REVIEW, "due": self.at(2026, 4, 1), "learned_at": self.at(2026, 3, 10, 14)},
        )  # 14:00 UTC is 23:00 on the 10th in Tokyo
        with patch.object(timezone, "now", return_value=self.at(2026, 3, 10, 16, 30)):  # the 11th, 01:30 in Tokyo
            utc = self.next()["stats"]["learned_today"]
            tokyo = self.client.post("/type/next", HTTP_X_TIMEZONE="Asia/Tokyo").json()["stats"]["learned_today"]
        self.assertEqual((utc, tokyo), (1, 0))
        self.assertEqual(Learner.objects.get(user=self.user).timezone, "Asia/Tokyo")

    def test_invalid_timezone_headers_are_ignored(self):
        self.onboard()
        for name in ("Not/AZone", "America", "../etc/passwd", "/etc/localtime", "x" * 100):
            self.client.post("/type/next", HTTP_X_TIMEZONE=name)
            self.assertEqual(Learner.objects.get(user=self.user).timezone, "", name)
        self.client.post("/type/next", HTTP_X_TIMEZONE="Asia/Tokyo")
        self.client.post("/type/next", HTTP_X_TIMEZONE="Not/AZone")
        learner = Learner.objects.get(user=self.user)
        self.assertEqual((learner.timezone, str(learner.zone())), ("Asia/Tokyo", "Asia/Tokyo"))
        learner.timezone = "garbage"
        self.assertEqual(str(learner.zone()), "UTC")

    def test_stats_needs_onboarding_and_clamps_the_window(self):
        self.assertEqual(self.client.post("/type/stats").status_code, 409)
        self.assertEqual(self.client.post("/type/stats").json(), {"error": "not_onboarded"})
        self.onboard()
        counts = [len(self.client.post("/type/stats", body, format="json").json()["days"])
                  for body in ({}, {"days": 1}, {"days": 9999}, {"days": 14}, {"days": "x"})]
        self.assertEqual(counts, [30, 7, 365, 14, 30])

    def build_history(self):
        """Learner in Tokyo; now is 2026-03-10 12:00 there. Four learned words, one of them before tracking."""
        learner = self.onboard("雨")
        learner.timezone = "Asia/Tokyo"
        learner.save()
        learner.words.all().delete()
        for text, learned_at, due in [
            ("先生", None, self.at(2026, 3, 1)),                               # before tracking, overdue
            ("会議", self.at(2026, 3, 9, 20), self.at(2026, 3, 10, 16)),       # learned on the 10th in Tokyo, due on the 11th there
            ("雨", self.at(2026, 3, 8), self.at(2026, 3, 11, 16)),             # kanji known at onboarding, due on the 12th there
            ("食べる", self.at(2026, 3, 5, 12), self.at(2026, 3, 20)),
        ]:
            UserWord.objects.create(learner=learner, word=Word.objects.get(text=text), status=UserWord.REVIEW,
                                    learned_at=learned_at, due=due)
        word = Word.objects.create(text="学生", reading="がくせい", frequency=1)
        UserWord.objects.create(learner=learner, word=word, status=UserWord.LEARNING)
        for rating, at in [(3, self.at(2026, 3, 10, 2)), (2, self.at(2026, 3, 10, 2)),
                           (4, self.at(2026, 3, 9, 20)), (3, self.at(2026, 2, 1, 12))]:
            ReviewLog.objects.create(learner=learner, word=word, rating=rating, at=at)
        for at in [self.at(2026, 3, 10, 2), self.at(2026, 3, 10, 3), self.at(2026, 3, 9, 2),
                   self.at(2026, 3, 8, 2), self.at(2026, 3, 6, 2)]:
            SentenceLog.objects.create(learner=learner, at=at)
        return learner

    def test_report_has_the_shape_of_the_stats_contract(self):
        self.build_history()
        with patch.object(timezone, "now", return_value=self.at(2026, 3, 10, 3)):
            report = self.client.post("/type/stats", {"days": 7}, format="json").json()
        self.assertEqual(set(report), {"today", "totals", "days", "forecast", "jlpt", "tracking_since"})
        self.assertEqual(set(report["today"]), {"words", "kanji", "reviews", "sentences"})
        self.assertEqual(set(report["totals"]), {"words", "kanji", "known_kanji", "learning", "due", "sentences",
                                                 "reviews", "retention", "streak"})
        self.assertEqual(set(report["days"][0]), {"date", "words", "kanji", "reviews", "hard", "sentences",
                                                  "total_words", "total_kanji"})
        self.assertEqual(set(report["forecast"][0]), {"date", "due"})
        self.assertEqual(set(report["jlpt"][0]), {"level", "total", "learned", "known"})

    def test_report_buckets_history_by_local_day(self):
        learner = self.build_history()
        with patch.object(timezone, "now", return_value=self.at(2026, 3, 10, 3)):
            report = learner.report(7)

        days = report["days"]
        self.assertEqual([d["date"] for d in days], [f"2026-03-{n:02d}" for n in range(4, 11)])  # zero-filled, ends today
        self.assertEqual([d["words"] for d in days], [0, 1, 0, 0, 1, 0, 1])
        self.assertEqual([d["kanji"] for d in days], [0, 1, 0, 0, 0, 0, 2])  # 食; 会 議 (先生 predates tracking, 雨 was known)
        self.assertEqual([d["total_words"] for d in days], [1, 2, 2, 2, 3, 3, 4])  # 先生 counts from the start
        self.assertEqual([d["total_kanji"] for d in days], [2, 3, 3, 3, 3, 3, 5])
        self.assertEqual([d["reviews"] for d in days], [0, 0, 0, 0, 0, 0, 3])
        self.assertEqual([d["hard"] for d in days], [0, 0, 0, 0, 0, 0, 1])
        self.assertEqual([d["sentences"] for d in days], [0, 0, 1, 0, 1, 1, 2])
        self.assertEqual(report["today"], {"words": 1, "kanji": 2, "reviews": 3, "sentences": 2})

        levels = jlpt_kanji()
        learned = set("先生会議食")  # 雨 is known, so it isn't learned in the app
        totals = report["totals"]
        self.assertEqual(totals, {
            "words": 4, "kanji": 5, "known_kanji": len(learner.known_kanji), "learning": 1, "due": 1,
            "sentences": 5, "reviews": 4, "retention": 2 / 3,  # the February review is outside the window
            "streak": 3,  # the 8th, 9th and 10th; nothing on the 7th
        })
        self.assertEqual(report["tracking_since"], "2026-02-01")
        self.assertEqual(
            report["forecast"],
            [{"date": f"2026-03-{n}", "due": due} for n, due in zip(range(10, 17), [1, 1, 1, 0, 0, 0, 0])],
        )  # 先生 is overdue so it counts today; the rest fall on their Tokyo dates
        self.assertEqual([j["level"] for j in report["jlpt"]], [5, 4, 3, 2, 1])
        for entry in report["jlpt"]:
            in_level = {k for k, level in levels.items() if level == entry["level"]}
            self.assertEqual(entry["total"], len(in_level))
            self.assertEqual(entry["learned"], len(in_level & learned))
            self.assertEqual(entry["known"], len(in_level & set(learner.known_kanji)))

    def test_streak_ends_yesterday_until_something_is_typed_today(self):
        learner = self.build_history()
        with patch.object(timezone, "now", return_value=self.at(2026, 3, 10, 14)):  # the 10th, 23:00 in Tokyo
            self.assertEqual(learner.report(7)["totals"]["streak"], 3)
        SentenceLog.objects.filter(learner=learner, at__gte=self.at(2026, 3, 10)).delete()
        with patch.object(timezone, "now", return_value=self.at(2026, 3, 10, 3)):
            self.assertEqual(learner.report(7)["totals"]["streak"], 2)  # the 8th and 9th
        with patch.object(timezone, "now", return_value=self.at(2026, 3, 11, 3)):
            self.assertEqual(learner.report(7)["totals"]["streak"], 0)  # the 9th was two days ago

    def test_report_for_a_learner_with_no_history(self):
        self.onboard()
        report = self.client.post("/type/stats").json()
        self.assertIsNone(report["totals"]["retention"])
        self.assertIsNone(report["tracking_since"])
        self.assertEqual((report["totals"]["streak"], report["totals"]["words"]), (0, 0))
