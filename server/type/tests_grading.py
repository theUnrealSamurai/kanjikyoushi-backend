"""How typing a sentence grades its words (owner, 2026-11-05): Good when typed clean, Easy after clean reads on other
days, a next-day check after a look-up (clean -> Hard, looked up again -> Again), Again for two misses in a row, and
for new words an Easy start from the first-sighting test or a Good/Hard start from the warm-up check."""
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from fsrs import Card, Rating, State

from .models import CHECK_DELAY, EASY_READS, WARMUP_SIGHTINGS, Learner, ReviewLog, Sentence, UserWord, Word
from .tests import NO_DICTIONARY, build_corpus


NOW = datetime(2026, 3, 10, 3, tzinfo=dt_timezone.utc)  # 12:00 in Tokyo


@NO_DICTIONARY
class GradingTests(TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.txt", "\n".join([
                "今日は先生と会議があります。", "先生が来た。", "会議は長い。", "今日は雨です。",
            ]))
        self.learner = Learner.objects.create(user=User.objects.create_user("taro", "t@example.com", "a-long-password"),
                                              timezone="Asia/Tokyo")
        with patch.object(timezone, "now", return_value=NOW):
            self.learner.onboard("")
        UserWord.objects.filter(learner=self.learner).delete()
        UserWord.objects.bulk_create(UserWord(learner=self.learner, word=word, status=UserWord.KNOWN)
                                     for word in Word.objects.all())
        self.now = NOW

    def word(self, text):
        return self.learner.words.get(word__text=text)

    def learned(self, text, due_in=timedelta(0), interval=timedelta(days=11), **fields):
        """Make `text` a learned word with a review card due `due_in` from now."""
        due = self.now + due_in
        card = Card(state=State.Review, stability=11.0, difficulty=5.0, due=due, last_review=due - interval)
        UserWord.objects.filter(learner=self.learner, word__text=text).update(
            status=UserWord.REVIEW, card=card.to_dict(), due=due, **fields)

    def warming(self, text, sightings):
        UserWord.objects.filter(learner=self.learner, word__text=text).update(status=UserWord.LEARNING, sightings=sightings)

    def put_on_screen(self, sentence_text, review=(), learning=()):
        sentence = Sentence.objects.get(text=sentence_text)
        ids = lambda texts: list(Word.objects.filter(text__in=texts).values_list("id", flat=True))
        self.learner.refresh_from_db()
        self.learner.current = {"sentence": sentence.id, "review": ids(review), "learning": ids(learning)}
        self.learner.save()
        return sentence

    def type(self, sentence_text, review=(), learning=(), tab=(), skip=(), help=False):
        """Type the sentence (now = self.now) with these words graded; tab/skip: words looked up / ⌥Tab'd.
        Returns {word text: outcome}."""
        sentence = self.put_on_screen(sentence_text, review, learning)
        ids = lambda texts: list(Word.objects.filter(text__in=texts).values_list("id", flat=True))
        with patch.object(timezone, "now", return_value=self.now):
            outcomes = self.learner.submit(sentence.id, sentence.text, ids(tab), ids(skip), help)
        return {Word.objects.get(id=word_id).text: outcome for word_id, outcome in outcomes.items()}

    def ratings(self, text):
        return list(ReviewLog.objects.filter(learner=self.learner, word__text=text).order_by("at", "id")
                    .values_list("rating", flat=True))

    def test_a_due_word_typed_clean_is_good(self):
        self.learned("先生")
        self.assertEqual(self.type("先生が来た。", review=["先生"]), {"先生": "good"})
        self.assertEqual(self.ratings("先生"), [Rating.Good])
        self.assertEqual(self.word("先生").last_rating, Rating.Good)

    def test_a_look_up_is_checked_the_next_day_and_clean_then_is_hard(self):
        self.learned("先生")
        card = self.word("先生").card
        self.assertEqual(self.type("先生が来た。", review=["先生"], tab=["先生"]), {"先生": "check"})
        teacher = self.word("先生")
        self.assertEqual((teacher.owes_check, teacher.due, teacher.card), (True, self.now + CHECK_DELAY, card))
        self.assertEqual(self.ratings("先生"), [])  # not rated yet

        self.now += timedelta(hours=1)  # seen again the same day, read clean: not the check, nothing changes
        self.assertEqual(self.type("先生が来た。"), {})
        self.assertEqual(self.word("先生").owes_check, True)

        self.now += CHECK_DELAY
        self.assertEqual(self.type("先生が来た。", review=["先生"]), {"先生": "hard"})
        teacher = self.word("先生")
        self.assertEqual((teacher.owes_check, teacher.last_rating), (False, Rating.Hard))
        self.assertEqual(self.ratings("先生"), [Rating.Hard])
        self.assertGreater(teacher.due, self.now + timedelta(days=11))  # Hard is a pass: the interval still grows

    def test_a_check_that_needs_tab_again_is_again(self):
        self.learned("先生")
        self.type("先生が来た。", review=["先生"], tab=["先生"])
        self.now += CHECK_DELAY
        self.assertEqual(self.type("先生が来た。", review=["先生"], tab=["先生"]), {"先生": "again"})
        teacher = self.word("先生")
        self.assertEqual((teacher.owes_check, Card.from_dict(teacher.card).state), (False, State.Relearning))
        self.assertLess(teacher.due, self.now + timedelta(hours=1))
        self.assertEqual(self.ratings("先生"), [Rating.Again])

    def test_a_check_owed_and_looked_up_again_before_it_is_due_is_again(self):
        self.learned("先生")
        self.type("先生が来た。", review=["先生"], tab=["先生"])
        self.now += timedelta(hours=1)
        self.assertEqual(self.type("先生が来た。", tab=["先生"]), {"先生": "again"})  # not due, still can't read it

    def test_a_look_up_right_after_a_miss_is_again(self):
        self.learned("先生", last_rating=Rating.Hard)  # e.g. a Hard from before 2026-11-05, which meant "needed Tab"
        self.assertEqual(self.type("先生が来た。", review=["先生"], tab=["先生"]), {"先生": "again"})
        self.learned("会議", due_in=timedelta(days=5), last_rating=Rating.Again)
        self.assertEqual(self.type("会議は長い。", tab=["会議"]), {"会議": "again"})  # not due: same rule
        self.learned("今日", due_in=timedelta(days=5), last_rating=Rating.Good)
        self.assertEqual(self.type("今日は雨です。", tab=["今日"]), {"今日": "check"})

    def test_skipped_words_are_not_graded(self):
        self.learned("先生")
        self.assertEqual(self.type("先生が来た。", review=["先生"], skip=["先生"]), {})
        self.assertEqual((self.word("先生").due, self.ratings("先生")), (self.now, []))  # still due

    def test_clean_reads_on_other_days_make_the_next_review_easy(self):
        self.learned("先生", due_in=timedelta(days=5))  # learned, not due; last reviewed 6 days ago
        self.type("先生が来た。")
        self.type("先生が来た。")  # the same day: one read a day
        self.assertEqual(self.word("先生").clean_reads, 1)

        self.now += timedelta(days=1)
        self.type("先生が来た。", help=True)  # help mode showed every reading
        self.type("先生が来た。", skip=["先生"])  # ⌥Tab showed the keys
        self.assertEqual(self.word("先生").clean_reads, 1)
        self.type("先生が来た。")
        self.assertEqual(self.word("先生").clean_reads, EASY_READS)

        self.now += timedelta(days=4)
        self.assertEqual(self.type("先生が来た。", review=["先生"]), {"先生": "easy"})
        self.assertEqual((self.word("先生").clean_reads, self.ratings("先生")), (0, [Rating.Easy]))

    def test_reads_on_the_day_of_the_last_review_dont_count(self):
        self.learned("先生", due_in=timedelta(days=5), interval=timedelta(days=5, hours=1))  # reviewed an hour ago
        self.type("先生が来た。")
        self.assertEqual(self.word("先生").clean_reads, 0)

    def test_a_look_up_resets_the_clean_reads(self):
        self.learned("先生", due_in=timedelta(days=5), clean_reads=EASY_READS)
        self.type("先生が来た。", tab=["先生"])
        self.assertEqual((self.word("先生").clean_reads, self.word("先生").owes_check), (0, True))

    def test_a_new_word_that_passes_its_first_test_starts_easy(self):
        self.warming("先生", 0)
        self.assertEqual(self.type("先生が来た。", learning=["先生"]), {"先生": "easy"})
        teacher = self.word("先生")
        self.assertEqual((teacher.status, teacher.sightings, teacher.last_rating), (UserWord.REVIEW, 1, Rating.Easy))
        self.assertEqual(Card.from_dict(teacher.card).state, State.Review)
        self.assertGreater(teacher.due, self.now + timedelta(days=7))

    def test_needing_help_on_the_first_test_means_the_warm_up(self):
        self.warming("先生", 0)
        self.warming("会議", 0)
        self.assertEqual(self.type("今日は先生と会議があります。", learning=["先生", "会議"], tab=["先生"], skip=["会議"]),
                         {"先生": "warmup", "会議": "warmup"})
        self.assertEqual([(self.word(t).status, self.word(t).sightings) for t in ("先生", "会議")],
                         [(UserWord.LEARNING, 1)] * 2)

    def test_the_warm_up_check_sets_the_first_rating(self):
        self.warming("先生", WARMUP_SIGHTINGS - 1)
        self.warming("会議", WARMUP_SIGHTINGS - 1)
        self.assertEqual(self.type("今日は先生と会議があります。", learning=["先生", "会議"], tab=["会議"]),
                         {"先生": "good", "会議": "hard"})
        teacher, meeting = self.word("先生"), self.word("会議")
        self.assertEqual((teacher.status, teacher.last_rating, meeting.status, meeting.last_rating),
                         (UserWord.REVIEW, Rating.Good, UserWord.REVIEW, Rating.Hard))
        self.assertGreater(meeting.card["difficulty"], teacher.card["difficulty"])  # set for life
        self.assertEqual(self.ratings("先生") + self.ratings("会議"), [])  # graduation reviews aren't logged

    def test_the_warm_up_check_comes_again_after_option_tab(self):
        self.warming("先生", WARMUP_SIGHTINGS - 1)
        self.assertEqual(self.type("先生が来た。", learning=["先生"], skip=["先生"]), {})
        self.assertEqual((self.word("先生").status, self.word("先生").sightings), (UserWord.LEARNING, WARMUP_SIGHTINGS - 1))

    def test_earlier_warm_up_typings_show_furigana_and_the_check_hides_it(self):
        self.warming("先生", 1)
        sentence = self.put_on_screen("先生が来た。", learning=["先生"])
        roles = lambda: {t["text"]: t["role"] for t in self.learner.sentence_payload(sentence, {})["tokens"]}
        self.assertEqual(roles()["先生"], "learning")
        self.assertEqual(self.type("先生が来た。", learning=["先生"]), {})
        sentence = self.put_on_screen("先生が来た。", learning=["先生"])
        self.assertEqual(roles()["先生"], "test")  # the warm-up check

    def test_a_known_word_looked_up_goes_to_the_warm_up(self):
        self.assertEqual(self.type("今日は雨です。", tab=["雨"]), {"雨": "warmup"})
        self.assertEqual((self.word("雨").status, self.word("雨").sightings), (UserWord.LEARNING, 1))

    def test_the_payload_says_what_each_word_will_get(self):
        self.learned("先生", clean_reads=EASY_READS)  # due, Easy if clean
        self.warming("会議", 0)                       # first-sighting test
        self.learned("今日", due_in=timedelta(days=3), owes_check=True)  # learned, owes a check, not due yet
        sentence = self.put_on_screen("今日は先生と会議があります。", review=["先生"], learning=["会議"])
        statuses = dict(self.learner.words.values_list("word_id", "status"))
        grades = {t["text"]: t["grade"] for t in self.learner.sentence_payload(sentence, statuses)["tokens"]
                  if t["text"] in ("今日", "先生", "会議")}
        self.assertEqual(grades, {
            "今日": {"clean": None, "tab": "again", "skip": None},
            "先生": {"clean": "easy", "tab": "check", "skip": None},
            "会議": {"clean": "easy", "tab": "warmup", "skip": "warmup"},
        })
        # What submit does matches, typed clean.
        self.assertEqual(self.type("今日は先生と会議があります。", review=["先生"], learning=["会議"]),
                         {"先生": "easy", "会議": "easy"})
