import random
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache, reduce
from operator import or_
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection, models
from django.db.models import Q
from django.utils import timezone
from fsrs import Card, Rating, Scheduler

from .corpus import KANJI_RE, furigana, has_kanji, jlpt_kanji, kanji_dictionary, kanji_info, same_text


scheduler = Scheduler()

DEFAULT_KNOWN_KANJI = "一二三四五六七八九十百千万"
LEARNING_POOL = 10          # new words being warmed up at any one time: enough for most sentences to hold NEW_PER_SENTENCE
WARMUP_SIGHTINGS = 3        # a new word is typed this many times (with furigana) before reviews start, unless it passes its test
NEW_PER_SENTENCE = 2        # at most this many new words are graded in one sentence (or passage line); every due word is
DUE_TARGETS = 12            # how many of the most overdue words to look for sentences for
CANDIDATES_PER_WORD = 200   # sentences sampled per target word when picking the next sentence
RECENT_SENTENCES = 50       # don't repeat a sentence typed this recently
DUE_LIST_LIMIT = 300        # most words the due list shows (the kanji list and the totals cover all due words)
REPORT_DAYS = (7, 365)      # fewest and most days the stats report covers
FORECAST_DAYS = 7           # days the due forecast looks ahead, today included
NOT_KANJI = "々〆ヶ"         # repeat marks: not kanji to learn (as in jlpt_level)
PASSAGE_DUE_TARGETS = 30    # how many of the most overdue words to look for passages for: a passage has room for many
NEW_WORD_WINDOW = 40        # paragraph mode's new words come from the next this-many words in the learning order, so a
                            # passage can bring in several at once: new words stay in JLPT order, roughly (within the window)
NEW_PER_PASSAGE = 5         # at most this many new words are graded in one passage: warm-up words first, then window words
MIN_DUE_PER_PASSAGE = 2     # due words first: a passage needs this many if any candidate has them (else 1 if any has 1)
MIN_NEW_PER_PASSAGE = 2     # while learning, then this many new words if any candidate left has them (always at least 1)
PASSAGE_SHORTLIST = 3000    # the passages with the most target words get their coverage checked when picking one
PASSAGE_COVERAGE = 0.60     # minimum share of a passage's graded words the learner knows, is learning or has learned
PASSAGE_TARGET_WEIGHT = 6   # score points per due or new word a passage grades (every due word, up to 5 new)
PASSAGE_COVERAGE_WEIGHT = 40  # score points for that share. Over the 60-100% a passage can have that is at most 16, so
                              # 3 more target words (18) always win, while 20% more coverage (8) beats 1 more word (6)
PASSAGE_LENGTH_BONUS = 10   # each line of a passage scores a point, up to this many (so at most 9 apart): bigger is better
RECENT_PASSAGES = 50        # don't repeat a passage finished this recently
SOURCE_NAMES = {"aozora": "Aozora Bunko"}  # Passage.source -> the name in the credit line


def line_targets(words, targets, assigned):
    """(review, learning): the targets a passage line grades, from the word ids in it (`words`), the passage's
    {"review", "learning"} targets and the ones earlier lines took (`assigned`). A line grades every target in it
    that no earlier line took, so each is graded once, on its first line, but at most NEW_PER_SENTENCE new words:
    one beyond that waits for its next line."""
    free = words - set(assigned)
    return [w for w in targets["review"] if w in free], [w for w in targets["learning"] if w in free][:NEW_PER_SENTENCE]


def passage_line_words(passage_id):
    """The word ids in each line of the passage, in order."""
    lines = defaultdict(set)
    for position, word_id in PassageSentence.objects.filter(passage_id=passage_id).values_list("position", "sentence__words"):
        lines[position] |= {word_id} - {None}
    return [lines[position] for position in sorted(lines)]


def graded_new_words(lines, new):
    """The new words (in order) that a line will grade, as line_targets assigns them; the rest are dropped."""
    assigned = []
    for words in lines:
        assigned += line_targets(words, {"review": [], "learning": new}, assigned)[1]
    return [w for w in new if w in assigned]


def made_of_known_kanji(text, known):
    """Whether every kanji in the word is in `known` (a set). The repeat marks 々〆ヶ don't count: 人々 is known with 人."""
    return set(KANJI_RE.findall(text)) - set(NOT_KANJI) <= known


@lru_cache(maxsize=1)
def n5_kanji():
    """The N5 kanji (the onboarding page's list): paragraph mode unlocks once every one of them is covered."""
    return frozenset(kanji for kanji, level in jlpt_kanji().items() if level == 5)


def word_role(word_id, review, learning, statuses, testing=frozenset()):
    """A token's role: "review" (graded, furigana hidden until Tab), "learning" (new word, furigana shown),
    "test" (a new word on its first sighting, furigana hidden until Tab: typed without help, it skips the warm-up),
    "unseen" (not taught yet, furigana shown), "known" (furigana hidden), None (kana/punctuation).
    A warm-up word that isn't graded here (review mode, or graded on an earlier line) counts as "unseen"."""
    if word_id is None:
        return None
    if word_id in review:
        return "review"
    if word_id in learning:
        return "test" if word_id in testing else "learning"
    if word_id not in statuses or statuses[word_id] == UserWord.LEARNING:
        return "unseen"
    return "known"


def sentence_tokens(sentence, review, learning, statuses, kana_with_meanings, testing=frozenset()):
    """The sentence's tokens for the typing page: furigana pairs, the role of each word (see word_role) and, for a
    kana-only word with a meaning (an id in kana_with_meanings), its lookup id for Tab."""
    tokens = [
        {
            "text": surface,
            "word": word_id,
            "role": word_role(word_id, review, learning, statuses, testing),
            "ruby": furigana(surface, reading),
            "lookup": rest[0] if rest and rest[0] in kana_with_meanings else None,
        }
        for surface, reading, word_id, *rest in sentence.tokens
    ]
    # Names, numbers and counters have kanji but no word to learn. Tab still opens a card for them (reading and
    # kanji, never graded) instead of skipping ahead; a run of them shares one card, so 一振り reads as one word.
    start = None
    for index, token in enumerate(tokens):
        if token["word"] is None and has_kanji(token["text"]):
            start = index if start is None else start
        else:
            start = None
        token["extra"] = start
    return tokens


def extra_cards(sentence, tokens):
    """The Tab cards for the runs of names, numbers and counters that sentence_tokens marked, keyed by token "extra"."""
    cards = {}
    for token, (surface, reading, *_) in zip(tokens, sentence.tokens):
        if token["extra"] is not None:
            card = cards.setdefault(str(token["extra"]), {"text": "", "reading": "", "meanings": [], "kanji": []})
            card["text"] += surface
            card["reading"] += reading
    for card in cards.values():
        card["kanji"] = kanji_info(card["text"])
    return cards


def kana_ids(sentence):
    return {token[3] for token in sentence.tokens if len(token) > 3 and token[3]}


def passage_hits(word_ids):
    """(passage id, word id) for each of these words in each passage that has it.
    Raw SQL, because the plan matters: "+ 0" stops the database from starting at the word index, which would visit
    every sentence in the corpus with a common word (hundreds of thousands); instead it reads the passages' lines
    and their words from the covering index, skipping the wide Sentence table. On the dev corpus (26k passages,
    1M sentences) that is ~60 ms warm and ~170 ms cold, against ~0.1 s and ~0.8 s (2 s without "+ 0") for the ORM's join."""
    if not word_ids:
        return []
    marks = ", ".join(["%s"] * len(word_ids))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT DISTINCT ps.passage_id, sw.word_id FROM {PassageSentence._meta.db_table} ps "
            f"JOIN {Sentence.words.through._meta.db_table} sw ON sw.sentence_id = ps.sentence_id "
            f"WHERE sw.word_id + 0 IN ({marks})",
            list(word_ids),
        )
        return cursor.fetchall()


def passage_sizes(learner_id, passage_ids):
    """{passage id: (lines, distinct list words, list words the learner has met)}, counted by the database.
    Only words on the JLPT list count: the others are never taught, so they're like names.
    Raw SQL for the same reason as passage_hits: ~30 ms for 3,000 passages against ~160 ms through the ORM."""
    if not passage_ids:
        return {}
    marks = ", ".join(["%s"] * len(passage_ids))
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT ps.passage_id, COUNT(DISTINCT ps.id), COUNT(DISTINCT w.id), COUNT(DISTINCT uw.word_id) "
            f"FROM {PassageSentence._meta.db_table} ps "
            f"LEFT JOIN {Sentence.words.through._meta.db_table} sw ON sw.sentence_id = ps.sentence_id "
            f"LEFT JOIN {Word._meta.db_table} w ON w.id = sw.word_id AND w.list_level IS NOT NULL "
            f"LEFT JOIN {UserWord._meta.db_table} uw ON uw.word_id = w.id AND uw.learner_id = %s "
            f"WHERE ps.passage_id IN ({marks}) GROUP BY ps.passage_id",
            [learner_id, *passage_ids],
        )
        return {passage_id: sizes for passage_id, *sizes in cursor.fetchall()}


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
    jlpt = models.PositiveSmallIntegerField(default=0)  # hardest kanji: 5 = N5 ... 1 = N1, 0 = beyond
    # Level on the JLPT vocabulary list (assets/jlpt_vocab: 5 = N5 ... 1 = N1); sets the learning order.
    # None: not on the list, so never taught or graded (like a name: furigana and a Tab card only).
    list_level = models.PositiveSmallIntegerField(null=True, blank=True)
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
    standalone = models.BooleanField(default=True, db_default=True)  # False: only in passages, so sentence mode never serves it out of context

    def __str__(self):
        return self.text


class Passage(models.Model):
    """A paragraph of a longer work (Aozora Bunko), typed one sentence at a time in paragraph mode.
    Its sentences are shared Sentence rows, so a line that's already in the corpus isn't stored twice."""

    source = models.CharField(max_length=32)           # e.g. "aozora"
    work_id = models.CharField(max_length=32)          # the source's id for the work
    position = models.PositiveIntegerField()           # paragraph number within the work
    title = models.CharField(max_length=255)
    author = models.CharField(max_length=255)
    url = models.URLField(max_length=500, blank=True)  # where the work can be read: the credit line links to it
    licence = models.CharField(max_length=64)          # shown in the credit line, e.g. "Public domain"
    readability = models.FloatField(null=True)         # jReadability of the whole work: higher is easier
    sentences = models.ManyToManyField(Sentence, through="PassageSentence", related_name="passages")

    class Meta:
        constraints = [models.UniqueConstraint(fields=["source", "work_id", "position"], name="unique_passage")]

    def __str__(self):
        return f"{self.title} ({self.author}) ¶{self.position}"


class PassageSentence(models.Model):
    passage = models.ForeignKey(Passage, on_delete=models.CASCADE, related_name="lines")
    sentence = models.ForeignKey(Sentence, on_delete=models.CASCADE, related_name="+")
    position = models.PositiveSmallIntegerField()  # 0-based order within the passage

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["passage", "position"], name="unique_passage_sentence")]


class Learner(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="learner")
    known_kanji = models.TextField(blank=True)  # kanji picked at onboarding; words made only of these are never taught
    onboarded = models.BooleanField(default=False)
    current = models.JSONField(default=dict, blank=True)  # the sentence on screen: {"sentence", "learning", "review"}; in a passage also {"passage", "index", "targets", "assigned"}
    recent = models.JSONField(default=list, blank=True)   # ids of recently typed sentences
    learn_new = models.BooleanField(default=True)         # off = review mode: only due words, no new ones
    paragraphs = models.BooleanField(default=False)       # paragraph mode, once unlocked: type passages, not single sentences
    recent_passages = models.JSONField(default=list, blank=True)  # ids of recently typed passages
    timezone = models.CharField(max_length=64, blank=True)  # IANA zone from the X-Timezone header: what "today" means

    def __str__(self):
        return self.user.username


    def zone(self):
        """The learner's timezone, UTC until the browser has told us one."""
        return load_zone(self.timezone) or UTC


    def onboard(self, kanji_list):
        """Store the kanji the user can already read and start their progress from scratch (sentence mode,
        learning new words). Every list word written only with those kanji (repeat marks like 々 aside) is marked
        known, so it is never taught or graded. Words not on the JLPT list never get a row: they're never taught."""
        known = "".join(dict.fromkeys(KANJI_RE.findall((kanji_list or "") + DEFAULT_KNOWN_KANJI)))
        self.words.all().delete()
        self.reviews.all().delete()
        self.sentence_logs.all().delete()
        self.known_kanji = known
        self.onboarded = True
        self.learn_new = True
        self.paragraphs = False
        self.current, self.recent, self.recent_passages = {}, [], []
        self.save()

        known_set = set(known)
        UserWord.objects.bulk_create(
            [
                UserWord(learner=self, word_id=word_id, status=UserWord.KNOWN)
                for word_id, text in Word.objects.filter(list_level__isnull=False).values_list("id", "text").iterator()
                if made_of_known_kanji(text, known_set)
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


    def n5_progress(self):
        """(covered, total) N5 kanji. A kanji is covered when it was known at onboarding or is in a word you know
        or have learned (status KNOWN or REVIEW; warm-up words don't count). Runs on every request, so the database
        is only asked about the N5 kanji not known at onboarding, and only returns words containing one of them."""
        n5 = n5_kanji()
        missing = n5 - set(self.known_kanji)
        if missing:
            texts = self.words.filter(
                reduce(or_, (Q(word__text__contains=kanji) for kanji in missing)),
                status__in=(UserWord.KNOWN, UserWord.REVIEW),
            ).values_list("word__text", flat=True)
            missing -= {kanji for text in texts for kanji in text}
        return len(n5) - len(missing), len(n5)


    def paragraphs_unlocked(self):
        """Paragraph mode unlocks once every N5 kanji is covered (see n5_progress)."""
        covered, total = self.n5_progress()
        return covered == total


    def stats(self):
        texts = list(self.due_words().values_list("word__text", flat=True))
        today_start = datetime.combine(timezone.now().astimezone(self.zone()).date(), time.min, tzinfo=self.zone())
        n5_covered, n5_total = self.n5_progress()
        return {
            "learned": self.words.filter(status=UserWord.REVIEW).count(),
            "learned_today": self.words.filter(learned_at__gte=today_start).count(),
            "due": len(texts),
            "due_kanji": len(self.due_kanji(texts)),
            "learning": self.words.filter(status=UserWord.LEARNING).count(),
            "learn_new": self.learn_new,
            "paragraphs": self.paragraphs and n5_covered == n5_total,  # only ever true once unlocked
            "paragraphs_unlocked": n5_covered == n5_total,
            "n5_covered": n5_covered,
            "n5_total": n5_total,
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


    def set_paragraphs(self, on):
        """Turn paragraph mode on or off. It can only be turned on once unlocked (ValueError otherwise). As with
        set_learn_new, the sentence or passage on screen is dropped; the lines already typed were graded."""
        if on and not self.paragraphs_unlocked():
            raise ValueError("Paragraphs unlock once every N5 kanji is covered.")
        self.paragraphs, self.current = on, {}
        self.save(update_fields=["paragraphs", "current"])


    def next_sentence(self):
        """Pick the next sentence to type and remember which of its words will be graded.

        Due review words come first (most overdue first), then the new words being warmed up.
        In review mode (learn_new off) no new words are brought in and the warm-up words are left alone:
        only due words are targeted, and None means nothing is due.
        In paragraph mode (on and unlocked) a whole passage is picked (see _pick_passage) and typed line by line;
        it brings in its own new words, so the learning pool isn't topped up first. When no passage qualifies,
        a single sentence is served instead, so practice never dead-ends.
        Otherwise a single sentence is picked (see _pick_sentence).
        A sentence that is already on screen and not yet submitted is returned again, so reloading
        the page doesn't skip it; in a passage that is the same line. Returns None when there is nothing left
        to learn or review.
        """
        pending = Sentence.objects.filter(id=self.current.get("sentence")).first() if self.current else None
        if pending and self.current.get("passage") and not (self.paragraphs and self.paragraphs_unlocked()):
            pending = None  # a passage line, but paragraph mode is off or locked: start afresh with a single sentence
        if pending:
            return self.sentence_payload(pending, dict(self.words.values_list("word_id", "status")))

        if self.paragraphs and self.paragraphs_unlocked():
            sentence = self._pick_passage()
            if sentence:
                return self.sentence_payload(sentence, dict(self.words.values_list("word_id", "status")))

        if self.learn_new:
            self._fill_learning_pool()
        due = list(self.due_words().values_list("word_id", flat=True))  # most overdue first
        learning = list(self.words.filter(status=UserWord.LEARNING).order_by("-word__list_level", "-word__frequency", "word_id")
                        .values_list("word_id", flat=True)) if self.learn_new else []
        if not due and not learning:
            return None
        statuses = dict(self.words.values_list("word_id", "status"))
        sentence = self._pick_sentence(due, learning, statuses)
        return self.sentence_payload(sentence, statuses) if sentence else None


    def _pick_sentence(self, due, learning, statuses):
        """Pick a single sentence, set it as current and return it (None if no sentence has a target word).

        due: every due word, most overdue first; sentences are looked for around the first DUE_TARGETS of them.
        The best sentence covers the most target words while containing as few never-seen words as possible.
        A sentence grades every due word in it and up to NEW_PER_SENTENCE warm-up words (in the learning order),
        so one with more of them scores higher. While learning is on, every sentence has at least one warm-up word:
        only sentences containing one are eligible (falling back to all candidates if somehow none do). Only
        standalone sentences are served: passage-only lines would make no sense out of context.
        """
        through = Sentence.words.through
        candidates = set()
        for word_id in due[:DUE_TARGETS] + learning:
            ids = list(
                through.objects.filter(word_id=word_id, sentence__standalone=True).values_list("sentence_id", flat=True)[:5000]
            )
            candidates.update(random.sample(ids, min(len(ids), CANDIDATES_PER_WORD)))
        if not candidates:
            return None  # the target words only appear in passages

        words_in = defaultdict(set)
        for sentence_id, word_id in through.objects.filter(sentence_id__in=candidates).values_list("sentence_id", "word_id"):
            words_in[sentence_id].add(word_id)
        recent, due_rank = set(self.recent), {word_id: rank for rank, word_id in enumerate(due)}

        pool = sorted(candidates)
        if learning:
            with_learning = [sid for sid in pool if words_in[sid] & set(learning)]
            pool = with_learning or pool
        best, best_score = None, None
        for sentence_id in random.sample(pool, len(pool)):
            words = words_in[sentence_id]
            due_hits = sorted((w for w in words if w in due_rank), key=due_rank.get)
            learning_hits = [w for w in learning if w in words][:NEW_PER_SENTENCE]
            unseen = sum(1 for w in words if w not in statuses)
            score = 3 * len(due_hits) + 2 * len(learning_hits) - 2 * unseen - 5 * (sentence_id in recent)
            if best_score is None or score > best_score:
                best, best_score = (sentence_id, due_hits, learning_hits), score

        sentence_id, review, learning = best
        self.current = {"sentence": sentence_id, "review": review, "learning": learning}
        self.save(update_fields=["current"])
        return Sentence.objects.get(id=sentence_id)


    def _pick_passage(self):
        """Pick a passage, bring in its new words, set its first line as current and return that line's sentence
        (None if no passage qualifies).

        Target words: the PASSAGE_DUE_TARGETS most overdue words and, while learning is on, the new words: the warm-up
        words plus the next NEW_WORD_WINDOW words in the learning order (see _new_word_window), so new words still
        come in JLPT order, roughly: from within the window. In review mode there are no new words.
        Every passage with a target word is a candidate (recently finished ones are skipped); the PASSAGE_SHORTLIST
        ranked first by the rules below get their coverage checked. Coverage is the share of a passage's distinct
        list words (the only ones ever graded) the learner has met (known, learning or learned); window words aren't
        met until they're brought in, and unmet words show furigana ("unseen") and are never graded.
        Due words come first ("finish your due words before learning more"). The rules, in this order:
        (a) coverage of at least PASSAGE_COVERAGE;
        (b) while learning, at least one new word: every passage teaches something;
        (c) MIN_DUE_PER_PASSAGE due words if any passage left has that many, else 1 if any has one;
        (d) while learning, MIN_NEW_PER_PASSAGE new words if any passage left has that many.
        Of the passages left, the best has the most target words, then the best coverage, then the most lines
        (see the weights' comments).
        The chosen passage grades every due word in it and up to NEW_PER_PASSAGE new words: warm-up words first,
        then window words in the learning order, which become warm-up words now. Each target is graded once, on its
        first line, with at most NEW_PER_SENTENCE new words on a line (see line_targets): a new word whose lines are
        all full is left out.
        """
        due = list(self.due_words().values_list("word_id", flat=True))  # most overdue first
        learning, window = [], []
        if self.learn_new:
            learning = list(self.words.filter(status=UserWord.LEARNING)
                            .order_by("-word__list_level", "-word__frequency", "word_id").values_list("word_id", flat=True))
            window = self._new_word_window()
        due_rank = {word_id: rank for rank, word_id in enumerate(due)}
        new_rank = {word_id: rank for rank, word_id in enumerate(learning + window)}  # warm-up words first
        targets = due[:PASSAGE_DUE_TARGETS] + learning + window
        if not targets:
            return None

        # The target words in every passage that has any, in one query.
        recent, hits = set(self.recent_passages), defaultdict(set)
        for passage_id, word_id in passage_hits(targets):
            if passage_id not in recent:
                hits[passage_id].add(word_id)
        counts = {
            passage_id: (sum(w in due_rank for w in words), min(sum(w in new_rank for w in words), NEW_PER_PASSAGE))
            for passage_id, words in hits.items()
        }
        # (b) first, as it needs no coverage; then rank as (c) and (d) will filter, so the shortlist has those passages.
        shortlist = [passage_id for passage_id, (due_n, new_n) in counts.items() if (new_n if self.learn_new else due_n)]
        random.shuffle(shortlist)  # ties are broken at random (the sort is stable)
        shortlist.sort(reverse=True, key=lambda passage_id: (
            min(counts[passage_id][0], MIN_DUE_PER_PASSAGE),
            min(counts[passage_id][1], MIN_NEW_PER_PASSAGE) if self.learn_new else 0,
            sum(counts[passage_id]),
        ))

        scored = []  # (score, tie-break, passage id, due words, new words)
        for passage_id, (lines, words, met) in passage_sizes(self.id, shortlist[:PASSAGE_SHORTLIST]).items():
            coverage = met / words if words else 0
            if coverage < PASSAGE_COVERAGE:  # (a)
                continue
            due_n, new_n = counts[passage_id]
            score = (PASSAGE_TARGET_WEIGHT * (due_n + new_n) + PASSAGE_COVERAGE_WEIGHT * coverage
                     + min(lines, PASSAGE_LENGTH_BONUS))
            scored.append((score, random.random(), passage_id, due_n, new_n))
        enough_due = min(max((entry[3] for entry in scored), default=0), MIN_DUE_PER_PASSAGE)  # (c)
        scored = [entry for entry in scored if entry[3] >= enough_due]
        if self.learn_new:
            enough_new = min(max((entry[4] for entry in scored), default=0), MIN_NEW_PER_PASSAGE)  # (d)
            scored = [entry for entry in scored if entry[4] >= enough_new]
        if not scored:
            return None
        passage_id = max(scored)[2]

        lines = passage_line_words(passage_id)
        words = set().union(*lines)
        review = sorted((w for w in words if w in due_rank), key=due_rank.get)
        new = graded_new_words(lines, sorted((w for w in words if w in new_rank), key=new_rank.get)[:NEW_PER_PASSAGE])
        warming_up = set(learning)
        UserWord.objects.bulk_create(
            [UserWord(learner=self, word_id=w, status=UserWord.LEARNING) for w in new if w not in warming_up],
            ignore_conflicts=True,
        )
        self.current = self._passage_line(passage_id, 0, {"review": review, "learning": new}, [])
        self.save(update_fields=["current"])
        return Sentence.objects.get(id=self.current["sentence"])


    def _new_word_window(self):
        """The next NEW_WORD_WINDOW words to learn, in the learning order (see _fill_learning_pool), for paragraph
        mode to bring in. Words met on the way that are made only of known kanji become known, as there."""
        known_set, window, known_rows = set(self.known_kanji), [], []
        for word_id, text in self._unmet_words().iterator():
            if made_of_known_kanji(text, known_set):
                known_rows.append(UserWord(learner=self, word_id=word_id, status=UserWord.KNOWN))
            else:
                window.append(word_id)
                if len(window) == NEW_WORD_WINDOW:
                    break
        UserWord.objects.bulk_create(known_rows, batch_size=1000, ignore_conflicts=True)
        return window


    def _passage_line(self, passage_id, index, targets, assigned):
        """The `current` for line `index` (0-based) of a passage, or None past its last line.
        targets: the passage's {"review", "learning"} word ids; assigned: the targets earlier lines took.
        The line grades the targets it contains that no earlier line took (see line_targets)."""
        sentence_ids = list(PassageSentence.objects.filter(passage_id=passage_id).values_list("sentence_id", flat=True))
        if index >= len(sentence_ids):
            return None
        words = set(Sentence.words.through.objects.filter(sentence_id=sentence_ids[index]).values_list("word_id", flat=True))
        review, learning = line_targets(words, targets, assigned)
        return {
            "sentence": sentence_ids[index],
            "review": review,
            "learning": learning,
            "passage": passage_id,
            "index": index,
            "targets": targets,
            "assigned": assigned,
        }


    def sentence_payload(self, sentence, statuses):
        """The sentence as the typing page needs it: tokens with furigana and the role of each word (see word_role),
        dictionary data for Tab, and the stats.

        "passage" is None for a single sentence. On a passage line it is the whole passage (credit line, the index
        of this line and every line's tokens). The lines' roles use the passage's targets, so review words on lines
        still to come keep their furigana hidden and new words stay highlighted; this line's own fields (tokens,
        words, kana_words...) come exactly as for a single sentence, with only this line's graded words.
        """
        lines = []
        if self.current.get("passage"):
            lines = list(PassageSentence.objects.filter(passage_id=self.current["passage"]).select_related("passage", "sentence"))
        kana = {
            kana.id: kana
            for kana in KanaWord.objects.filter(
                id__in=set().union(kana_ids(sentence), *(kana_ids(line.sentence) for line in lines))
            ).exclude(meanings=[])
        }
        kana_words = {
            str(kana_id): {"text": kana[kana_id].text, "reading": kana[kana_id].reading,
                           "meanings": kana[kana_id].meanings, "kanji": []}
            for kana_id in kana_ids(sentence) if kana_id in kana
        }
        # New words on their first sighting are tested: furigana hidden on every line until Tab (see submit).
        testing = set(self.words.filter(
            word_id__in=self.current["targets"]["learning"] if lines else self.current["learning"],
            status=UserWord.LEARNING, sightings=0,
        ).values_list("word_id", flat=True))
        tokens = sentence_tokens(sentence, set(self.current["review"]), set(self.current["learning"]), statuses, kana, testing)
        extra_words = extra_cards(sentence, tokens)
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
            "passage": self._passage_view(lines, statuses, kana, testing) if lines else None,
        }


    def _passage_view(self, lines, statuses, kana, testing):
        """The passage around the line being typed (lines: its PassageSentence rows, in order, with passage and
        sentence loaded). Roles in every line come from the passage's targets, as on the line where each is graded."""
        passage, targets = lines[0].passage, self.current["targets"]
        review, learning = set(targets["review"]), set(targets["learning"])
        return {
            "id": passage.id,
            "title": passage.title,
            "author": passage.author,
            "source": SOURCE_NAMES.get(passage.source, passage.source),
            "url": passage.url,
            "licence": passage.licence,
            "index": self.current["index"],
            "lines": [
                {
                    "sentence_id": line.sentence_id,
                    "text": line.sentence.text,
                    "tokens": sentence_tokens(line.sentence, review, learning, statuses, kana, testing),
                }
                for line in lines
            ],
        }


    def submit(self, sentence_id, typed, revealed=(), skipped=()):
        """Grade the sentence that was just typed. Typos never count: only whether you needed the reading.

        Review words: reading revealed (Tab or help mode) -> Hard, otherwise -> Good.
        Skipped review words (you asked how to type them: you know the reading, not the keystrokes) aren't
        graded at all and stay due, unless you had also revealed the reading.
        New words: count one more sighting; after WARMUP_SIGHTINGS they get an FSRS card and start reviews.
        On its first sighting a new word is a test (furigana hidden): typed without help (no Tab, help mode or ⌥Tab),
        you already know it, so it skips the rest of the warm-up and starts reviews now.
        Words you were assumed to know but looked up with Tab: a known word goes back to the warm-up
        (furigana shown again; the look-up was its first sighting), a learned word that wasn't due is reviewed as Hard.
        On a passage line, the next line becomes current; after the last line the passage joins recent_passages
        and the next call picks a new one.
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
                passed_test = user_word.sightings == 0 and user_word.word_id not in revealed | skipped
                user_word.sightings += 1
                if passed_test or user_word.sightings >= WARMUP_SIGHTINGS:
                    user_word.status = UserWord.REVIEW
                    user_word.review(Rating.Good, now)
                    user_word.learned_at = user_word.learned_at or now  # a word sent back to warm-up keeps its first date

        in_sentence = {token[2] for token in sentence.tokens if token[2]}
        looked_up = list(self.words.filter(
            word_id__in=(revealed & in_sentence) - set(self.current["review"] + self.current["learning"])
        ))
        for user_word in looked_up:
            if user_word.status == UserWord.KNOWN:
                user_word.status, user_word.sightings = UserWord.LEARNING, 1  # not tested again: you just looked it up
            elif user_word.status == UserWord.REVIEW:
                user_word.review(Rating.Hard, now)
                logs.append(ReviewLog(learner=self, word_id=user_word.word_id, rating=Rating.Hard, at=now))
        UserWord.objects.bulk_update(graded + looked_up, ["status", "sightings", "card", "due", "learned_at"])
        ReviewLog.objects.bulk_create(logs)
        SentenceLog.objects.create(learner=self, at=now)

        self.recent = (self.recent + [sentence_id])[-RECENT_SENTENCES:]
        self.current = self._next_line() if self.current.get("passage") else {}
        self.save(update_fields=["recent", "current", "recent_passages"])


    def _next_line(self):
        """After a passage line is typed: the `current` for the next line, or {} after the last one, when the
        passage joins recent_passages so it isn't picked again soon."""
        line = self.current
        following = self._passage_line(
            line["passage"], line["index"] + 1, line["targets"], line["assigned"] + line["review"] + line["learning"]
        )
        if following is None:
            self.recent_passages = (self.recent_passages + [line["passage"]])[-RECENT_PASSAGES:]
        return following or {}


    def _fill_learning_pool(self):
        """Top the learning pool up with the next words the user hasn't met yet, in the JLPT list's order: every N5
        word before any N4 word and so on, most frequent first within a level (see _unmet_words).
        Words added to the corpus after onboarding (or 々 words left unmet by an older onboarding) are checked
        against the known kanji here, and become known."""
        missing = LEARNING_POOL - self.words.filter(status=UserWord.LEARNING).count()
        if missing <= 0:
            return

        known_set = set(self.known_kanji)
        new_rows = []
        for word_id, text in self._unmet_words().iterator():
            known = made_of_known_kanji(text, known_set)
            new_rows.append(UserWord(learner=self, word_id=word_id, status=UserWord.KNOWN if known else UserWord.LEARNING))
            missing -= not known
            if missing == 0:
                break
        UserWord.objects.bulk_create(new_rows, batch_size=1000, ignore_conflicts=True)  # two requests at once may add the same words


    def _unmet_words(self):
        """(id, text) of the words the learner hasn't met yet, in the learning order: level on the JLPT list (N5 first),
        most frequent first within a level. Only words on the list, and only those in a standalone sentence
        (frequency > 0), so sentence mode can serve them."""
        return (
            Word.objects.filter(list_level__isnull=False, frequency__gt=0)
            .exclude(id__in=UserWord.objects.filter(learner=self).values("word_id"))
            .order_by("-list_level", "-frequency", "id").values_list("id", "text")
        )


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
