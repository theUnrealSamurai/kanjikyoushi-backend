import random
from collections import Counter, defaultdict
from datetime import UTC, datetime, time, timedelta
from functools import lru_cache, reduce
from operator import or_
from zoneinfo import ZoneInfo

from django.contrib.auth.models import User
from django.db import connection, models, transaction
from django.db.models import Q
from django.utils import timezone
from fsrs import Card, Rating, Scheduler, State

from .corpus import (BEYOND, KANJI_RE, furigana, has_kanji, jlpt_kanji, kanji_dictionary, kanji_info, kanji_order,
                     kanji_rank, rank_level, reading_units, same_text)


scheduler = Scheduler()

DEFAULT_KNOWN_KANJI = "一二三四五六七八九十百千万"
LEARNING_POOL = 10          # new words being warmed up at any one time: enough for most sentences to hold NEW_PER_SENTENCE
WARMUP_SIGHTINGS = 3        # a new word is typed this many times before reviews start, unless it passes its test; the last
                            # typing is the warm-up check (furigana hidden), which sets the card's first rating
CHECK_DELAY = timedelta(hours=20)  # a learned word whose reading you looked up comes back for its check this much later:
                                   # the next day, so it tests memory, not the reading you just saw (owner, 2026-11-05)
EASY_READS = 2              # clean reads of a learned word while it isn't due (one a day, not on its review day) that make
                            # its next clean review Easy
MISSES = (Rating.Again, Rating.Hard)  # a look-up right after one of these is a second miss in a row: Again
NEW_PER_SENTENCE = 2        # at most this many new words are graded in one sentence (or passage line); every due word is
DUE_TARGETS = 12            # how many of the most overdue words to look for sentences for
CANDIDATES_PER_WORD = 200   # sentences sampled per target word when picking the next sentence
RECENT_SENTENCES = 50       # don't repeat a sentence typed this recently
DUE_LIST_LIMIT = 300        # most words the due list shows (the kanji list and the totals cover all due words)
REPORT_DAYS = (7, 365)      # fewest and most days the stats report covers
TYPING_IDLE = timedelta(minutes=3)  # a longer gap between two finished sentences is a break, not time spent typing
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
PREFETCH_FROM_LEVEL = 2     # learners whose next kanji is N2 or harder get their next sentence prepared while they type


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


def teachable_words():
    """(id, text, rank, list level, frequency) of every word sentence mode can teach: on the list and in a sentence."""
    return list(Word.objects.filter(list_level__isnull=False, frequency__gt=0)
                .values_list("id", "text", "rank", "list_level", "frequency"))


def learning_order(rows):
    """The teachable words (rows from teachable_words) in the order they're taught, as (id, text, stage): kanji by
    kanji, N5 to N1 (owner, 2026-11-04: "I really just want to learn N5 - 4 - 3 - 2 - 1 in order").

    A word's stage is the harder of its level on the vocabulary list and its hardest kanji's level, so 椅子 (an N5
    word with an N1 kanji) waits for N1, and so does 休学 (an N1 word made of N5 kanji). Stages go N5 first; within
    one, words go by their hardest kanji in the learning order (corpus.kanji_order), so kanji come in list order,
    then by list level and frequency. A kanji that no word of its own level's stage has (不 is only in harder
    words) comes in at that stage with its easiest word that has no harder kanji (不安: an N3 word, but its kanji are
    N4 and N5), so each level can be finished before the next starts; if every word with it has a harder kanji, it
    waits for that one's stage. Then each kanji's one-kanji word moves up in front of its longer words (see
    one_kanji_first)."""
    levels = jlpt_kanji()
    stage = {id: min(list_level, rank_level(rank)) for id, _, rank, list_level, _ in rows}
    easiest, staged = {}, set()  # kanji -> (key, id) of its easiest word with no harder kanji; kanji their stage has
    for id, text, rank, list_level, frequency in rows:
        for kanji in set(KANJI_RE.findall(text)) & levels.keys():
            if stage[id] == levels[kanji]:
                staged.add(kanji)
            key = (list_level, frequency, -id)
            if rank_level(rank) == levels[kanji] and (kanji not in easiest or key > easiest[kanji][0]):
                easiest[kanji] = (key, id)
    for kanji, (_, id) in easiest.items():
        if kanji not in staged:
            stage[id] = max(stage[id], levels[kanji])
    rows = sorted(rows, key=lambda row: (-stage[row[0]], row[2], -row[3], -row[4], row[0]))
    return one_kanji_first([(id, text, stage[id]) for id, text, *_ in rows])


def word_kanji(text):
    """The kanji of a word, in order, once each. The repeat marks 々〆ヶ aren't kanji to learn."""
    return [kanji for kanji in dict.fromkeys(KANJI_RE.findall(text)) if kanji not in NOT_KANJI]


def one_kanji_first(order):
    """The learning order with each kanji's first one-kanji word moved up to just before the first longer word with
    that kanji (owner, 2026-11-10: "if a 2 word kanji appears, I should know both the kanjis. if I don't, introduce
    me the words with only one kanji"): 週 before 今週, 毎 before 毎日. A moved word takes the stage of the word it
    goes in front of, even when the list puts it on a harder level (週 is an N3 word, 毎 N2): it brings no harder
    kanji in. A kanji with no one-kanji word (曜, 族) still comes in with its first longer word."""
    single = {}  # kanji -> its first one-kanji word, (id, text)
    for id, text, _ in order:
        kanji = word_kanji(text)
        if len(kanji) == 1:
            single.setdefault(kanji[0], (id, text))
    placed, moved = set(), []
    for id, text, stage in order:
        if id in placed:
            continue
        kanji = word_kanji(text)
        if len(kanji) > 1:
            for one in (single[k] for k in kanji if k in single and single[k][0] not in placed):
                moved.append((*one, stage))
                placed.add(one[0])
        moved.append((id, text, stage))
        placed.add(id)
    return moved


def introducers(order):
    """{kanji: id of the first word in the learning order that has it}: the word that brings that kanji in. After
    one_kanji_first that's its one-kanji word when there is one; otherwise its first longer word (曜日 brings 曜 in,
    and 研究 both of its kanji)."""
    first = {}
    for id, text, _ in order:
        for kanji in word_kanji(text):
            first.setdefault(kanji, id)
    return first


def sentence_range(word_rank, bound):
    """(first, last) sentence ids to search for a word of this rank, up to the rank `bound`. The corpus is stored in
    rank order and no sentence with the word is easier than the word, so they all come after the last sentence of
    the rank before it (passage-only lines and older sentences sit below) and the search stops at the bookmark.
    The whole table if there are no bookmarks."""
    last = bookmark(bound)
    if last is None:
        return 1, Sentence.objects.order_by("-id").values_list("id", flat=True).first() or 0
    return (bookmark(word_rank - 1) or 0) + 1, last


def sample_sentences(word_id, first, last, count):
    """Up to `count` ids of standalone sentences with the word, between sentence ids `first` and `last` (see
    sentence_range), from a random starting point and wrapping round. Each part is one range on the (word_id,
    sentence_id) index, so it costs the same whether the corpus has a million sentences or thirty."""
    if first > last:
        return []
    start = random.randint(first, last)
    rows = Sentence.words.through.objects.filter(word_id=word_id, sentence__standalone=True).order_by("sentence_id")
    ids = list(rows.filter(sentence_id__gte=start, sentence_id__lte=last).values_list("sentence_id", flat=True)[:count])
    if len(ids) < count:
        ids += rows.filter(sentence_id__gte=first, sentence_id__lt=start).values_list("sentence_id", flat=True)[:count - len(ids)]
    return ids


def word_role(word_id, review, learning, statuses, testing=frozenset()):
    """A token's role: "review" (graded, furigana hidden until Tab), "learning" (new word, furigana shown),
    "test" (a new word on its first sighting or its warm-up check, furigana hidden until Tab: see Learner.submit),
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


def sentence_tokens(sentence, review, learning, statuses, kana_with_meanings, testing=frozenset(), can_read=None):
    """The sentence's tokens for the typing page: furigana pairs, the role of each word (see word_role) and, for a
    kana-only word with a meaning (an id in kana_with_meanings), its lookup id for Tab.
    can_read(surface, reading) (see Learner.reader): a word never met that the learner can read anyway, because every
    kanji in it is read the way they already read it, is "known" instead: furigana hidden until Tab, never graded.
    A word warming up keeps its furigana: they're learning it (or looked it up and were sent back to the warm-up)."""
    tokens = []
    for surface, reading, word_id, *rest in sentence.tokens:
        role = word_role(word_id, review, learning, statuses, testing)
        if role == "unseen" and word_id not in statuses and can_read and can_read(surface, reading):
            role = "known"
        tokens.append({
            "text": surface,
            "word": word_id,
            "role": role,
            "ruby": furigana(surface, reading),
            "lookup": rest[0] if rest and rest[0] in kana_with_meanings else None,
        })
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


GRADE_OUTCOMES = ("easy", "good", "hard", "again", "check", "warmup")  # what submit can do to a word (see token_grade)


def token_grade(role, user_word):
    """What the line being typed does to a token's word, for the page to flash once the word is typed:
    {"clean", "tab", "skip"} -> the outcome (one of GRADE_OUTCOMES, or None: nothing to show) when it is typed without
    help, after its reading was revealed (Tab or help mode), or after ⌥Tab. None for a word nothing can happen to.
    Mirrors Learner.submit; user_word is the learner's row for the word (None if they have none)."""
    if user_word is None:
        return None
    if role == "review":
        if user_word.owes_check:
            return {"clean": "hard", "tab": "again", "skip": None}
        return {"clean": "easy" if user_word.easy_ready() else "good", "tab": user_word.look_up_outcome(), "skip": None}
    if role == "test":
        if user_word.sightings == 0:
            return {"clean": "easy", "tab": "warmup", "skip": "warmup"}
        return {"clean": "good", "tab": "hard", "skip": None}  # the warm-up check
    if role == "known" and user_word.status == UserWord.KNOWN:
        return {"clean": None, "tab": "warmup", "skip": None}
    if role == "known" and user_word.status == UserWord.REVIEW:
        return {"clean": None, "tab": user_word.look_up_outcome(), "skip": None}
    return None


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
    rank = models.SmallIntegerField(default=BEYOND)     # hardest kanji's place in the learning order (corpus.kanji_rank)
    # Level on the JLPT vocabulary list (assets/jlpt_vocab: 5 = N5 ... 1 = N1); sets the learning order.
    # None: not on the list, so never taught or graded (like a name: furigana and a Tab card only).
    list_level = models.PositiveSmallIntegerField(null=True, blank=True)
    meanings = models.JSONField(default=list, blank=True)  # English, one string per sense (up to 3), from JMdict

    class Meta:
        constraints = [models.UniqueConstraint(fields=["text", "reading"], name="unique_word")]
        # teachable_words runs on every sentence pick: this covers it, so it reads ~6k index entries, not the table.
        indexes = [models.Index(fields=["list_level", "frequency", "rank", "text"], name="type_word_teachable",
                                condition=Q(list_level__isnull=False, frequency__gt=0))]

    def __str__(self):
        return f"{self.text} ({self.reading})"

    def save(self, *args, **kwargs):
        if self._state.adding and self.rank == BEYOND:
            self.rank = kanji_rank(self.text)  # bulk_create skips this: build_corpus sets it itself
        super().save(*args, **kwargs)


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
    # Hardest kanji's place in the learning order (corpus.kanji_rank). build_corpus stores standalone sentences in
    # rank order, easiest first, so a Bookmark (an id) marks where the sentences a learner can read end.
    rank = models.SmallIntegerField(default=BEYOND)

    def __str__(self):
        return self.text


class Bookmark(models.Model):
    """For each rank, the last standalone sentence id whose rank is at or below it: the original n1n5 table's
    kanji_end_index. Sentence picking only searches ids up to the learner's bookmark, so it never reads past what
    they know however big the corpus is. Rebuilt by refresh_bookmarks() whenever the corpus changes."""

    rank = models.SmallIntegerField(primary_key=True)
    last_sentence = models.PositiveIntegerField()  # a sentence id (not a key: it only bounds a range)


def refresh_bookmarks():
    """Rebuild the Bookmark table from the standalone sentences (one pass over the table)."""
    last, rows = 0, []
    for rank, top in (Sentence.objects.filter(standalone=True).order_by().values("rank")
                      .annotate(top=models.Max("id")).order_by("rank").values_list("rank", "top")):
        last = max(last, top)
        rows.append(Bookmark(rank=rank, last_sentence=last))
    with transaction.atomic():
        Bookmark.objects.all().delete()
        Bookmark.objects.bulk_create(rows, batch_size=1000)


def bookmark(rank):
    """The last standalone sentence id with a rank at or below this one: 0 if there is none, None if the Bookmark
    table is empty (no bound: callers check each sentence's rank anyway). One primary-key lookup."""
    last = Bookmark.objects.filter(rank__lte=rank).order_by("-rank").values_list("last_sentence", flat=True).first()
    if last is not None:
        return last
    return 0 if Bookmark.objects.exists() else None


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


KANJI_MILESTONES = (100, 250, 500, 1000, 1500, 2000)  # kanji learned here that get a celebration, then every 500 after
MILESTONE_STEP = 500


def kanji_milestone(before, after):
    """The highest kanji-count milestone passed going from `before` to `after` kanji learned, or None."""
    passed = [m for m in KANJI_MILESTONES if before < m <= after]
    if after > KANJI_MILESTONES[-1]:
        top = after // MILESTONE_STEP * MILESTONE_STEP
        if top > max(before, KANJI_MILESTONES[-1]):
            passed.append(top)
    return max(passed, default=None)


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
    upcoming = models.JSONField(default=dict, blank=True)  # the next sentence, prepared while one is typed (see prefetch)

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
        self.current, self.recent, self.recent_passages, self.upcoming = {}, [], [], {}
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


    def readable_kanji(self, teachable):
        """(kanji a sentence may contain, bookmark rank), for sentence picking (owner, 2026-11-04: "try not to show
        the sentences with the kanji I don't know until I learn it"). Readable: known at onboarding, or in a word
        you know, have learned or are warming up (so a new word's own kanji are fine). The bookmark is the last
        rank before the first kanji in the learning order you can't read yet, so every sentence up to it is readable.
        Kanji no teachable word has (rows from teachable_words) are passed over: they'd never be learned, and they
        only appear in names and unlisted words, which show furigana."""
        texts = self.words.values_list("word__text", flat=True)
        readable = set(self.known_kanji) | {kanji for text in texts for kanji in KANJI_RE.findall(text)}
        can_learn = {kanji for _, text, *_ in teachable for kanji in KANJI_RE.findall(text)}
        bookmark = -1
        for kanji, rank in kanji_order().items():  # in rank order
            if kanji in can_learn and kanji not in readable:
                break
            readable.add(kanji)
            bookmark = rank
        return readable, bookmark


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
        # Time typing, roughly: each sentence is credited the time since the one before it, unless that gap is a break
        # (TYPING_IDLE). A session's first sentence has no earlier one, so it counts nothing. Credited to the later day.
        typing_days, ordered = Counter(), sorted(sentence_times)
        for before, after in zip(ordered, ordered[1:]):
            if after - before <= TYPING_IDLE:
                typing_days[local(after)] += (after - before).total_seconds()
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
                "typing_seconds": round(typing_days[date]),
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
            "today": {key: last[key] for key in ("words", "kanji", "reviews", "sentences", "typing_seconds")},
            "totals": {
                "words": len(learned_rows),
                "kanji": len(learned_kanji),
                "known_kanji": len(known),
                "learning": self.words.filter(status=UserWord.LEARNING).count(),
                "due": self.due_words().count(),
                "sentences": len(sentence_times),
                "typing_seconds": round(sum(typing_days.values())),
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

        teachable = teachable_words()
        if self.learn_new:
            self._fill_learning_pool(teachable)
        due = list(self.due_words().values_list("word_id", flat=True))  # most overdue first
        learning = list(self.words.filter(status=UserWord.LEARNING).order_by("word__rank", "-word__list_level", "-word__frequency", "word_id")
                        .values_list("word_id", flat=True)) if self.learn_new else []
        if not due and not learning:
            return None
        statuses = dict(self.words.values_list("word_id", "status"))
        sentence, prefetch = self._take_upcoming(due, learning), True
        if not sentence:
            readable = self.readable_kanji(teachable)
            sentence = self._pick_sentence(due, learning, statuses, teachable, readable)
            prefetch = rank_level(readable[1] + 1) <= PREFETCH_FROM_LEVEL
        if not sentence:
            return None
        return {**self.sentence_payload(sentence, statuses), "prefetch": prefetch}


    def prefetch(self):
        """Prepare the next single sentence while this one is typed (owner, 2026-11-04: only from N2 on, where a pick
        takes ~300 ms on the phone; see PREFETCH_FROM_LEVEL). It's picked as if this sentence had been typed: the
        words it grades aren't targets, and it can't come up again. Stored in `upcoming` for next_sentence, which
        checks it still fits (see _take_upcoming). Nothing happens in a passage, or for learners below N2."""
        current = self.current
        if not current.get("sentence") or current.get("passage") or (self.paragraphs and self.paragraphs_unlocked()):
            return
        teachable = teachable_words()
        readable = self.readable_kanji(teachable)
        if rank_level(readable[1] + 1) > PREFETCH_FROM_LEVEL:
            return
        graded = set(current["review"] + current["learning"])
        due = [w for w in self.due_words().values_list("word_id", flat=True) if w not in graded]
        learning = [w for w in self.words.filter(status=UserWord.LEARNING)
                    .order_by("word__rank", "-word__list_level", "-word__frequency", "word_id")
                    .values_list("word_id", flat=True) if w not in graded] if self.learn_new else []
        if not due and not learning:
            return
        statuses = dict(self.words.values_list("word_id", "status"))
        choice = self._choose_sentence(due, learning, statuses, teachable, readable, avoid=current["sentence"])
        if choice:
            self.upcoming = {"after": current["sentence"], "sentence": choice[0], "learn_new": self.learn_new}
            self.save(update_fields=["upcoming"])


    def _take_upcoming(self, due, learning):
        """The sentence prefetch prepared while the last one was typed, set as current, if it still fits: prepared
        after that sentence, in the same mode, and still with a word to grade (a warm-up word while learning). The
        words it grades are worked out now, so they're right however the last sentence went. It's used once either
        way. None if there is none or it doesn't fit: then a sentence is picked as usual."""
        upcoming = self.upcoming
        if not upcoming:
            return None
        self.upcoming = {}
        sentence = None
        if (upcoming["after"] == (self.recent[-1] if self.recent else None) and upcoming["learn_new"] == self.learn_new
                and not (self.paragraphs and self.paragraphs_unlocked())):
            sentence = Sentence.objects.filter(id=upcoming["sentence"], standalone=True).first()
        words = set(Sentence.words.through.objects.filter(sentence=sentence).values_list("word_id", flat=True)) if sentence else set()
        due_rank = {word_id: rank for rank, word_id in enumerate(due)}
        review = sorted((w for w in words if w in due_rank), key=due_rank.get)
        new = [w for w in learning if w in words][:NEW_PER_SENTENCE]
        if not (new if self.learn_new else review):
            self.save(update_fields=["upcoming"])
            return None
        self.current = {"sentence": sentence.id, "review": review, "learning": new}
        self.save(update_fields=["current", "upcoming"])
        return sentence


    def _pick_sentence(self, due, learning, statuses, teachable, readable=None):
        """Pick a single sentence (see _choose_sentence), set it as current and return it (None if no sentence has a
        target word). readable: readable_kanji's answer, if the caller has it."""
        choice = self._choose_sentence(due, learning, statuses, teachable, readable or self.readable_kanji(teachable))
        if not choice:
            return None
        sentence_id, review, learning = choice
        self.current = {"sentence": sentence_id, "review": review, "learning": learning}
        self.save(update_fields=["current"])
        return Sentence.objects.get(id=sentence_id)


    def _choose_sentence(self, due, learning, statuses, teachable, readable, avoid=None):
        """Choose a single sentence: (id, due words it grades, warm-up words it grades), or None if no sentence has a
        target word. `avoid`: a sentence id that mustn't be chosen (the one being typed, when prefetching).

        due: every due word, most overdue first; sentences are looked for around the first DUE_TARGETS of them.
        The owner's bookmark logic: sentences are ranked by their hardest kanji and stored in that order, and a
        target word's sentences are only searched up to the learner's bookmark (see readable_kanji), or up to the
        word's own rank for a word learned out of order. Of those, only sentences whose kanji are all readable are
        eligible; if none are, the ones with the fewest unreadable kanji ("try not to show" them).
        The best sentence covers the most target words while containing as few never-seen words as possible.
        A sentence grades every due word in it and up to NEW_PER_SENTENCE warm-up words (in the learning order),
        so one with more of them scores higher. While learning is on, every sentence has at least one warm-up word:
        only sentences containing one are eligible (falling back to all candidates if somehow none do). Only
        standalone sentences are served: passage-only lines would make no sense out of context.
        """
        readable, reach = readable
        targets = due[:DUE_TARGETS] + learning
        ranks, bounds = dict(Word.objects.filter(id__in=targets).values_list("id", "rank")), {}
        for word_id in targets:
            bound = max(reach, ranks[word_id])
            for sentence_id in sample_sentences(word_id, *sentence_range(ranks[word_id], bound), CANDIDATES_PER_WORD):
                bounds[sentence_id] = max(bounds.get(sentence_id, bound), bound)
        unreadable = {
            sentence_id: len(set(KANJI_RE.findall(text)) - readable - set(NOT_KANJI))
            for sentence_id, text, rank in Sentence.objects.filter(id__in=bounds).values_list("id", "text", "rank")
            if rank <= bounds[sentence_id] and sentence_id != avoid  # a passage line later added to the corpus keeps its id
        }
        if not unreadable:
            return None  # the target words only appear in passages

        through = Sentence.words.through
        words_in = defaultdict(set)
        for sentence_id, word_id in through.objects.filter(sentence_id__in=unreadable).values_list("sentence_id", "word_id"):
            words_in[sentence_id].add(word_id)
        recent, due_rank = set(self.recent), {word_id: rank for rank, word_id in enumerate(due)}

        pool = sorted(unreadable)
        if learning:
            with_learning = [sid for sid in pool if words_in[sid] & set(learning)]
            pool = with_learning or pool
        fewest = min(unreadable[sid] for sid in pool)
        pool = [sid for sid in pool if unreadable[sid] == fewest]
        best, best_score = None, None
        for sentence_id in random.sample(pool, len(pool)):
            words = words_in[sentence_id]
            due_hits = sorted((w for w in words if w in due_rank), key=due_rank.get)
            learning_hits = [w for w in learning if w in words][:NEW_PER_SENTENCE]
            unseen = sum(1 for w in words if w not in statuses)
            score = 3 * len(due_hits) + 2 * len(learning_hits) - 2 * unseen - 5 * (sentence_id in recent)
            if best_score is None or score > best_score:
                best, best_score = (sentence_id, due_hits, learning_hits), score
        return best


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
                            .order_by("word__rank", "-word__list_level", "-word__frequency", "word_id").values_list("word_id", flat=True))
            window = self._new_word_window(teachable_words())
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


    def _new_word_window(self, teachable):
        """The next NEW_WORD_WINDOW words to learn, in the learning order (see _fill_learning_pool), for paragraph
        mode to bring in. Words met on the way that are made only of known kanji become known, as there."""
        known_set, window, known_rows = set(self.known_kanji), [], []
        for word_id, text in self._unmet_words(teachable):
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
        # New words on their first sighting and at their warm-up check are tested: furigana hidden on every line until
        # Tab (see submit). In a passage, only the ones no earlier line has graded (one graded there has moved on).
        testing = set(self.words.filter(
            Q(sightings=0) | Q(sightings__gte=WARMUP_SIGHTINGS - 1),
            word_id__in=set(self.current["targets"]["learning"]) - set(self.current["assigned"]) if lines else self.current["learning"],
            status=UserWord.LEARNING,
        ).values_list("word_id", flat=True))
        can_read = self.reader([sentence, *(line.sentence for line in lines)])
        tokens = sentence_tokens(sentence, set(self.current["review"]), set(self.current["learning"]), statuses, kana, testing,
                                 can_read)
        rows = {row.word_id: row for row in self.words.filter(word_id__in={t["word"] for t in tokens if t["word"]})}
        for token in tokens:
            token["grade"] = token_grade(token["role"], rows.get(token["word"]))
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
            "passage": self._passage_view(lines, statuses, kana, testing, can_read) if lines else None,
        }


    def reader(self, sentences):
        """can_read(surface, reading) for the words in these sentences: whether the learner can read the word without
        furigana. They can when each kanji in it (see reading_units) is read the way it is in a word they have learned
        or know, or is a kanji they knew at onboarding (any reading, as for the words made only of those). So once
        友達 is learned, 人達 needs no furigana, but 発達 (達 read たつ) still does."""
        known_kanji = set(self.known_kanji) | set(NOT_KANJI)
        kanji = {kanji for sentence in sentences for surface, _, word_id, *_ in sentence.tokens if word_id
                 for kanji in KANJI_RE.findall(surface)}
        units = set()
        if kanji - known_kanji:
            for text, reading in self.words.filter(status__in=(UserWord.REVIEW, UserWord.KNOWN)).values_list("word__text", "word__reading"):
                if not kanji.isdisjoint(text):
                    units.update(reading_units(text, reading))

        def can_read(surface, reading):
            word_units = reading_units(surface, reading)
            return bool(word_units) and all(
                unit in units or set(KANJI_RE.findall(unit[0])) <= known_kanji for unit in word_units
            )
        return can_read


    def _passage_view(self, lines, statuses, kana, testing, can_read=None):
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
                    "tokens": sentence_tokens(line.sentence, review, learning, statuses, kana, testing, can_read),
                }
                for line in lines
            ],
        }


    def submit(self, sentence_id, typed, revealed=(), skipped=(), help=False):
        """Grade the sentence that was just typed. Typos never count: only whether you needed the reading.
        Returns {word id: outcome} for the words it graded or changed (see GRADE_OUTCOMES), which the page flashes.

        Due words (owner, 2026-11-05):
        - typed without help -> Good, or Easy once read clean EASY_READS times since its last review (see easy_ready)
        - reading revealed (Tab or help mode) -> no rating yet: a check CHECK_DELAY later (the next day), furigana
          hidden. The check typed clean -> Hard; looked up again -> Again. A look-up right after a miss (last rating
          Hard or Again) is Again at once: two misses in a row.
        - skipped (⌥Tab: you know the reading, not the keystrokes) -> not graded and stays due, unless you had also
          revealed the reading.
        New words: on the first sighting a new word is a test (furigana hidden): typed without help (no Tab, help mode
        or ⌥Tab), you already knew it, so its card starts with Easy and the warm-up is skipped. Otherwise it warms up
        with furigana, and its last warm-up typing (WARMUP_SIGHTINGS) is a check with furigana hidden: clean -> the
        card starts with Good, Tab -> with Hard (⌥Tab: the check comes again next time).
        Words you were assumed to know but looked up with Tab: a known word goes back to the warm-up (furigana shown
        again; the look-up was its first sighting), a learned word that isn't due gets a check (or Again) as above.
        Learned words that aren't due, read without help (no Tab, ⌥Tab or help mode on this sentence), count a clean
        read, at most one a day and not on the day of their last review.
        On a passage line, the next line becomes current; after the last line the passage joins recent_passages
        and the next call picks a new one.
        """
        sentence = Sentence.objects.filter(id=sentence_id).first()
        if sentence is None or self.current.get("sentence") != sentence_id:
            raise ValueError("That is not the sentence currently being typed.")
        if not same_text(typed, sentence.text):
            raise ValueError("The typed sentence doesn't match.")

        now, revealed, skipped = timezone.now(), set(revealed), set(skipped)
        today = now.astimezone(self.zone()).date()
        logs, outcomes = [], {}  # one ReviewLog per FSRS review of a learned word (not the graduation review)

        def rate(user_word, rating):
            user_word.review(rating, now)
            logs.append(ReviewLog(learner=self, word_id=user_word.word_id, rating=rating, at=now))
            outcomes[user_word.word_id] = rating.name.lower()

        def look_up(user_word):
            outcomes[user_word.word_id] = user_word.look_up(now)
            if outcomes[user_word.word_id] == "again":
                logs.append(ReviewLog(learner=self, word_id=user_word.word_id, rating=Rating.Again, at=now))

        def graduate(user_word, rating):
            user_word.status = UserWord.REVIEW
            user_word.review(rating, now)
            user_word.learned_at = user_word.learned_at or now  # a word sent back to warm-up keeps its first date
            outcomes[user_word.word_id] = rating.name.lower()

        graded = list(self.words.filter(word_id__in=self.current["review"] + self.current["learning"]))
        for user_word in graded:
            word_id = user_word.word_id
            if word_id in self.current["review"]:
                if word_id in revealed:
                    look_up(user_word)
                elif word_id not in skipped:
                    rate(user_word, Rating.Hard if user_word.owes_check else Rating.Easy if user_word.easy_ready() else Rating.Good)
            elif user_word.sightings == 0:  # the first-sighting test
                user_word.sightings = 1
                if word_id in revealed | skipped:
                    outcomes[word_id] = "warmup"
                else:
                    graduate(user_word, Rating.Easy)
            elif user_word.sightings >= WARMUP_SIGHTINGS - 1:  # the warm-up check
                if word_id in revealed or word_id not in skipped:
                    user_word.sightings += 1
                    graduate(user_word, Rating.Hard if word_id in revealed else Rating.Good)
            else:
                user_word.sightings += 1

        in_sentence = {token[2] for token in sentence.tokens if token[2]}
        graded_ids = set(self.current["review"] + self.current["learning"])
        looked_up = list(self.words.filter(word_id__in=(revealed & in_sentence) - graded_ids))
        for user_word in looked_up:
            if user_word.status == UserWord.KNOWN:
                user_word.status, user_word.sightings = UserWord.LEARNING, 1  # not tested again: you just looked it up
                outcomes[user_word.word_id] = "warmup"
            elif user_word.status == UserWord.REVIEW:
                look_up(user_word)

        read = []
        if not help:
            for user_word in self.words.filter(status=UserWord.REVIEW, owes_check=False, due__gt=now,
                                               word_id__in=in_sentence - graded_ids - revealed - skipped):
                last = Card.from_dict(user_word.card).last_review if user_word.card else None
                if user_word.read_on != today and (last is None or last.astimezone(self.zone()).date() != today):
                    user_word.clean_reads, user_word.read_on = user_word.clean_reads + 1, today
                    read.append(user_word)

        UserWord.objects.bulk_update(graded + looked_up + read, ["status", "sightings", "card", "due", "learned_at", "owes_check",
                                                                 "last_rating", "clean_reads", "read_on"])
        ReviewLog.objects.bulk_create(logs)
        SentenceLog.objects.create(learner=self, at=now)

        self.recent = (self.recent + [sentence_id])[-RECENT_SENTENCES:]
        self.current = self._next_line() if self.current.get("passage") else {}
        self.save(update_fields=["recent", "current", "recent_passages"])
        return outcomes


    def warming_words(self):
        """{word id: text} of the warm-up words the sentence on screen grades: the only words typing it can graduate.
        Taken before submit, for celebrations."""
        return dict(self.words.filter(word_id__in=self.current.get("learning", []), status=UserWord.LEARNING)
                    .values_list("word_id", "word__text"))


    def celebrations(self, warming):
        """What the sentence just submitted earned, for the page to celebrate: [{"kind": "level", "level": 5}, ...,
        {"kind": "kanji", "count": 100}], levels first and at most one kanji milestone (the highest). `warming` is
        warming_words() from before the submit. Only a word graduating to REVIEW can change anything, so when none
        did this costs one small query. Then one query for the learned words' text.
        - kanji: the stats page's number (report totals.kanji): distinct kanji in REVIEW words, minus those known at
          onboarding, minus 々〆ヶ. A milestone is a count crossing it.
        - level N5-N1: every kanji of that JLPT level that some teachable word has is covered (known at onboarding
          or in a REVIEW word; a KNOWN word's kanji are all known at onboarding), and one of the new kanji is in it."""
        if not warming:
            return []
        graduated = set(self.words.filter(word_id__in=warming, status=UserWord.REVIEW).values_list("word_id", flat=True))
        if not graduated:
            return []
        known, repeat_marks = set(self.known_kanji), set(NOT_KANJI)
        before, after = set(), set()
        for word_id, text in self.words.filter(status=UserWord.REVIEW).values_list("word_id", "word__text"):
            kanji = set(KANJI_RE.findall(text)) - known - repeat_marks
            after |= kanji
            if word_id not in graduated:
                before |= kanji
        new = after - before
        if not new:
            return []
        covered, levels = after | known, jlpt_kanji()
        can_learn = {kanji for _, text, *_ in teachable_words() for kanji in KANJI_RE.findall(text)}
        celebrate = []
        for level in range(5, 0, -1):
            required = {kanji for kanji, kanji_level in levels.items() if kanji_level == level and kanji in can_learn}
            if new & required and required <= covered:
                celebrate.append({"kind": "level", "level": level})
        milestone = kanji_milestone(len(before), len(after))
        if milestone:
            celebrate.append({"kind": "kanji", "count": milestone})
        return celebrate


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


    def _fill_learning_pool(self, teachable):
        """Top the learning pool up with the next words the user hasn't met yet, in the learning order: kanji by
        kanji, N5 to N1 (see learning_order). Words added to the corpus after onboarding (or 々 words left unmet by
        an older onboarding) are checked against the known kanji here, and become known."""
        missing = LEARNING_POOL - self.words.filter(status=UserWord.LEARNING).count()
        if missing <= 0:
            return

        known_set = set(self.known_kanji)
        new_rows = []
        for word_id, text in self._unmet_words(teachable):
            known = made_of_known_kanji(text, known_set)
            new_rows.append(UserWord(learner=self, word_id=word_id, status=UserWord.KNOWN if known else UserWord.LEARNING))
            missing -= not known
            if missing == 0:
                break
        UserWord.objects.bulk_create(new_rows, batch_size=1000, ignore_conflicts=True)  # two requests at once may add the same words


    def _unmet_words(self, teachable):
        """(id, text) of the words the learner hasn't met yet and can start now, in the learning order (see
        learning_order). Only words on the list, and only those in a standalone sentence (frequency > 0), so sentence
        mode can serve them.

        A word waits while it has a kanji you don't know yet (see kanji_known), unless it's the word that brings that
        kanji in (see introducers; owner, 2026-11-10: "if a 2 word kanji appears, I should know both the kanjis"):
        今週 waits until 週 has finished its warm-up, while 曜日, the first word with 曜, brings 曜 in itself. Waiting
        words are passed over, so the words after them fill the pool meanwhile; there's no stopping at the end of a
        stage, where a waiting word could otherwise hold everything up (a one-kanji word like 週 may have no sentence
        you can read until the bookmark moves on)."""
        order = learning_order(teachable)
        met, known, first = set(self.words.values_list("word_id", flat=True)), self.kanji_known(), introducers(order)
        return [(id, text) for id, text, _ in order
                if id not in met and all(first[kanji] == id for kanji in word_kanji(text) if kanji not in known)]


    def kanji_known(self):
        """The kanji you know: picked at onboarding, or in a word you have learned (REVIEW; a word still warming up
        doesn't count), as for n5_progress. KNOWN words are left out: a word is only ever marked known when it's made
        of kanji known at onboarding, and leaving them out keeps this quick for learners who knew many at onboarding."""
        texts = self.words.filter(status=UserWord.REVIEW).values_list("word__text", flat=True)
        return set(self.known_kanji) | {kanji for text in texts for kanji in KANJI_RE.findall(text)}


    def release_warmups(self, teachable, apply=False):
        """Warm-up words (no FSRS card yet) from a stage past the one the learner is on: the old order, by vocabulary
        level alone, brought in words like 財布 (an N5 word with an N2 kanji) early. They go back to unmet and come
        again at their stage. Words made of known kanji stay (they're there because they were looked up with Tab).
        Returns the released words' texts; only deletes them when `apply`."""
        order, met = learning_order(teachable), set(self.words.values_list("word_id", flat=True))
        current = next((stage for id, _, stage in order if id not in met), None)
        if current is None:
            return []
        stages, known_set = {id: stage for id, _, stage in order}, set(self.known_kanji)
        rows = [(row.id, row.word.text) for row in self.words.filter(status=UserWord.LEARNING, card__isnull=True).select_related("word")
                if stages.get(row.word_id, 0) < current and not made_of_known_kanji(row.word.text, known_set)]
        if apply:
            UserWord.objects.filter(id__in=[id for id, _ in rows]).delete()
        return [text for _, text in rows]


class UserWord(models.Model):
    KNOWN, LEARNING, REVIEW = "known", "learning", "review"
    STATUSES = [(KNOWN, "Known"), (LEARNING, "Learning"), (REVIEW, "Review")]

    learner = models.ForeignKey(Learner, on_delete=models.CASCADE, related_name="words")
    word = models.ForeignKey(Word, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=8, choices=STATUSES)
    sightings = models.PositiveSmallIntegerField(default=0)  # times typed while learning
    card = models.JSONField(null=True, blank=True)           # FSRS card, once reviews start
    due = models.DateTimeField(null=True, blank=True)         # copy of card.due so due words can be queried; while a
                                                              # check is owed, when the check comes up instead
    learned_at = models.DateTimeField(null=True, blank=True)  # when the warm-up finished; null = before history was tracked
    owes_check = models.BooleanField(default=False, db_default=False)  # reading looked up: owes a check, rated at its next review
    last_rating = models.PositiveSmallIntegerField(null=True, blank=True)  # last FSRS rating, graduation included
    clean_reads = models.PositiveSmallIntegerField(default=0, db_default=0)  # read without help while not due, since the last review
    read_on = models.DateField(null=True, blank=True)          # local day of the last clean read counted (one a day)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["learner", "word"], name="unique_learner_word")]
        indexes = [models.Index(fields=["learner", "status", "due"])]

    def __str__(self):
        return f"{self.learner} · {self.word} · {self.status}"

    def review(self, rating, now):
        """One FSRS review. It settles any check and starts the clean-read count afresh."""
        card = Card.from_dict(self.card) if self.card else Card()
        card, _ = scheduler.review_card(card, rating, now)
        self.card, self.due = card.to_dict(), card.due
        self.last_rating, self.owes_check, self.clean_reads = int(rating), False, 0

    def easy_ready(self):
        """A clean review now would be Easy: read clean EASY_READS times since its last review, on other days, while
        it wasn't due (words seen everywhere move out faster). Only for a card past its learning steps."""
        return self.clean_reads >= EASY_READS and bool(self.card) and Card.from_dict(self.card).state == State.Review

    def look_up_outcome(self):
        """What looking up this learned word's reading does: "again" if it already owes a check or its last rating
        was a miss (two misses in a row, owner, 2026-11-05), else "check" (no rating yet, a check the next day)."""
        return "again" if self.owes_check or self.last_rating in MISSES else "check"

    def look_up(self, now):
        """Its reading was looked up (Tab or help mode): Again now, or a check CHECK_DELAY from now (see
        look_up_outcome). Returns the outcome; an Again needs a ReviewLog, which the caller adds."""
        outcome = self.look_up_outcome()
        if outcome == "again":
            self.review(Rating.Again, now)
        else:
            self.owes_check, self.due, self.clean_reads = True, now + CHECK_DELAY, 0
        return outcome


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
