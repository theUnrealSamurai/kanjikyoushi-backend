"""Paragraph mode: the N5 unlock, the toggle, and typing passages line by line."""
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Learner, Passage, PassageSentence, ReviewLog, Sentence, UserWord, Word, n5_kanji
from .tests import NO_DICTIONARY, build_corpus


LINES = [
    "先生は本を読んだ。",    # 先生 本 読む
    "学生は山へ行った。",    # 学生 山 行く
    "先生と学生は話した。",  # 先生 学生 話す
    "母は魚を買った。",      # 母 魚 買う
    "父は車で来た。",        # 父 車 来る
    "雨が降った。",          # 雨 降る
    "川の水は冷たい。",      # 川 水 冷たい
    "新しい本を買った。",    # 新しい 本 買う
    "犬が走った。",          # 犬 走る
    "空は青い。",            # 空 青い
    "花が咲いた。",          # 花 咲く
    "友達と駅で会った。",    # 友達 駅 会う
]
# The words with a kanji beyond N5: for a learner who knows every N5 kanji, these are the next words to learn.
WINDOW = ("降る", "冷たい", "犬", "走る", "青い", "咲く", "友達")
LOCKED = {"error": "Paragraphs unlock once every N5 kanji is covered."}


def role_words(tokens, role):
    """The words (as text) of the tokens with this role, in order."""
    ids = [t["word"] for t in tokens if t["role"] == role]
    texts = dict(Word.objects.filter(id__in=ids).values_list("id", "text"))
    return [texts[word_id] for word_id in ids]


@NO_DICTIONARY
class ParagraphTests(TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.txt", "\n".join(LINES))
        self.user = User.objects.create_user("taro", "taro@example.com", "a-long-password")
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def passage(self, *texts, only=True):
        """A passage of these corpus sentences, in order; only=True makes them passage-only (not standalone)."""
        passage = Passage.objects.create(
            source="aozora", work_id="18376", position=Passage.objects.count(), title="桃太郎", author="楠山正雄",
            url="https://www.aozora.gr.jp/cards/000329/card18376.html", licence="Public domain",
        )
        for position, text in enumerate(texts):
            PassageSentence.objects.create(passage=passage, sentence=Sentence.objects.get(text=text), position=position)
        if only:
            Sentence.objects.filter(text__in=texts).update(standalone=False)
        return passage

    def learner(self, due=(), learning=(), unseen=(), paragraphs=True, learn_new=True):
        """Onboarded with every N5 kanji (so unlocked), then progress reset: `due` words due since yesterday,
        `learning` words warming up, `unseen` words never met, every other word known.
        While learning is on, unseen words (from WINDOW) are the next words to learn, so a passage can bring them in."""
        self.client.post("/type/onboard", {"kanji_list": "".join(n5_kanji())}, format="json")
        learner = Learner.objects.get(user=self.user)
        learner.words.all().delete()
        past = timezone.now() - timedelta(days=1)
        UserWord.objects.bulk_create(
            UserWord(learner=learner, word=word, due=past if word.text in due else None,
                     status=UserWord.REVIEW if word.text in due else UserWord.LEARNING if word.text in learning
                     else UserWord.KNOWN)
            for word in Word.objects.exclude(text__in=unseen)
        )
        learner.paragraphs, learner.learn_new = paragraphs, learn_new
        learner.save()
        return learner

    def next(self, fresh=False):
        """POST /type/next; fresh=True drops the sentence on screen first, so a new pick is made."""
        if fresh:
            Learner.objects.filter(user=self.user).update(current={})
        return self.client.post("/type/next").json()

    def submit(self, payload):
        return self.client.post("/type/submit", {"sentence_id": payload["sentence_id"], "typed": payload["text"]},
                                format="json").json()

    def toggle(self, on):
        return self.client.post("/type/paragraphs", {"on": on}, format="json")

    def test_unlock_needs_every_n5_kanji_known_or_in_a_known_or_learned_word(self):
        n5 = n5_kanji()
        self.assertEqual(len(n5), 109)
        self.client.post("/type/onboard", {"kanji_list": "".join(n5 - set("先生雨"))}, format="json")
        learner = Learner.objects.get(user=self.user)

        def progress():
            stats = learner.stats()
            return stats["n5_covered"], stats["n5_total"], stats["paragraphs_unlocked"], stats["paragraphs"]

        self.assertEqual(progress(), (106, 109, False, False))
        teacher = UserWord.objects.create(learner=learner, word=Word.objects.get(text="先生"), status=UserWord.LEARNING)
        self.assertEqual(progress(), (106, 109, False, False))  # a warm-up word doesn't count
        teacher.status = UserWord.REVIEW
        teacher.save()
        self.assertEqual(progress(), (108, 109, False, False))
        UserWord.objects.create(learner=learner, word=Word.objects.get(text="雨"), status=UserWord.KNOWN)
        self.assertEqual(progress(), (109, 109, True, False))  # unlocked, but not switched on
        self.assertTrue(learner.paragraphs_unlocked())

    def test_toggle_validates_stays_locked_and_switches_modes(self):
        self.client.post("/type/onboard", {"kanji_list": ""}, format="json")
        for body in ({}, {"on": "true"}, {"on": 1}, {"on": None}):
            response = self.client.post("/type/paragraphs", body, format="json")
            self.assertEqual((response.status_code, response.json()), (400, {"error": "Expected 'on': true or false."}))
        response = self.toggle(True)
        self.assertEqual((response.status_code, response.json()), (400, LOCKED))
        self.assertFalse(Learner.objects.get(user=self.user).paragraphs)

        self.passage("学生は山へ行った。", "先生と学生は話した。")
        self.learner(learning=("学生", "父"), paragraphs=False)
        on = self.toggle(True).json()
        self.assertTrue(on["stats"]["paragraphs"])
        self.assertEqual(on["passage"]["index"], 0)
        off = self.toggle(False).json()
        self.assertEqual((off["text"], off["passage"], off["stats"]["paragraphs"]), ("父は車で来た。", None, False))

        # Locked while on (can't happen in practice): the passage on screen gives way to a single sentence.
        self.assertIsNotNone(self.toggle(True).json()["passage"])
        Learner.objects.filter(user=self.user).update(known_kanji="")
        payload = self.next()
        self.assertEqual((payload["text"], payload["passage"], payload["stats"]["paragraphs"]), ("父は車で来た。", None, False))

        self.client.force_authenticate(User.objects.create_user("jiro", password="a-long-password"))
        self.assertEqual(self.toggle(False).status_code, 409)  # not onboarded

    def test_sentence_mode_never_serves_passage_only_sentences(self):
        self.passage("先生は本を読んだ。", "先生と学生は話した。")  # the only sentences with 先生
        self.learner(learning=("先生", "父"), paragraphs=False)
        for _ in range(10):
            payload = self.next(fresh=True)
            self.assertEqual((payload["text"], payload["passage"]), ("父は車で来た。", None))
        self.learner(learning=("先生",), paragraphs=False)
        self.assertTrue(self.next()["done"])  # its only target is in passages alone: nothing to serve, no crash

    def test_a_passage_is_typed_line_by_line(self):
        first = self.passage("先生は本を読んだ。", "学生は山へ行った。", "先生と学生は話した。")
        second = self.passage("学生は山へ行った。", "父は車で来た。", only=False)
        self.learner(due=("先生",), learning=("学生",))

        payload = self.next()
        passage, lines = payload["passage"], payload["passage"]["lines"]
        self.assertEqual(set(payload), {"sentence_id", "text", "translation", "romaji", "tokens", "words",
                                        "kana_words", "extra_words", "stats", "passage"})
        self.assertEqual(set(passage), {"id", "title", "author", "source", "url", "licence", "index", "lines"})
        self.assertEqual(set(lines[0]), {"sentence_id", "text", "tokens"})
        self.assertEqual(
            (passage["id"], passage["index"], passage["title"], passage["author"], passage["source"], passage["licence"]),
            (first.id, 0, "桃太郎", "楠山正雄", "Aozora Bunko", "Public domain"),
        )
        self.assertEqual(passage["url"], first.url)
        self.assertEqual([line["text"] for line in lines], ["先生は本を読んだ。", "学生は山へ行った。", "先生と学生は話した。"])
        self.assertEqual((payload["sentence_id"], payload["text"]), (lines[0]["sentence_id"], lines[0]["text"]))
        self.assertEqual(lines[0]["tokens"], payload["tokens"])
        self.assertEqual((role_words(payload["tokens"], "review"), role_words(payload["tokens"], "learning")), (["先生"], []))
        self.assertEqual(set(payload["words"]), {str(t["word"]) for t in payload["tokens"] if t["word"]})
        # Lines still to come show the passage's targets: the review word stays hidden, the new word highlighted.
        self.assertEqual(role_words(lines[1]["tokens"], "learning"), ["学生"])
        self.assertEqual((role_words(lines[2]["tokens"], "review"), role_words(lines[2]["tokens"], "learning")),
                         (["先生"], ["学生"]))
        self.assertEqual(self.next(), payload)  # reloading returns the same line

        payload = self.submit(payload)
        self.assertEqual((payload["passage"]["index"], payload["text"]), (1, "学生は山へ行った。"))
        self.assertEqual((role_words(payload["tokens"], "review"), role_words(payload["tokens"], "learning")), ([], ["学生"]))
        self.assertEqual(self.next()["sentence_id"], payload["sentence_id"])

        payload = self.submit(payload)
        self.assertEqual(payload["passage"]["index"], 2)
        self.assertEqual(payload["passage"]["lines"][2]["tokens"], lines[2]["tokens"])

        payload = self.submit(payload)  # the last line: the passage is done and a new one is picked
        learner = Learner.objects.get(user=self.user)
        self.assertEqual(learner.recent_passages, [first.id])
        self.assertEqual((payload["passage"]["id"], payload["passage"]["index"]), (second.id, 0))

    def test_each_target_is_graded_once_on_its_first_line(self):
        self.passage("先生は本を読んだ。", "学生は山へ行った。", "先生と学生は話した。")
        learner = self.learner(due=("先生",), learning=("学生",))
        payload = self.next()
        payload = self.submit(self.submit(payload))
        self.assertEqual(payload["text"], "先生と学生は話した。")
        roles = {word: role for word, role in ((t["text"], t["role"]) for t in payload["tokens"]) if role}
        self.assertEqual((roles["先生"], roles["学生"]), ("known", "unseen"))  # both graded already: not again
        self.submit(payload)
        self.assertEqual(ReviewLog.objects.filter(learner=learner, word__text="先生").count(), 1)
        self.assertEqual(learner.words.get(word__text="学生").sightings, 1)

    def test_learn_mode_needs_a_new_word_and_review_mode_a_due_word(self):
        due_only = self.passage("雨が降った。", "川の水は冷たい。")
        learn_only = self.passage("母は魚を買った。", "新しい本を買った。")
        self.learner(due=("雨",), learning=("魚",))
        for _ in range(5):
            self.assertEqual(self.next(fresh=True)["passage"]["id"], learn_only.id)  # due_only has no new word

        mixed = self.passage("雨が降った。", "母は魚を買った。")
        Learner.objects.filter(user=self.user).update(recent_passages=[due_only.id])
        for _ in range(5):
            Learner.objects.filter(user=self.user).update(current={})
            payload = self.client.post("/type/mode", {"learn_new": False}, format="json").json()
            self.assertEqual(payload["passage"]["id"], mixed.id)  # learn_only has no due word
            self.assertEqual(role_words(payload["tokens"], "review"), ["雨"])
            self.assertEqual(role_words(payload["passage"]["lines"][1]["tokens"], "unseen"), ["魚"])  # left alone

    def test_a_passage_with_several_new_words_wins_and_brings_them_in(self):
        big = self.passage("犬が走った。", "空は青い。", "雨が降った。", "友達と駅で会った。", "花が咲いた。", "川の水は冷たい。",
                           "先生は本を読んだ。", "母は魚を買った。", "父は車で来た。", "先生と学生は話した。",
                           "新しい本を買った。", "学生は山へ行った。", only=False)  # all 7 window words in 28: 75% met
        self.passage("犬が走った。", "先生は本を読んだ。", "母は魚を買った。", "父は車で来た。", "新しい本を買った。",
                     "学生は山へ行った。", "先生と学生は話した。", only=False)  # better known (14 of 16), but 3 new words
        learner = self.learner(learning=("学生",), unseen=WINDOW)

        payload = self.next()
        self.assertEqual(payload["passage"]["id"], big.id)
        order = list(Word.objects.filter(text__in=WINDOW).order_by("-jlpt", "-frequency", "id").values_list("text", flat=True))
        new = {"学生", *order[:4]}  # the warm-up word first, then the window in the learning order: 5 in all
        self.assertEqual(set(learner.words.filter(status=UserWord.LEARNING).values_list("word__text", flat=True)), new)
        self.assertFalse(learner.words.filter(word__text__in=order[4:]).exists())  # the rest stay unmet
        lines = payload["passage"]["lines"]
        self.assertEqual({word for line in lines for word in role_words(line["tokens"], "learning")}, new)
        self.assertEqual({word for line in lines for word in role_words(line["tokens"], "unseen")}, set(order[4:]))

    def test_due_words_come_first_then_new_ones(self):
        two_due = self.passage("先生は本を読んだ。", "花が咲いた。", only=False)  # 先生 本 due, 咲く new
        one_due = self.passage("先生と学生は話した。", "犬が走った。", "空は青い。", "友達と駅で会った。", "母は魚を買った。",
                               only=False)  # 先生 due and 4 new words: a higher score
        self.passage("犬が走った。", "空は青い。", "友達と駅で会った。", "雨が降った。", "母は魚を買った。", "父は車で来た。",
                     "学生は山へ行った。", only=False)  # 5 new words and nothing due: the highest score
        self.learner(due=("先生", "本"), unseen=WINDOW)
        self.assertEqual(self.next()["passage"]["id"], two_due.id)
        Learner.objects.filter(user=self.user).update(current={}, recent_passages=[two_due.id])
        self.assertEqual(self.next()["passage"]["id"], one_due.id)  # no passage left with 2 due words: 1 will do

    def test_learning_needs_two_new_words_when_a_passage_has_them(self):
        one_new = self.passage("先生は本を読んだ。", "父は車で来た。", "花が咲いた。", only=False)  # 5 due + 咲く: the higher score
        two_new = self.passage("先生は本を読んだ。", "犬が走った。", "母は魚を買った。", only=False)  # 3 due + 犬 走る
        self.learner(due=("先生", "本", "読む", "父", "車"), unseen=WINDOW)
        payload = self.next()
        self.assertEqual(payload["passage"]["id"], two_new.id)
        self.assertEqual({word for line in payload["passage"]["lines"] for word in role_words(line["tokens"], "learning")},
                         {"犬", "走る"})

        Learner.objects.filter(user=self.user).update(current={}, recent_passages=[two_new.id])
        self.assertEqual(self.next()["passage"]["id"], one_new.id)  # no passage left with 2: 1 will do

    def test_several_due_words_are_graded_across_lines(self):
        self.passage("先生は本を読んだ。", "母は魚を買った。", "父は車で来た。")
        learner = self.learner(due=("先生", "本", "魚", "車"), learn_new=False)
        payload, graded = self.next(), []
        while payload.get("passage"):
            graded.append(role_words(payload["tokens"], "review"))
            payload = self.submit(payload)
        self.assertEqual(graded, [["先生", "本"], ["魚"], ["車"]])
        self.assertTrue(payload["done"])  # nothing else is due
        self.assertEqual(sorted(ReviewLog.objects.filter(learner=learner).values_list("word__text", flat=True)),
                         sorted(["先生", "本", "魚", "車"]))

    def test_review_mode_brings_in_no_new_words(self):
        self.passage("犬が走った。", "雨が降った。", "母は魚を買った。", "父は車で来た。", "先生は本を読んだ。", only=False)
        learner = self.learner(due=("雨",), unseen=WINDOW, learn_new=False)  # 犬 走る 降る unmet: 10 of 13 met
        before = learner.words.count()
        lines = self.next()["passage"]["lines"]
        self.assertEqual({word for line in lines for word in role_words(line["tokens"], "review")}, {"雨"})
        self.assertEqual({word for line in lines for word in role_words(line["tokens"], "learning")}, set())
        self.assertEqual({word for line in lines for word in role_words(line["tokens"], "unseen")}, {"犬", "走る", "降る"})
        self.assertEqual(learner.words.count(), before)

    def test_coverage_floor_and_the_better_known_passage_wins(self):
        low = self.passage("雨が降った。", "川の水は冷たい。", only=False)  # 雨 降る 川 水 冷たい
        self.learner(due=("雨",), unseen=("川", "水", "冷たい"), learn_new=False)  # 2 of 5 met: below 60%
        for _ in range(5):
            payload = self.next(fresh=True)
            self.assertEqual((payload["text"], payload["passage"]), ("雨が降った。", None))  # a single sentence instead

        better = self.passage("雨が降った。", "母は魚を買った。", only=False)  # every word met
        self.learner(due=("雨",), unseen=("冷たい",), learn_new=False)  # low is at 80% now
        for _ in range(5):
            self.assertEqual(self.next(fresh=True)["passage"]["id"], better.id)  # same hits and length: 100% wins

        self.learner(due=("雨",), unseen=("川", "冷たい"), learn_new=False)  # low at 60%: right at the floor
        Learner.objects.filter(user=self.user).update(recent_passages=[better.id])
        payload = self.next()
        self.assertEqual(payload["passage"]["id"], low.id)
        self.assertEqual(role_words(payload["passage"]["lines"][1]["tokens"], "unseen"), ["川", "冷たい"])  # furigana, not graded

    def test_onboarding_resets_paragraph_mode(self):
        learner = self.learner()
        learner.recent_passages = [1, 2]
        learner.save()
        self.client.post("/type/onboard", {"kanji_list": ""}, format="json")
        learner.refresh_from_db()
        self.assertEqual((learner.paragraphs, learner.recent_passages), (False, []))
