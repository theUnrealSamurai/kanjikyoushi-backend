import random
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone
from fsrs import Card, Rating, Scheduler

from .corpus import KANJI_RE, furigana, has_kanji, jlpt_kanji, kanji_dictionary, kanji_info, same_text


scheduler = Scheduler()

DEFAULT_KNOWN_KANJI = "一二三四五六七八九十百千万"
LEARNING_POOL = 5           # new words being warmed up at any one time
WARMUP_SIGHTINGS = 3        # a new word is typed this many times (with furigana) before reviews start
REVIEWS_PER_SENTENCE = 2    # at most this many due words are graded in one sentence
DUE_TARGETS = 6             # how many of the most overdue words to look for sentences for
CANDIDATES_PER_WORD = 200   # sentences sampled per target word when picking the next sentence
RECENT_SENTENCES = 50       # don't repeat a sentence typed this recently
DUE_LIST_LIMIT = 300        # most words the due list shows (the kanji list and the totals cover all due words)
REPORT_DAYS = (7, 365)      # fewest and most days the stats report covers
FORECAST_DAYS = 7           # days the due forecast looks ahead, today included
NOT_KANJI = "々〆ヶ"         # repeat marks: not kanji to learn (as in jlpt_level)


def load_zone(name):
    """The ZoneInfo for an IANA zone name like "Asia/Tokyo", or None if it isn't one.
    The name comes from a request header, so anything odd (paths, directories, junk) is rejected."""
    if not isinstance(name, str) or not name or len(name) > 64:
        return None
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError):
        return None


class Word(models.Model):
    """A word as written plus its reading, e.g. 食べる/たべる. This is the unit that gets learned and graded.
    The reading is part of the identity because 方 (ほう) and 方 (かた) are different things to learn."""

    text = models.CharField(max_length=64)
    reading = models.CharField(max_length=128)
    frequency = models.PositiveIntegerField(default=0, db_index=True)  # sentences using it; orders words within a level
    jlpt = models.PositiveSmallIntegerField(default=0)  # hardest kanji: 5 = N5 ... 1 = N1, 0 = beyond; sets the learning order
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
    learn_new = models.BooleanField(default=True)         # off = review mode: only due words, no new ones
    timezone = models.CharField(max_length=64, blank=True)  # IANA zone from the X-Timezone header: what "today" means

    def __str__(self):
        return self.user.username


    def zone(self):
        """The learner's timezone, UTC until the browser has told us one."""
        return load_zone(self.timezone) or UTC


    def onboard(self, kanji_list):
        """Store the kanji the user can already read and start their progress from scratch.
        Every word written only with those kanji is marked known, so it is never taught or graded."""
        known = "".join(dict.fromkeys(KANJI_RE.findall((kanji_list or "") + DEFAULT_KNOWN_KANJI)))
        self.words.all().delete()
        self.reviews.all().delete()
        self.sentence_logs.all().delete()
        self.known_kanji = known
        self.onboarded = True
        self.learn_new = True
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


    def due_words(self):
        """Review words due now, most overdue first. Only words that appear in a sentence, so each can come up."""
        return self.words.filter(status=UserWord.REVIEW, due__lte=timezone.now(), word__frequency__gt=0).order_by("due")


    def due_kanji(self, texts):
        """The distinct kanji in the given due words, in the order they first appear (most overdue first).
        The repeat marks 々〆ヶ aren't kanji to learn, so they're left out (as in jlpt_level)."""
        return list(dict.fromkeys(kanji for text in texts for kanji in KANJI_RE.findall(text) if kanji not in "々〆ヶ"))


    def due_list(self):
        """What is due, for the due page. Words: the DUE_LIST_LIMIT most overdue. Kanji: every distinct kanji
        across all due words (not just the listed ones) with how many due words contain it, in the order
        they first appear. The totals are the full counts. One query, no per-word lookups."""
        rows = list(self.due_words().values("word_id", "word__text", "word__reading", "word__meanings", "due"))
        kanji_dict, counts = kanji_dictionary(), {}
        for row in rows:
            for kanji in set(KANJI_RE.findall(row["word__text"])):
                counts[kanji] = counts.get(kanji, 0) + 1
        kanji = self.due_kanji(row["word__text"] for row in rows)
        return {
            "words": [
                {"id": row["word_id"], "text": row["word__text"], "reading": row["word__reading"],
                 "meanings": row["word__meanings"], "due": row["due"]}
                for row in rows[:DUE_LIST_LIMIT]
            ],
            "kanji": [
                {"kanji": k, "meanings": kanji_dict.get(k, {}).get("meanings", []), "words": counts[k]}
                for k in kanji
            ],
            "total_words": len(rows),
            "total_kanji": len(kanji),
        }


    def stats(self):
        texts = list(self.due_words().values_list("word__text", flat=True))
        today_start = datetime.combine(timezone.now().astimezone(self.zone()).date(), time.min, tzinfo=self.zone())
        return {
            "learned": self.words.filter(status=UserWord.REVIEW).count(),
            "learned_today": self.words.filter(learned_at__gte=today_start).count(),
            "due": len(texts),
            "due_kanji": len(self.due_kanji(texts)),
            "learning": self.words.filter(status=UserWord.LEARNING).count(),
            "learn_new": self.learn_new,
        }


    def report(self, days=30):
        """The stats page: per-day learning history over the last `days` days (every day present, oldest first,
        ending today), totals, streak, the due forecast and JLPT progress. Days are local dates in the learner's
        timezone. A word's learned day is its learned_at (null = learned before history was tracked, which counts
        in the totals but on no day). A kanji is learned in the app when it is in a learned word and wasn't known
        at onboarding; its day is the first of its words to be learned. The log rows are bucketed here, in a few
        queries, rather than asking the database for each day."""
        zone = self.zone()

        def local(moment):
            return moment.astimezone(zone).date()

        today = local(timezone.now())
        window = [today - timedelta(days=n) for n in range(days - 1, -1, -1)]

        # Learned words and the kanji they teach.
        word_days, before_words = Counter(), 0
        kanji_first, before_kanji = {}, set()
        known = set(self.known_kanji)
        learned_rows = list(self.words.filter(status=UserWord.REVIEW).values_list("word__text", "learned_at"))
        for text, learned_at in learned_rows:
            if learned_at is None:
                before_words += 1
            else:
                word_days[local(learned_at)] += 1
            for kanji in set(KANJI_RE.findall(text)) - known - set(NOT_KANJI):
                if learned_at is None:
                    before_kanji.add(kanji)
                elif kanji not in kanji_first or learned_at < kanji_first[kanji]:
                    kanji_first[kanji] = learned_at
        learned_kanji = before_kanji | set(kanji_first)
        kanji_days = Counter(local(at) for kanji, at in kanji_first.items() if kanji not in before_kanji)

        # History logs.
        review_rows = list(self.reviews.values_list("rating", "at"))
        sentence_times = list(self.sentence_logs.values_list("at", flat=True))
        review_days, hard_days, sentence_days = Counter(), Counter(), Counter(local(at) for at in sentence_times)
        for rating, at in review_rows:
            review_days[local(at)] += 1
            hard_days[local(at)] += rating < Rating.Good
        in_window = [rating for rating, at in review_rows if local(at) >= window[0]]

        # Cumulative totals: everything before the window, then day by day.
        total_words = before_words + sum(n for date, n in word_days.items() if date < window[0])
        total_kanji = len(before_kanji) + sum(n for date, n in kanji_days.items() if date < window[0])
        rows = []
        for date in window:
            total_words += word_days[date]
            total_kanji += kanji_days[date]
            rows.append({
                "date": date.isoformat(),
                "words": word_days[date],
                "kanji": kanji_days[date],
                "reviews": review_days[date],
                "hard": hard_days[date],
                "sentences": sentence_days[date],
                "total_words": total_words,
                "total_kanji": total_kanji,
            })

        # Streak: days in a row with a sentence typed, ending today (or yesterday if nothing typed yet today).
        typed = set(sentence_days)
        day = today if today in typed else today - timedelta(days=1)
        streak = 0
        while day in typed:
            streak, day = streak + 1, day - timedelta(days=1)

        # Forecast: learned words by the local day they fall due; overdue ones count as today.
        coming = Counter(
            max(local(due), today)
            for due in self.words.filter(status=UserWord.REVIEW, word__frequency__gt=0, due__isnull=False)
            .values_list("due", flat=True)
        )

        levels = jlpt_kanji()
        jlpt = []
        for level in range(5, 0, -1):
            in_level = {kanji for kanji, kanji_level in levels.items() if kanji_level == level}
            jlpt.append({
                "level": level,
                "total": len(in_level),
                "learned": len(in_level & learned_kanji),
                "known": len(in_level & known),
            })

        first_moments = [moment for moment in (
            min((at for _, at in review_rows), default=None),
            min(sentence_times, default=None),
            min((at for _, at in learned_rows if at), default=None),
        ) if moment]
        last = rows[-1]
        return {
            "today": {key: last[key] for key in ("words", "kanji", "reviews", "sentences")},
            "totals": {
                "words": len(learned_rows),
                "kanji": len(learned_kanji),
                "known_kanji": len(known),
                "learning": self.words.filter(status=UserWord.LEARNING).count(),
                "due": self.due_words().count(),
                "sentences": len(sentence_times),
                "reviews": len(review_rows),
                "retention": sum(rating >= Rating.Good for rating in in_window) / len(in_window) if in_window else None,
                "streak": streak,
            },
            "days": rows,
            "forecast": [
                {"date": (today + timedelta(days=n)).isoformat(), "due": coming[today + timedelta(days=n)]}
                for n in range(FORECAST_DAYS)
            ],
            "jlpt": jlpt,
            "tracking_since": local(min(first_moments)).isoformat() if first_moments else None,
        }


    def set_learn_new(self, learn_new):
        """Switch between learning new words and reviewing only. The sentence on screen is dropped, since it
        was picked for the other mode; nothing is graded until a sentence is submitted, so nothing is lost."""
        self.learn_new, self.current = learn_new, {}
        self.save(update_fields=["learn_new", "current"])


    def next_sentence(self):
        """Pick the next sentence to type and remember which of its words will be graded.

        Due review words come first (most overdue first), then the new words being warmed up.
        The best sentence covers the most of those while containing as few never-seen words as possible.
        While learning is on, every sentence has at least one warm-up word: only sentences containing one
        are eligible (falling back to all candidates if somehow none do).
        In review mode (learn_new off) no new words are brought in and the warm-up words are left alone:
        only due words are targeted, and None means nothing is due.
        A sentence that is already on screen and not yet submitted is returned again, so reloading
        the page doesn't skip it. Returns None when there is nothing left to learn or review.
        """
        pending = Sentence.objects.filter(id=self.current.get("sentence")).first() if self.current else None
        if pending:
            return self.sentence_payload(pending, dict(self.words.values_list("word_id", "status")))

        if self.learn_new:
            self._fill_learning_pool()
        due = list(self.due_words().values_list("word_id", flat=True)[:DUE_TARGETS])
        learning = list(self.words.filter(status=UserWord.LEARNING).values_list("word_id", flat=True)) if self.learn_new else []
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

        pool = sorted(candidates)
        if learning:
            with_learning = [sid for sid in pool if words_in[sid] & set(learning)]
            pool = with_learning or pool
        best, best_score = None, None
        for sentence_id in random.sample(pool, len(pool)):
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
        "unseen" (not taught yet, furigana shown), "known" (furigana hidden), None (kana/punctuation).
        A warm-up word that isn't graded in this sentence (review mode) counts as "unseen": furigana shown."""
        review, learning = set(self.current["review"]), set(self.current["learning"])

        def role(word_id):
            if word_id is None:
                return None
            if word_id in review:
                return "review"
            if word_id in learning:
                return "learning"
            if word_id not in statuses or statuses[word_id] == UserWord.LEARNING:
                return "unseen"
            return "known"

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
        # Names, numbers and counters have kanji but no word to learn. Tab still opens a card for them (reading and
        # kanji, never graded) instead of skipping ahead; a run of them shares one card, so 一振り reads as one word.
        extra_words, start = {}, None
        for index, (token, (surface, reading, *_)) in enumerate(zip(tokens, sentence.tokens)):
            if token["word"] is None and has_kanji(surface):
                start = index if start is None else start
                card = extra_words.setdefault(str(start), {"text": "", "reading": "", "meanings": [], "kanji": []})
                card["text"] += surface
                card["reading"] += reading
            else:
                start = None
            token["extra"] = start
        for card in extra_words.values():
            card["kanji"] = kanji_info(card["text"])
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
            "extra_words": extra_words,  # names, numbers and counters, keyed by token "extra"; never graded
            "stats": self.stats(),
        }


    def submit(self, sentence_id, typed, revealed=(), skipped=()):
        """Grade the sentence that was just typed. Typos never count: only whether you needed the reading.

        Review words: reading revealed (Tab or help mode) -> Hard, otherwise -> Good.
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

        now, revealed, skipped = timezone.now(), set(revealed), set(skipped)
        logs = []  # one ReviewLog per FSRS review of a learned word (not the graduation review)
        graded = list(self.words.filter(word_id__in=self.current["review"] + self.current["learning"]))
        for user_word in graded:
            if user_word.word_id in self.current["review"]:
                if user_word.word_id in skipped and user_word.word_id not in revealed:
                    continue
                rating = Rating.Hard if user_word.word_id in revealed else Rating.Good
                user_word.review(rating, now)
                logs.append(ReviewLog(learner=self, word_id=user_word.word_id, rating=rating, at=now))
            else:
                user_word.sightings += 1
                if user_word.sightings >= WARMUP_SIGHTINGS:
                    user_word.status = UserWord.REVIEW
                    user_word.review(Rating.Good, now)
                    user_word.learned_at = user_word.learned_at or now  # a word sent back to warm-up keeps its first date

        in_sentence = {token[2] for token in sentence.tokens if token[2]}
        looked_up = list(self.words.filter(
            word_id__in=(revealed & in_sentence) - set(self.current["review"] + self.current["learning"])
        ))
        for user_word in looked_up:
            if user_word.status == UserWord.KNOWN:
                user_word.status, user_word.sightings = UserWord.LEARNING, 0
            elif user_word.status == UserWord.REVIEW:
                user_word.review(Rating.Hard, now)
                logs.append(ReviewLog(learner=self, word_id=user_word.word_id, rating=Rating.Hard, at=now))
        UserWord.objects.bulk_update(graded + looked_up, ["status", "sightings", "card", "due", "learned_at"])
        ReviewLog.objects.bulk_create(logs)
        SentenceLog.objects.create(learner=self, at=now)

        self.recent = (self.recent + [sentence_id])[-RECENT_SENTENCES:]
        self.current = {}
        self.save(update_fields=["recent", "current"])


    def _fill_learning_pool(self):
        """Top the learning pool up with the next words the user hasn't met yet, in JLPT order: every N5 word
        before any N4 word and so on, most frequent first within a level.
        Words added to the corpus after onboarding are checked against the known kanji here."""
        missing = LEARNING_POOL - self.words.filter(status=UserWord.LEARNING).count()
        if missing <= 0:
            return

        known_set = set(self.known_kanji)
        new_rows = []
        unmet = (
            Word.objects.filter(frequency__gt=0)
            .exclude(id__in=UserWord.objects.filter(learner=self).values("word_id"))
            .order_by("-jlpt", "-frequency", "id").values_list("id", "text")
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
    learned_at = models.DateTimeField(null=True, blank=True)  # when the warm-up finished; null = before history was tracked

    class Meta:
        constraints = [models.UniqueConstraint(fields=["learner", "word"], name="unique_learner_word")]
        indexes = [models.Index(fields=["learner", "status", "due"])]

    def __str__(self):
        return f"{self.learner} · {self.word} · {self.status}"

    def review(self, rating, now):
        card = Card.from_dict(self.card) if self.card else Card()
        card, _ = scheduler.review_card(card, rating, now)
        self.card, self.due = card.to_dict(), card.due


class ReviewLog(models.Model):
    """One FSRS review of a learned word (the graduation review doesn't count), for the stats page."""

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="reviews")
    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name="+")
    rating = models.PositiveSmallIntegerField()  # FSRS Rating value: 1 Again, 2 Hard, 3 Good, 4 Easy
    at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["learner", "at"])]


class SentenceLog(models.Model):
    """One successfully typed sentence, for the stats page and the streak."""

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="sentence_logs")
    at = models.DateTimeField()

    class Meta:
        indexes = [models.Index(fields=["learner", "at"])]
