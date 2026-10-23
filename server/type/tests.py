import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .corpus import furigana, same_text, tokenize
from .models import WARMUP_SIGHTINGS, Learner, Sentence, UserWord, Word


SENTENCES = [
    "今日は先生と会議があります。",
    "先生は会議室で食べました。",
    "今日は雨です。",
    "先生が来た。",
    "会議は長い。",
    "田中さんは四つ食べた。",
    "テントが一つしかない。",
]


def build_corpus(path, contents):
    path.write_text(contents, encoding="utf-8")
    with open(path.parent / "log.txt", "w") as log:
        call_command("build_corpus", str(path), stdout=log)


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

        self.assertEqual(self.submit(payload, revealed=[teacher.id], missed=[meeting.id]).status_code, 200)
        cards = {w.word_id: w.card for w in learner.words.filter(status=UserWord.REVIEW)}
        self.assertGreater(cards[teacher.id]["stability"], cards[meeting.id]["stability"])  # Hard beats Again

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
