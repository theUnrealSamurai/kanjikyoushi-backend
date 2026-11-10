"""The kanji celebrations (owner, 2026-11-10: "a small little animation when a user finishes a level", "100-250-500-1000-
1500-2000 (500+ from here)"): a submit that graduates a word reports the JLPT level it completed and the kanji-count
milestone it passed, and nothing otherwise."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient

from . import models
from .corpus import jlpt_kanji, kanji_rank
from .models import Learner, Sentence, UserWord, Word, kanji_milestone

LEVELS = {"日": 5, "本": 5, "人": 5, "語": 4, "話": 4}  # a small made-up kanji list for the level tests


def word(text, level=5, frequency=10):
    return Word.objects.create(text=text, reading=text, list_level=level, frequency=frequency)


class MilestoneTests(TestCase):
    def test_the_fixed_milestones_then_every_500(self):
        self.assertEqual(kanji_milestone(99, 100), 100)
        self.assertEqual(kanji_milestone(0, 1), None)
        self.assertEqual(kanji_milestone(100, 101), None)
        self.assertEqual(kanji_milestone(249, 250), 250)
        self.assertEqual(kanji_milestone(499, 500), 500)
        self.assertEqual(kanji_milestone(999, 1000), 1000)
        self.assertEqual(kanji_milestone(1999, 2000), 2000)
        self.assertEqual(kanji_milestone(2000, 2001), None)
        self.assertEqual(kanji_milestone(2499, 2500), 2500)
        self.assertEqual(kanji_milestone(2999, 3000), 3000)
        self.assertEqual(kanji_milestone(3000, 3499), None)

    def test_one_jump_over_several_reports_the_highest(self):
        self.assertEqual(kanji_milestone(90, 600), 500)
        self.assertEqual(kanji_milestone(1900, 3100), 3000)


class CelebrateTests(TestCase):
    def setUp(self):
        user = User.objects.create_user("taro", "taro@example.com", "a-long-password")
        self.learner = Learner.objects.create(user=user, known_kanji="", onboarded=True)

    def warm(self, *texts, status=UserWord.LEARNING):
        words = [Word.objects.get(text=text) for text in texts]
        for made in words:
            UserWord.objects.create(learner=self.learner, word=made, status=status)
        self.learner.current = {"sentence": 0, "review": [], "learning": [made.id for made in words]}
        return words

    def graduate(self, *texts, levels=LEVELS):
        """The submit step: snapshot the warm-up words on screen, graduate `texts`, ask what to celebrate."""
        snapshot = self.learner.warming_words()
        UserWord.objects.filter(learner=self.learner, word__text__in=texts).update(status=UserWord.REVIEW)
        with patch.object(models, "jlpt_kanji", return_value=levels):
            return self.learner.celebrations(snapshot)

    def learn(self, count):
        """`count` single-kanji words already learned (status REVIEW), from the real JLPT kanji lists."""
        for kanji in list(jlpt_kanji())[:count]:
            UserWord.objects.create(learner=self.learner, word=word(kanji), status=UserWord.REVIEW)

    def test_nothing_graduating_celebrates_nothing(self):
        word("日"), word("本")
        self.warm("日", "本")
        self.assertEqual(self.learner.celebrations(self.learner.warming_words()), [])

    def test_the_hundredth_kanji_is_celebrated(self):
        self.learn(99)
        extra = list(jlpt_kanji())[99]
        word(extra)
        self.warm(extra)
        self.assertEqual(self.graduate(extra, levels={}), [{"kind": "kanji", "count": 100}])

    def test_already_above_a_threshold_is_not_celebrated_again(self):
        self.learn(100)
        extra = list(jlpt_kanji())[100]
        word(extra)
        self.warm(extra)
        self.assertEqual(self.graduate(extra, levels={}), [])

    def test_a_word_of_kanji_already_learned_adds_nothing(self):
        self.learn(99)
        first = list(jlpt_kanji())[0]
        word(first * 2)  # same kanji again in a new word: still 99 kanji
        self.warm(first * 2)
        self.assertEqual(self.graduate(first * 2, levels={}), [])

    def test_kanji_known_at_onboarding_dont_count(self):
        kanji = list(jlpt_kanji())
        self.learner.known_kanji = "".join(kanji[:50])
        self.learner.save()
        for k in kanji[:99]:  # 49 learned here, 50 known at onboarding
            UserWord.objects.create(learner=self.learner, word=word(k), status=UserWord.REVIEW)
        word(kanji[99])
        self.warm(kanji[99])
        self.assertEqual(self.graduate(kanji[99], levels={}), [])  # the 100th kanji overall, but only the 50th learned here

    def test_repeat_marks_dont_count(self):
        self.learn(99)
        word("々")
        self.warm("々")
        self.assertEqual(self.graduate("々", levels={}), [])

    def test_a_level_is_celebrated_when_its_last_kanji_is_covered(self):
        for text in ("日本", "人", "語", "話"):
            word(text, 5 if text in ("日本", "人") else 4)
        UserWord.objects.create(learner=self.learner, word=Word.objects.get(text="日本"), status=UserWord.REVIEW)
        self.warm("人")
        self.assertEqual(self.graduate("人"), [{"kind": "level", "level": 5}])

    def test_a_level_with_kanji_left_is_not_celebrated(self):
        for text in ("日本", "人", "語", "話"):
            word(text, 5 if text in ("日本", "人") else 4)
        self.warm("日本")
        self.assertEqual(self.graduate("日本"), [])  # 人 is still missing
        self.warm("語")
        self.assertEqual(self.graduate("語"), [])  # a new N4 kanji, with 話 still missing

    def test_kanji_nothing_can_teach_dont_block_a_level(self):
        for text in ("日本", "人"):
            word(text)
        self.warm("人")
        UserWord.objects.create(learner=self.learner, word=Word.objects.get(text="日本"), status=UserWord.REVIEW)
        # 話 is N4 but in no teachable word, so N4 has nothing left to cover: no celebration for it either
        self.assertEqual(self.graduate("人"), [{"kind": "level", "level": 5}])

    def test_a_level_covered_before_is_not_celebrated_again(self):
        for text in ("日本", "人", "語"):
            word(text, 5 if text != "語" else 4)
        for text in ("日本", "人"):
            UserWord.objects.create(learner=self.learner, word=Word.objects.get(text=text), status=UserWord.REVIEW)
        self.warm("語")
        self.assertEqual(self.graduate("語"), [{"kind": "level", "level": 4}])  # N4's only teachable kanji, not N5 again

    def test_a_level_already_known_at_onboarding_is_never_celebrated(self):
        word("日本"), word("人"), word("語"), word("話")
        self.learner.known_kanji = "日本人"
        UserWord.objects.create(learner=self.learner, word=Word.objects.get(text="日本"), status=UserWord.KNOWN)
        UserWord.objects.create(learner=self.learner, word=Word.objects.get(text="人"), status=UserWord.KNOWN)
        self.warm("語")
        # N5 was known at onboarding and 語 is only the first of N4's two kanji
        self.assertEqual(self.graduate("語"), [])
        self.warm("話")
        self.assertEqual(self.graduate("話"), [{"kind": "level", "level": 4}])  # N4 is done: the only level celebrated

    def test_a_level_and_a_count_together_levels_first(self):
        self.learn(98)
        kanji = list(jlpt_kanji())
        self.learner.known_kanji = ""
        levels = {kanji[98]: 5, kanji[99]: 5}
        word(kanji[98]), word(kanji[99])
        self.warm(kanji[98], kanji[99])
        # N5 here is just those two kanji (patched levels): both covered, 98 + 2 = 100 learned
        self.assertEqual(self.graduate(kanji[98], kanji[99], levels=levels), [{"kind": "level", "level": 5}, {"kind": "kanji", "count": 100}])

    def test_an_unfinished_warm_up_is_not_a_graduation(self):
        self.learn(99)
        extra = list(jlpt_kanji())[99]
        word(extra)
        self.warm(extra)
        snapshot = self.learner.warming_words()
        self.assertEqual(self.learner.celebrations(snapshot), [])  # still LEARNING: nothing graduated

    def test_no_warm_up_words_on_screen_costs_nothing(self):
        word("日")
        self.learner.current = {"sentence": 0, "review": [], "learning": []}
        with self.assertNumQueries(0):
            self.assertEqual(self.learner.celebrations({}), [])


class SubmitCelebrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("taro", "taro@example.com", "a-long-password")
        self.learner = Learner.objects.create(user=self.user, known_kanji="", onboarded=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_submit_reports_the_celebration_and_otherwise_an_empty_list(self):
        kanji = list(jlpt_kanji())
        for k in kanji[:99]:
            UserWord.objects.create(learner=self.learner, word=word(k), status=UserWord.REVIEW)
        target = word(kanji[99])
        UserWord.objects.create(learner=self.learner, word=target, status=UserWord.LEARNING, sightings=0)
        text = kanji[99] + "。"
        made = Sentence.objects.create(text=text, tokens=[[target.text, target.reading, target.id, None], ["。", "。", None, None]],
                                       rank=kanji_rank(text))
        made.words.set([target])
        self.learner.current = {"sentence": made.id, "review": [], "learning": [target.id]}
        self.learner.save()
        body = self.client.post("/type/submit", {"sentence_id": made.id, "typed": text}, format="json").json()
        self.assertEqual(body["graded"], {str(target.id): "easy"})
        self.assertEqual([c for c in body["celebrate"] if c["kind"] == "kanji"], [{"kind": "kanji", "count": 100}])

        # the next submit graduates nothing, so there's nothing to celebrate
        again = self.client.post("/type/submit", {"sentence_id": made.id, "typed": text}, format="json")
        self.assertEqual(again.status_code, 400)  # (that sentence is no longer on screen)
