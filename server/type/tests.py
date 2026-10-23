import json
import tempfile
import zipfile
from datetime import timedelta
from io import StringIO
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from .corpus import furigana, is_joyo_only, same_text, tokenize
from .dictionary import cached_dictionary, find_kana_meanings, find_meanings, index_by_kana, index_by_kanji
from .models import WARMUP_SIGHTINGS, KanaWord, Learner, Sentence, UserWord, Word


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

    def test_kana_only_content_words_can_be_looked_up(self):
        tokens = {t.surface: t.kana_word for t in tokenize("その言葉に思わず僕は口をつぐんでしまった。")}
        self.assertEqual(tokens["つぐん"], ("つぐむ", "つぐむ", "噤む"))
        self.assertIsNone(tokens["に"])  # particles are grammar, not words to look up
        self.assertIsNone(tokens["言葉"])  # kanji words are learned instead

    def test_typed_text_comparison(self):
        self.assertTrue(same_text("今日は雨です", "今日は雨です。"))
        self.assertTrue(same_text("４ドル", "4ドル"))
        self.assertFalse(same_text("今日は飴です。", "今日は雨です。"))

    def test_build_corpus_loads_sentences_and_counts_words(self):
        rows = ["index\ttext\ttranslation"] + [f"{i}\t{text}\tEnglish {i}" for i, text in enumerate(SENTENCES)]
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.tsv", "\n".join(rows) + "\n")

        self.assertEqual(Sentence.objects.count(), 6)  # テントが一つしかない has nothing to learn
        self.assertEqual(Sentence.objects.get(text="今日は雨です。").translation, "English 2")
        self.assertEqual(Word.objects.get(text="先生").frequency, 3)


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

    def test_new_words_warm_up_then_get_reviewed(self):
        self.onboard()
        payload = self.next()
        self.assertTrue(self.word_ids(payload, "learning"))

        for _ in range(30):
            response = self.submit(payload)
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            if payload.get("done"):
                break
        learner = Learner.objects.get(user=self.user)
        self.assertTrue(learner.words.filter(status=UserWord.REVIEW, sightings__gte=WARMUP_SIGHTINGS).exists())

    def test_review_ratings_come_from_tab_and_mistakes(self):
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

        self.assertEqual(self.submit(payload, revealed=[teacher.id], missed=[meeting.id]).status_code, 200)
        cards = {w.word_id: w.card for w in learner.words.filter(status=UserWord.REVIEW)}
        self.assertGreater(cards[teacher.id]["stability"], cards[meeting.id]["stability"])  # Hard beats Again

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
        self.assertEqual(self.word_ids(payload, "learning"), [meeting.id])
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
            set(payload), {"sentence_id", "text", "translation", "romaji", "tokens", "words", "kana_words", "stats"}
        )

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
