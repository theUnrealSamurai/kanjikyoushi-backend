"""The owner's kanji-by-kanji order (2026-11-04): new words go N5 to N1 by their hardest kanji, sentences are ranked
by their hardest kanji and searched only up to the learner's bookmark, and sentences with kanji the learner can't
read yet are avoided."""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase

from . import models
from .corpus import kanji_rank
from .models import (Learner, Sentence, UserWord, Word, bookmark, learning_order, refresh_bookmarks, sample_sentences,
                     sentence_range, teachable_words)


def word(text, level=5, frequency=10, reading=""):
    return Word.objects.create(text=text, reading=reading or text, list_level=level, frequency=frequency)


def sentence(text, *words):
    made = Sentence.objects.create(text=text, tokens=[[w.text, w.reading, w.id, None] for w in words], rank=kanji_rank(text))
    made.words.set(words)
    return made


class KanjiOrderTests(TestCase):
    def learner(self, known):
        user = User.objects.create_user("taro", "taro@example.com", "a-long-password")
        return Learner.objects.create(user=user, known_kanji=known, onboarded=True)

    def order(self):
        return [text for _, text, _ in learning_order(teachable_words())]

    def test_words_come_by_the_harder_of_their_list_level_and_their_hardest_kanji(self):
        for text, level in [("椅子", 5), ("休学", 1), ("映画", 5), ("本", 4), ("銀行", 5), ("学生", 5)]:
            word(text, level)
        for text in ("日本", "子", "行く", "休む"):  # N5 words for those N5 kanji, so none is a leftover
            word(text, 5)
        # 学生 is N5 all round. 本, 銀行 and 映画 are stage N4 (an N4 word, then N4 kanji, by their place in the
        # kanji list: 銀 before 映). 休学 (an N1 word) and 椅子 (an N1 kanji) both wait for N1, 休 coming first.
        order = [text for text in self.order() if text in ("椅子", "休学", "映画", "本", "銀行", "学生")]
        self.assertEqual(order, ["学生", "本", "銀行", "映画", "休学", "椅子"])

    def test_a_kanji_no_word_of_its_own_stage_has_comes_at_the_end_of_that_stage(self):
        word("銀行", 5)
        word("部屋", 3)  # stage N3: 部 is N3
        word("不安", 3)  # stage N3 too, but it's the only word with 不 (N4) and has nothing harder: in for N4
        word("不便", 3)  # 便 is N3: never brought forward
        word("安い", 5)
        self.assertEqual(self.order(), ["安い", "銀行", "不安", "部屋", "不便"])

    def test_the_bookmark_stops_at_the_first_kanji_you_could_learn_but_cant_read(self):
        word("一人", 5)
        word("日本", 5)
        now = word("今", 5)
        word("先生", 5)
        Word.objects.create(text="東京", reading="とうきょう", list_level=None, frequency=10)  # a name: never taught
        learner = self.learner("一人日本")
        readable, reach = learner.readable_kanji(teachable_words())
        self.assertEqual(reach, kanji_rank("年"))  # 今 is next; 年 (just before it) isn't in any word, so it's passed
        self.assertNotIn("今", readable)
        self.assertIn("年", readable)
        self.assertNotIn("京", readable)  # past the bookmark, so not passed over yet

        UserWord.objects.create(learner=learner, word=now, status=UserWord.LEARNING)  # warming up: readable
        readable, reach = learner.readable_kanji(teachable_words())
        self.assertIn("今", readable)
        self.assertEqual(reach, kanji_rank("先") - 1)

    def test_sentences_with_kanji_you_cant_read_are_avoided_but_not_ruled_out(self):
        word("日本", 5)
        teacher = word("先生", 5)
        word("来る", 5, reading="くる")  # makes 来 a kanji to learn, after 先
        learner = self.learner("一人日本")
        UserWord.objects.create(learner=learner, word=teacher, status=UserWord.LEARNING)
        came = sentence("先生が来た。", teacher)  # 来 isn't readable yet
        readable = sentence("日本の先生です。", teacher)
        statuses = dict(learner.words.values_list("word_id", "status"))
        for _ in range(10):
            learner._pick_sentence([], [teacher.id], statuses, teachable_words())
            self.assertEqual(learner.current["sentence"], readable.id)

        readable.delete()  # only the sentence with 来 is left: better than nothing
        learner._pick_sentence([], [teacher.id], statuses, teachable_words())
        self.assertEqual(learner.current["sentence"], came.id)

    def test_only_sentences_up_to_the_bookmark_are_searched(self):
        teacher = word("先生", 5)
        easy = sentence("日本の先生。", teacher)  # stored in rank order, easiest first, like build_corpus does
        hard = sentence("映画の先生。", teacher)
        self.assertEqual(sentence_range(kanji_rank("先生"), kanji_rank("映")), (1, Sentence.objects.order_by("-id")[0].id))
        refresh_bookmarks()
        self.assertEqual(bookmark(kanji_rank("先生")), easy.id)
        self.assertEqual(bookmark(kanji_rank("映")), hard.id)
        self.assertEqual(bookmark(-5), 0)
        for _ in range(10):
            self.assertEqual(sample_sentences(teacher.id, *sentence_range(kanji_rank("先生"), kanji_rank("先生")), 200), [easy.id])
        self.assertCountEqual(sample_sentences(teacher.id, *sentence_range(kanji_rank("先生"), kanji_rank("映")), 200),
                              [easy.id, hard.id])

    def test_the_search_starts_at_the_words_rank_so_passage_lines_and_easier_sentences_dont_crowd_it_out(self):
        teacher = word("先生", 5)
        for n in range(30):  # passage-only lines with the word, loaded before the corpus: lower ids
            Sentence.objects.create(text=f"先生{n}。", tokens=[], rank=kanji_rank("先生"), standalone=False).words.add(teacher)
        for n in range(30):
            sentence(f"日本{n}。")  # easier than 先生: can't have it
        mine = [sentence(f"先生と日本{n}。", teacher).id for n in range(30)]
        refresh_bookmarks()
        first, last = sentence_range(kanji_rank("先生"), kanji_rank("先生"))
        self.assertEqual((first, last), (mine[0], mine[-1]))
        picks = {tuple(sample_sentences(teacher.id, first, last, 5)) for _ in range(30)}
        self.assertTrue(all(len(ids) == 5 and set(ids) <= set(mine) for ids in picks))
        self.assertGreater(len(picks), 5)  # a random starting point each time, not the same first five

    def test_warm_up_words_past_your_stage_go_back_until_their_stage(self):
        word("学生", 5)  # the next word: you're on stage N5
        chair, teacher, book = word("椅子", 5), word("先生", 5), word("本", 4)
        learner = self.learner("一人日本")
        for target in (chair, teacher, book):
            UserWord.objects.create(learner=learner, word=target, status=UserWord.LEARNING)
        # 椅子 is stage N1. 先生 is this stage, and 本 is made of known kanji (it was looked up with Tab): they stay.
        self.assertEqual(learner.release_warmups(teachable_words()), ["椅子"])
        self.assertEqual(learner.words.count(), 3)  # a dry run
        learner.release_warmups(teachable_words(), apply=True)
        self.assertEqual(set(learner.words.values_list("word__text", flat=True)), {"先生", "本"})

    def test_from_n2_the_next_sentence_is_prepared_while_this_one_is_typed(self):
        teacher, student = word("先生", 5), word("学生", 5)
        word("来る", 5, reading="くる")  # not met yet: the learner is on N5 (来 is next)
        learner = self.learner("一人日本")
        for target in (teacher, student):
            UserWord.objects.create(learner=learner, word=target, status=UserWord.LEARNING)
        typing = sentence("日本の先生です。", teacher)
        sentence("先生は日本人。", teacher)  # only grades 先生, which the sentence being typed grades: not next
        following = sentence("日本の学生です。", student)
        learner.current = {"sentence": typing.id, "review": [], "learning": [teacher.id]}
        learner.save()

        learner.prefetch()  # a beginner: picking is quick enough, nothing is prepared
        self.assertEqual(learner.upcoming, {})
        with patch.object(models, "PREFETCH_FROM_LEVEL", 5):
            learner.prefetch()
        self.assertEqual(learner.upcoming, {"after": typing.id, "sentence": following.id, "learn_new": True})

        learner.submit(typing.id, typing.text)
        payload = learner.next_sentence()
        self.assertEqual(payload["sentence_id"], following.id)
        self.assertTrue(payload["prefetch"])
        self.assertEqual(learner.current, {"sentence": following.id, "review": [], "learning": [student.id]})
        self.assertEqual(learner.upcoming, {})  # used once

    def test_a_prepared_sentence_that_no_longer_fits_is_dropped(self):
        student = word("学生", 5)
        learner = self.learner("一人日本")
        following = sentence("日本の学生です。", student)
        learner.recent = [123]
        learner.upcoming = {"after": 123, "sentence": following.id, "learn_new": True}
        self.assertIsNone(learner._take_upcoming([], []))  # 学生 isn't being warmed up any more: nothing to grade
        self.assertEqual(Learner.objects.get(id=learner.id).upcoming, {})

        learner.upcoming = {"after": 999, "sentence": following.id, "learn_new": True}  # made for another sentence
        self.assertIsNone(learner._take_upcoming([], [student.id]))
        learner.upcoming = {"after": 123, "sentence": following.id, "learn_new": True}
        self.assertEqual(learner._take_upcoming([], [student.id]), following)
