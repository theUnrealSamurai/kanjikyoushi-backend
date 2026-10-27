"""Readings carry over: a word never taught needs no furigana when every kanji in it is read the way the learner
already reads it in a word they've learned (or is a kanji they knew at onboarding)."""
from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase

from .corpus import reading_units
from .models import Learner, Sentence, UserWord, Word


class ReadingUnitsTests(SimpleTestCase):
    def test_one_reading_per_kanji(self):
        self.assertEqual(reading_units("友達", "ともだち"), (("友", "とも"), ("達", "たち")))  # rendaku: だち is たち
        self.assertEqual(reading_units("人達", "ひとたち"), (("人", "ひと"), ("達", "たち")))
        self.assertEqual(reading_units("食べ", "たべ"), (("食", "た"),))  # okurigana isn't a reading
        self.assertEqual(reading_units("先生", "せんせい"), (("先", "せん"), ("生", "せい")))

    def test_a_different_reading_is_a_different_unit(self):
        self.assertEqual(reading_units("発達", "はったつ"), (("発", "はつ"), ("達", "たつ")))  # はっ is はつ
        self.assertEqual(reading_units("達成", "たっせい"), (("達", "たつ"), ("成", "せい")))  # たっ is たつ, not たち

    def test_sound_changes_are_the_plain_reading(self):
        self.assertEqual(reading_units("一杯", "いっぱい"), (("一", "いち"), ("杯", "はい")))
        self.assertEqual(reading_units("日曜日", "にちようび"), (("日", "にち"), ("曜", "よう"), ("日", "ひ")))
        self.assertEqual(reading_units("時々", "ときどき"), (("時", "とき"), ("時", "とき")))  # 々 is the kanji before it
        self.assertEqual(reading_units("受付", "うけつけ"), (("受", "う"), ("付", "つ")))  # 受ける's う, 付ける's つ

    def test_special_readings_are_one_unit(self):
        self.assertEqual(reading_units("今日", "きょう"), (("今日", "きょう"),))
        self.assertEqual(reading_units("大人", "おとな"), (("大人", "おとな"),))

    def test_kana_has_no_units(self):
        self.assertEqual(reading_units("だった", ""), ())


class CarryOverTests(TestCase):
    TEXT = "同じ人達だった。"

    def setUp(self):
        words = [("同じ", "おなじ"), ("人", "ひと"), ("達", "たち"), ("友達", "ともだち"), ("発達", "はったつ")]
        self.words = {text: Word.objects.create(text=text, reading=reading, list_level=5, frequency=1)
                      for text, reading in words}
        w = self.words
        self.sentence = Sentence.objects.create(text=self.TEXT, tokens=[
            ["同じ", "おなじ", w["同じ"].id, None], ["人", "ひと", w["人"].id, None], ["達", "たち", w["達"].id, None],
            ["だっ", "", None, None], ["た", "", None, None], ["。", "", None, None],
        ])
        user = User.objects.create_user("taro", "taro@example.com", "a-long-password")
        self.learner = Learner.objects.create(user=user, onboarded=True, known_kanji="同人",
                                              current={"sentence": self.sentence.id, "review": [], "learning": []})
        for text in ("同じ", "人"):
            UserWord.objects.create(learner=self.learner, word=w[text], status=UserWord.KNOWN)

    def learn(self, text, status=UserWord.REVIEW):
        UserWord.objects.create(learner=self.learner, word=self.words[text], status=status)

    def role_of_達(self):
        statuses = dict(self.learner.words.values_list("word_id", "status"))
        tokens = self.learner.sentence_payload(self.sentence, statuses)["tokens"]
        return next(t["role"] for t in tokens if t["text"] == "達")

    def test_never_taught_and_no_reading_known_shows_furigana(self):
        self.assertEqual(self.role_of_達(), "unseen")

    def test_a_learned_word_with_the_same_reading_hides_it(self):
        self.learn("友達")
        self.assertEqual(self.role_of_達(), "known")

    def test_a_word_still_warming_up_doesnt_count(self):
        self.learn("友達", UserWord.LEARNING)
        self.assertEqual(self.role_of_達(), "unseen")

    def test_another_reading_of_the_kanji_doesnt_count(self):
        self.learn("発達")  # 達 read たつ
        self.assertEqual(self.role_of_達(), "unseen")

    def test_a_kanji_known_at_onboarding_counts_for_any_reading(self):
        Learner.objects.filter(id=self.learner.id).update(known_kanji="同人達")
        self.learner.refresh_from_db()
        self.assertEqual(self.role_of_達(), "known")

    def test_a_word_warming_up_keeps_its_furigana(self):
        self.learn("友達")
        self.learn("達", UserWord.LEARNING)  # e.g. looked up and sent back to the warm-up
        self.assertEqual(self.role_of_達(), "unseen")

    def test_looking_it_up_grades_nothing(self):
        self.learn("友達")
        before = set(self.learner.words.values_list("word_id", "status"))
        self.learner.submit(self.sentence.id, self.TEXT, revealed=[self.words["達"].id])
        self.assertEqual(set(self.learner.words.values_list("word_id", "status")), before)
