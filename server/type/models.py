import random
from collections import defaultdict

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from fsrs import Card, Rating, Scheduler

from .corpus import KANJI_RE, furigana, kanji_info, same_text


scheduler = Scheduler()

DEFAULT_KNOWN_KANJI = "一二三四五六七八九十百千万"
LEARNING_POOL = 5           # new words being warmed up at any one time
WARMUP_SIGHTINGS = 3        # a new word is typed this many times (with furigana) before reviews start
REVIEWS_PER_SENTENCE = 2    # at most this many due words are graded in one sentence
DUE_TARGETS = 6             # how many of the most overdue words to look for sentences for
CANDIDATES_PER_WORD = 200   # sentences sampled per target word when picking the next sentence
RECENT_SENTENCES = 50       # don't repeat a sentence typed this recently


class Word(models.Model):
    """A word as written plus its reading, e.g. 食べる/たべる. This is the unit that gets learned and graded.
    The reading is part of the identity because 方 (ほう) and 方 (かた) are different things to learn."""

    text = models.CharField(max_length=64)
    reading = models.CharField(max_length=128)
    frequency = models.PositiveIntegerField(default=0, db_index=True)  # sentences using it; sets the learning order
    meanings = models.JSONField(default=list, blank=True)  # English, one string per sense (up to 3), from JMdict

    class Meta:
        constraints = [models.UniqueConstraint(fields=["text", "reading"], name="unique_word")]

    def __str__(self):
        return f"{self.text} ({self.reading})"


class KanaWord(models.Model):
    """A word written in kana only, e.g. つぐむ or とりあえず. Tab shows its meaning; it is never learned or graded.
    Kept apart from Word so it can't end up in the learning queue, onboarding or FSRS."""

    text = models.CharField(max_length=64)    # dictionary form as written
    reading = models.CharField(max_length=128)
    lemma = models.CharField(max_length=64, blank=True)  # standard spelling, often kanji (噤む): finds the JMdict entry
    meanings = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["text", "reading", "lemma"], name="unique_kana_word")]

    def __str__(self):
        return f"{self.text} ({self.lemma})"


class Sentence(models.Model):
    text = models.TextField(unique=True)
    translation = models.TextField(blank=True)
    romaji = models.TextField(blank=True)
    tokens = models.JSONField()  # [[surface, reading, word_id or None, kana_word_id or None], ...] in sentence order
    words = models.ManyToManyField(Word, related_name="sentences")

    def __str__(self):
        return self.text


class Learner(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="learner")
    known_kanji = models.TextField(blank=True)  # kanji picked at onboarding; words made only of these are never taught
    onboarded = models.BooleanField(default=False)
    current = models.JSONField(default=dict, blank=True)  # the sentence on screen: {"sentence", "learning", "review"}
    recent = models.JSONField(default=list, blank=True)   # ids of recently typed sentences

    def __str__(self):
        return self.user.username


    def onboard(self, kanji_list):
        """Store the kanji the user can already read and start their progress from scratch.
        Every word written only with those kanji is marked known, so it is never taught or graded."""
        known = "".join(dict.fromkeys(KANJI_RE.findall((kanji_list or "") + DEFAULT_KNOWN_KANJI)))
        self.words.all().delete()
        self.known_kanji = known
        self.onboarded = True
        self.current, self.recent = {}, []
        self.save()

        known_set = set(known)
        UserWord.objects.bulk_create(
            [
                UserWord(learner=self, word_id=word_id, status=UserWord.KNOWN)
                for word_id, text in Word.objects.values_list("id", "text").iterator()
                if set(KANJI_RE.findall(text)) <= known_set
            ],
            batch_size=1000,
        )


    def stats(self):
        now = timezone.now()
        return {
            "learned": self.words.filter(status=UserWord.REVIEW).count(),
            "due": self.words.filter(status=UserWord.REVIEW, due__lte=now).count(),
            "learning": self.words.filter(status=UserWord.LEARNING).count(),
        }


    def next_sentence(self):
        """Pick the next sentence to type and remember which of its words will be graded.

        Due review words come first (most overdue first), then the new words being warmed up.
        The best sentence covers the most of those while containing as few never-seen words as possible.
        A sentence that is already on screen and not yet submitted is returned again, so reloading
        the page doesn't skip it. Returns None when there is nothing left to learn or review.
        """
        pending = Sentence.objects.filter(id=self.current.get("sentence")).first() if self.current else None
        if pending:
            return self.sentence_payload(pending, dict(self.words.values_list("word_id", "status")))

        self._fill_learning_pool()
        due = list(
            self.words.filter(status=UserWord.REVIEW, due__lte=timezone.now(), word__frequency__gt=0)
            .order_by("due").values_list("word_id", flat=True)[:DUE_TARGETS]
        )
        learning = list(self.words.filter(status=UserWord.LEARNING).values_list("word_id", flat=True))
        if not due and not learning:
            return None

        through = Sentence.words.through
        candidates = set()
        for word_id in due + learning:
            ids = list(through.objects.filter(word_id=word_id).values_list("sentence_id", flat=True)[:5000])
            candidates.update(random.sample(ids, min(len(ids), CANDIDATES_PER_WORD)))

        words_in = defaultdict(set)
        for sentence_id, word_id in through.objects.filter(sentence_id__in=candidates).values_list("sentence_id", "word_id"):
            words_in[sentence_id].add(word_id)
        statuses = dict(self.words.values_list("word_id", "status"))
        recent = set(self.recent)

        best, best_score = None, None
        for sentence_id in random.sample(sorted(candidates), len(candidates)):
            words = words_in[sentence_id]
            due_hits = [w for w in due if w in words][:REVIEWS_PER_SENTENCE]
            learning_hits = [w for w in learning if w in words]
            unseen = sum(1 for w in words if w not in statuses)
            score = 3 * len(due_hits) + 2 * min(len(learning_hits), 2) - 2 * unseen - 5 * (sentence_id in recent)
            if best_score is None or score > best_score:
                best, best_score = (sentence_id, due_hits, learning_hits), score

        sentence_id, review, learning = best
        self.current = {"sentence": sentence_id, "review": review, "learning": learning}
        self.save(update_fields=["current"])
        return self.sentence_payload(Sentence.objects.get(id=sentence_id), statuses)


    def sentence_payload(self, sentence, statuses):
        """The sentence as the typing page needs it: tokens with furigana and the role of each word.
        Roles: "review" (graded, furigana hidden until Tab), "learning" (new word, furigana shown),
        "unseen" (not taught yet, furigana shown), "known" (furigana hidden), None (kana/punctuation)."""
        review, learning = set(self.current["review"]), set(self.current["learning"])

        def role(word_id):
            if word_id is None:
                return None
            if word_id in review:
                return "review"
            if word_id in learning:
                return "learning"
            return "unseen" if word_id not in statuses else "known"

        kana_ids = {token[3] for token in sentence.tokens if len(token) > 3 and token[3]}
        kana_words = {
            str(kana.id): {"text": kana.text, "reading": kana.reading, "meanings": kana.meanings, "kanji": []}
            for kana in KanaWord.objects.filter(id__in=kana_ids).exclude(meanings=[])
        }
        tokens = [
            {
                "text": surface,
                "word": word_id,
                "role": role(word_id),
                "ruby": furigana(surface, reading),
                "lookup": rest[0] if rest and str(rest[0]) in kana_words else None,  # kana word with a meaning
            }
            for surface, reading, word_id, *rest in sentence.tokens
        ]
        # Dictionary data for the page's cards; the keys are word ids as strings (JSON object keys).
        graded = {t["word"] for t in tokens if t["word"]}  # every word, so Tab can open the card of whichever comes next
        words = {
            str(word.id): {
                "text": word.text,
                "reading": word.reading,
                "meanings": word.meanings,
                "kanji": kanji_info(word.text),
            }
            for word in Word.objects.filter(id__in=graded)
        }
        return {
            "sentence_id": sentence.id,
            "text": sentence.text,
            "translation": sentence.translation,
            "romaji": sentence.romaji,
            "tokens": tokens,
            "words": words,
            "kana_words": kana_words,  # meanings for kana-only words, shown by Tab without any grading
            "stats": self.stats(),
        }


    def submit(self, sentence_id, typed, revealed=(), missed=(), skipped=()):
        """Grade the sentence that was just typed.

        Review words: typed wrong -> Again, reading revealed (Tab or help mode) -> Hard, typed clean -> Good.
        Skipped review words (you asked how to type them: you know the reading, not the keystrokes) aren't
        graded at all and stay due, unless you had also revealed the reading.
        New words: count one more sighting; after WARMUP_SIGHTINGS they get an FSRS card and start reviews.
        Words you were assumed to know but looked up with Tab: a known word goes back to the warm-up
        (furigana shown again), a learned word that wasn't due is reviewed as Hard.
        """
        sentence = Sentence.objects.filter(id=sentence_id).first()
        if sentence is None or self.current.get("sentence") != sentence_id:
            raise ValueError("That is not the sentence currently being typed.")
        if not same_text(typed, sentence.text):
            raise ValueError("The typed sentence doesn't match.")

        now, revealed, missed, skipped = timezone.now(), set(revealed), set(missed), set(skipped)
        graded = list(self.words.filter(word_id__in=self.current["review"] + self.current["learning"]))
        for user_word in graded:
            if user_word.word_id in self.current["review"]:
                if user_word.word_id in skipped and user_word.word_id not in revealed:
                    continue
                if user_word.word_id in missed:
                    rating = Rating.Again
                elif user_word.word_id in revealed:
                    rating = Rating.Hard
                else:
                    rating = Rating.Good
                user_word.review(rating, now)
            else:
                user_word.sightings += 1
                if user_word.sightings >= WARMUP_SIGHTINGS:
                    user_word.status = UserWord.REVIEW
                    user_word.review(Rating.Good, now)

        in_sentence = {token[2] for token in sentence.tokens if token[2]}
        looked_up = list(self.words.filter(
            word_id__in=(revealed & in_sentence) - set(self.current["review"] + self.current["learning"])
        ))
        for user_word in looked_up:
            if user_word.status == UserWord.KNOWN:
                user_word.status, user_word.sightings = UserWord.LEARNING, 0
            elif user_word.status == UserWord.REVIEW:
                user_word.review(Rating.Hard, now)
        UserWord.objects.bulk_update(graded + looked_up, ["status", "sightings", "card", "due"])

        self.recent = (self.recent + [sentence_id])[-RECENT_SENTENCES:]
        self.current = {}
        self.save(update_fields=["recent", "current"])


    def _fill_learning_pool(self):
        """Top the learning pool up with the most frequent words the user hasn't met yet.
        Words added to the corpus after onboarding are checked against the known kanji here."""
        missing = LEARNING_POOL - self.words.filter(status=UserWord.LEARNING).count()
        if missing <= 0:
            return

        known_set = set(self.known_kanji)
        new_rows = []
        unmet = (
            Word.objects.filter(frequency__gt=0)
            .exclude(id__in=UserWord.objects.filter(learner=self).values("word_id"))
            .order_by("-frequency", "id").values_list("id", "text")
        )
        for word_id, text in unmet.iterator():
            known = set(KANJI_RE.findall(text)) <= known_set
            new_rows.append(UserWord(learner=self, word_id=word_id, status=UserWord.KNOWN if known else UserWord.LEARNING))
            missing -= not known
            if missing == 0:
                break
        UserWord.objects.bulk_create(new_rows, batch_size=1000, ignore_conflicts=True)  # two requests at once may add the same words


class UserWord(models.Model):
    KNOWN, LEARNING, REVIEW = "known", "learning", "review"
    STATUSES = [(KNOWN, "Known"), (LEARNING, "Learning"), (REVIEW, "Review")]

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="words")
    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=8, choices=STATUSES)
    sightings = models.PositiveSmallIntegerField(default=0)  # times typed while learning
    card = models.JSONField(null=True, blank=True)           # FSRS card, once reviews start
    due = models.DateTimeField(null=True, blank=True)         # copy of card.due so due words can be queried

    class Meta:
        constraints = [models.UniqueConstraint(fields=["learner", "word"], name="unique_learner_word")]
        indexes = [models.Index(fields=["learner", "status", "due"])]

    def __str__(self):
        return f"{self.learner} · {self.word} · {self.status}"

    def review(self, rating, now):
        card = Card.from_dict(self.card) if self.card else Card()
        card, _ = scheduler.review_card(card, rating, now)
        self.card, self.due = card.to_dict(), card.due
