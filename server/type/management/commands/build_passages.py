"""Build the passages file for paragraph mode from the cleaned Aozora Bunko dataset.

Runs on the development machine (needs fugashi and cutlet). It writes a self-contained gzipped JSON Lines
file that `load_passages` imports, on the Mac or on the phone, without any of the heavy dependencies.

Line 1 is a header, line 2 the dictionary data (jlpt level and meanings) of every word referenced, then one
line per passage, easiest first. Everything is keyed by natural keys (text and reading), never database ids.
"""
import gzip
import json
import os
import re
from collections import Counter
from datetime import date
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from type.corpus import KANJI_RE, is_joyo_only, jlpt_kanji, jlpt_level, katakana_count, romaji, strip_invisible, tokenize
from type.dictionary import (
    cached_dictionary, find_kana_meanings, find_meanings, index_by_kana, index_by_kanji, read_entries,
)


FORMAT_VERSION = 1
DATASET = "globis-university/aozorabunko-clean (aozorabunko-dedupe-clean.jsonl.gz)"
LICENCE_NOTES = (
    "Dataset: CC BY 4.0, globis-university/aozorabunko-clean (https://huggingface.co/datasets/globis-university/aozorabunko-clean). "
    "Texts: public domain, from Aozora Bunko (https://www.aozora.gr.jp/). Attribution to both is required."
)
MODERN_SPELLING = "新字新仮名"
MAX_KATAKANA_SHARE = 0.4  # katakana-only writing (テガミ) is hard to read and not what is being taught
PER_WORK_SLACK = 25   # candidates kept per work beyond --per-work, in case some fail to tokenize

KANJI_NOT_TO_LEARN = "々〆ヶ"
OPENERS = {"「": "」", "『": "』", "（": "）"}
CLOSERS = set(OPENERS.values())
TERMINATORS = "。！？"

# Hiragana, katakana, kanji (incl. 々〆ヶ), and the punctuation 、。！？「」『』（）―ー・ . Anything else (markup remnants,
# Latin letters and digits, くの字点, 〇, spaces inside the text, ellipses) means the paragraph is dropped.
ALLOWED_RE = re.compile(r"[ぁ-ゖァ-ヺー、。！？「」『』（）―・㐀-䶿一-鿿豈-﫿々〆ヶ]+")
ELLIPSES = "…‥"  # the owner doesn't want them in passages at all

# Old-fashioned writing that a modern learner shouldn't have to type: historical kana, 候, やう-/てふ/せう,
# ふ-verbs (言ふ, 思ふ, 給ふ, いふ。 and 言はず 言はぬ), 文語 endings (べし, けり, ざる, なり。, 知らぬ) and unwritten small っ (あつた).
ARCHAIC_RE = re.compile("|".join([
    "[ゐゑヰヱ]", "候", "やう", "てふ", "せう",
    "[一-鿿]ふ",                    # 言ふ 思ふ 給ふ 行ふ
    "(?<!ふ)[ぁ-ん]ふ(?=[。、！？」』）])",  # いふ。 おもふ、 (not ふふ)
    "(?<=[一-鿿])(?<!手)は[ずぬ]",     # 言はず 思はぬ (but 手はず)
    "べし", "けり(?=[、。！？」』）])", "ざる(?!を得|そば)", "給へ", "たまへ",
    "なり[。！？」』]", "(?<![死いぬ])ぬ(?=[。、！？」』）])",  # ...なり。 知らぬ。 (死ぬ and 犬 are fine)
    "つた(?![えわな])", "[一-鿿]つ[たて]",                     # あつた 食つて: small っ not written
]))


def is_archaic(paragraph):
    return bool(ARCHAIC_RE.search(paragraph))


def split_sentences(paragraph):
    """Split on 。！？ but never inside 「」『』（）, so 「今日は雨だ。明日も雨だ。」 stays one sentence.
    A closing bracket right after a terminator ends the sentence when a new quote starts or the paragraph
    ends (「雨だ。」「そうか。」 is two sentences); 「雨だ。」と言った。 stays whole. Returns None when the
    brackets don't balance, so the paragraph can be dropped."""
    sentences, start, stack, i, n = [], 0, [], 0, len(paragraph)

    def cut(end):
        nonlocal start
        sentence = paragraph[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = end

    while i < n:
        char = paragraph[i]
        if char in OPENERS:
            stack.append(OPENERS[char])
        elif char in CLOSERS:
            if not stack or stack.pop() != char:
                return None
            if not stack and paragraph[i - 1] in TERMINATORS and (i + 1 == n or paragraph[i + 1] in OPENERS):
                cut(i + 1)
        elif char in TERMINATORS and not stack:
            while i + 1 < n and paragraph[i + 1] in TERMINATORS:
                i += 1
            cut(i + 1)
        i += 1
    if stack:
        return None
    cut(n)
    return sentences


def kanji_shares(text, levels):
    """(kanji count, share in N5-N4, share in N3) over the kanji occurrences in the text; 々〆ヶ aren't counted."""
    kanji = [k for k in KANJI_RE.findall(text) if k not in KANJI_NOT_TO_LEARN]
    if not kanji:
        return 0, 0.0, 0.0
    counts = Counter(levels.get(k, 0) for k in kanji)
    return len(kanji), (counts[5] + counts[4]) / len(kanji), counts[3] / len(kanji)


def read_works(path, limit_works=None):
    """Stream the works in modern spelling from the jsonl.gz as (meta, text). Also yields (None, None) per skipped work."""
    kept = 0
    with gzip.open(path, "rt", encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            work = json.loads(line)
            if work["meta"].get("文字遣い種別") != MODERN_SPELLING:
                yield None, None
                continue
            yield work["meta"], work["text"]
            kept += 1
            if limit_works and kept >= limit_works:
                return


def paragraphs_of(text):
    """The work's paragraphs (non-blank lines) with their position among ALL of them, leading full-width space stripped."""
    position = 0
    for line in text.split("\n"):
        paragraph = strip_invisible(line).strip().strip("　").strip()
        if not paragraph:
            continue
        yield position, paragraph
        position += 1


class Command(BaseCommand):
    help = "Build the passages file (gzipped JSON Lines) for paragraph mode from the cleaned Aozora Bunko dataset."

    def add_arguments(self, parser):
        parser.add_argument("path", type=Path, help="aozorabunko-dedupe-clean.jsonl.gz")
        parser.add_argument("output", type=Path, help="passages file to write (.jsonl.gz)")
        parser.add_argument("--max-sentences", type=int, default=150000, help="stop after this many sentences in total")
        parser.add_argument("--max-sentence-chars", type=int, default=80, help="drop paragraphs with a longer sentence")
        parser.add_argument("--min-sentences", type=int, default=3, help="drop paragraphs with fewer sentences")
        parser.add_argument("--min-chars", type=int, default=120, help="drop paragraphs shorter than this")
        parser.add_argument("--min-kanji", type=int, default=5, help="drop paragraphs with fewer kanji (nothing to learn)")
        parser.add_argument("--per-work", type=int, default=150, help="most paragraphs taken from one work")
        parser.add_argument("--limit-works", type=int, help="only read the first N works in modern spelling (for trying it out)")

    def handle(self, path, output, max_sentences, max_sentence_chars, min_sentences, min_chars, min_kanji,
               per_work, limit_works, **options):
        if not path.exists():
            raise CommandError(f"{path} does not exist")
        options = {
            "max_sentences": max_sentences, "max_sentence_chars": max_sentence_chars, "min_sentences": min_sentences,
            "min_chars": min_chars, "min_kanji": min_kanji, "per_work": per_work, "limit_works": limit_works,
        }
        self.levels = jlpt_kanji()

        candidates, works, drops, totals = self.collect(path, options)
        self.stdout.write(
            f"Read {totals['works']} modern-spelling works ({totals['skipped']} other spellings skipped), "
            f"{totals['paragraphs']} paragraphs, {len(candidates)} candidates after the filters and the per-work cut."
        )
        for reason, count in drops.most_common():
            self.stdout.write(f"  dropped {count:>8}  {reason}")

        candidates.sort(key=lambda c: c[0])  # easiest first
        temp = output.with_name(output.name + ".partial")
        stats = self.write_passages(candidates, works, temp, options)
        self.write_output(temp, output, stats, options, totals)
        self.report(stats)

    # ---------------------------------------------------------------- reading and filtering

    def collect(self, path, options):
        """Filter every paragraph without tokenizing. Returns candidates sorted later, work info, drop counts, totals.
        A candidate is (sort key, work_id, position, sentences): per work only the best few are kept."""
        drops, totals, works, candidates = Counter(), Counter(), {}, []
        for meta, text in read_works(path, options["limit_works"]):
            if meta is None:
                totals["skipped"] += 1
                continue
            totals["works"] += 1
            work_id = meta["作品ID"]
            found = []
            for position, paragraph in paragraphs_of(text):
                totals["paragraphs"] += 1
                reason = self.reject(paragraph, options)
                if isinstance(reason, str):
                    drops[reason] += 1
                    continue
                sentences, shares = reason
                easy, n3, length = shares
                # Primarily the share of kanji in N5-N4 (to a whole percent, so length can break near-ties), then N3, then longer.
                found.append(((-round(easy, 2), -round(n3, 2), -length, work_id, position), work_id, position, sentences))
            found.sort(key=lambda c: c[0])
            for candidate in found[:options["per_work"] + PER_WORK_SLACK]:
                candidates.append(candidate)
            if found:
                works[work_id] = {
                    "title": meta["作品名"],
                    "author": " ".join(part for part in (meta.get("姓", ""), meta.get("名", "")) if part),
                    "url": meta.get("図書カードURL", ""),
                }
            if totals["works"] % 1000 == 0:
                self.stdout.write(f"  {totals['works']} works read")
        return candidates, works, drops, totals

    def reject(self, paragraph, options):
        """A reason string if the paragraph is dropped, else ((sentences), (easy share, N3 share, length))."""
        if any(mark in paragraph for mark in ELLIPSES):
            return "ellipsis (… or ‥)"
        if not ALLOWED_RE.fullmatch(paragraph):
            return "markup remnants, other characters, or full-width space inside the text"
        if len(paragraph) < options["min_chars"]:
            return "too short"
        if paragraph[-1] not in "。！？」』）":
            return "doesn't end like a sentence"
        if is_archaic(paragraph):
            return "archaic spelling or 文語"
        if katakana_count(paragraph) > len(paragraph) * MAX_KATAKANA_SHARE:
            return "mostly katakana"
        if not is_joyo_only(paragraph):
            return "kanji outside the jōyō list"
        count, easy, n3 = kanji_shares(paragraph, self.levels)
        if count < options["min_kanji"]:
            return "too few kanji"
        sentences = split_sentences(paragraph)
        if sentences is None:
            return "unbalanced brackets"
        if len(sentences) < options["min_sentences"]:
            return "too few sentences"
        if any(len(sentence) > options["max_sentence_chars"] for sentence in sentences):
            return "a sentence is too long"
        return sentences, (easy, n3, len(paragraph))

    # ---------------------------------------------------------------- tokenizing and writing

    def write_passages(self, candidates, works, temp, options):
        """Tokenize the candidates in ranked order and write the accepted passages to a temporary file,
        until the sentence budget is used up. Memory stays flat: only the word keys are kept."""
        words, kana_words = set(), set()
        per_work, lengths, level_counts = Counter(), Counter(), Counter()
        passages = sentence_total = 0
        failed = 0
        with open(temp, "w", encoding="utf-8") as out:
            for _, work_id, position, sentences in candidates:
                if per_work[work_id] >= options["per_work"]:
                    continue
                if sentence_total + len(sentences) > options["max_sentences"]:
                    break
                lines = self.tokenize_all(sentences)
                if lines is None:
                    failed += 1
                    continue
                work = works[work_id]
                out.write(json.dumps({
                    "source": "aozora", "work_id": work_id, "position": position, "title": work["title"],
                    "author": work["author"], "url": work["url"], "licence": "Public domain", "readability": None,
                    "sentences": lines,
                }, ensure_ascii=False) + "\n")
                for sentence in lines:
                    lengths[min(len(sentence["text"]) // 10 * 10, 80)] += 1
                    for _, _, word, kana in sentence["tokens"]:
                        if word:
                            words.add(tuple(word))
                        if kana:
                            kana_words.add(tuple(kana))
                    for kanji in KANJI_RE.findall(sentence["text"]):
                        if kanji not in KANJI_NOT_TO_LEARN:
                            level_counts[self.levels.get(kanji, 0)] += 1
                per_work[work_id] += 1
                passages += 1
                sentence_total += len(sentences)
                if passages % 1000 == 0:
                    self.stdout.write(f"  {passages} passages, {sentence_total} sentences tokenized")
        return {
            "works": len(per_work), "passages": passages, "sentences": sentence_total, "failed": failed,
            "words": words, "kana_words": kana_words, "lengths": lengths, "levels": level_counts,
        }

    def tokenize_all(self, sentences):
        """The sentences as output lines, or None if any fails the round trip or has no tokens."""
        lines = []
        for text in sentences:
            tokens = tokenize(text)
            if not tokens or "".join(token.surface for token in tokens) != text:
                return None
            lines.append({
                "text": text,
                "romaji": romaji(text),
                "tokens": [
                    [t.surface, t.reading, [t.word, t.word_reading] if t.word else None,
                     list(t.kana_word) if t.kana_word else None]
                    for t in tokens
                ],
            })
        return lines

    def dictionary_line(self, stats):
        """jlpt level and meanings for every referenced word (meanings from the cached JMdict, if there is one)."""
        words, kana_words = sorted(stats["words"]), sorted(stats["kana_words"])
        meanings, kana_meanings = {}, {}
        dictionary = cached_dictionary()
        if dictionary is None:
            self.stdout.write(self.style.WARNING(
                "No JMdict cached in data/: the file has no meanings (run `manage.py load_dictionary`, then build again)."))
        else:
            self.stdout.write(f"Looking up meanings in {dictionary.name}...")
            entries = read_entries(dictionary)
            kanji_index = index_by_kanji(entries, {text for text, _ in words} | {lemma for _, _, lemma in kana_words})
            kana_index = index_by_kana(entries, {reading for _, reading, _ in kana_words})
            del entries
            meanings = {key: find_meanings(kanji_index, *key) for key in words}
            kana_meanings = {key: find_kana_meanings(kanji_index, kana_index, *key) for key in kana_words}
        return {
            "kind": "dictionary",
            "words": [[text, reading, jlpt_level(text), meanings.get((text, reading), [])] for text, reading in words],
            "kana_words": [[text, reading, lemma, kana_meanings.get((text, reading, lemma), [])]
                           for text, reading, lemma in kana_words],
        }, dictionary is not None

    def write_output(self, temp, output, stats, options, totals):
        dictionary, has_meanings = self.dictionary_line(stats)
        header = {
            "kind": "header", "format": FORMAT_VERSION, "source": "aozora", "dataset": DATASET,
            "licence_notes": LICENCE_NOTES, "built": date.today().isoformat(),
            "options": {key: value for key, value in options.items()},
            "counts": {
                "works": stats["works"], "passages": stats["passages"], "sentences": stats["sentences"],
                "words": len(dictionary["words"]), "kana_words": len(dictionary["kana_words"]),
                "works_read": totals["works"], "meanings_included": has_meanings,
            },
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        partial = output.with_name(output.name + ".gz-partial")
        with gzip.open(partial, "wt", encoding="utf-8", compresslevel=6) as out:
            out.write(json.dumps(header, ensure_ascii=False) + "\n")
            out.write(json.dumps(dictionary, ensure_ascii=False) + "\n")
            with open(temp, encoding="utf-8") as lines:
                for line in lines:
                    out.write(line)
        os.replace(partial, output)
        os.remove(temp)
        size = output.stat().st_size / 1e6
        self.stdout.write(self.style.SUCCESS(f"Wrote {output} ({size:.1f} MB)"))

    def report(self, stats):
        total_sentences = max(stats["sentences"], 1)
        self.stdout.write(
            f"Kept {stats['passages']} passages from {stats['works']} works: {stats['sentences']} sentences, "
            f"{len(stats['words'])} words, {len(stats['kana_words'])} kana words "
            f"({stats['failed']} candidates failed to tokenize)."
        )
        self.stdout.write("Sentence length (characters):")
        for bucket in sorted(stats["lengths"]):
            label = f"{bucket}+" if bucket == 80 else f"{bucket}-{bucket + 9}"
            self.stdout.write(f"  {label:>6}  {stats['lengths'][bucket]:>7}  {stats['lengths'][bucket] / total_sentences:6.1%}")
        kanji_total = max(sum(stats["levels"].values()), 1)
        self.stdout.write("Kanji occurrences by JLPT level: " + ", ".join(
            f"{'N' + str(level) if level else 'beyond N1'} {stats['levels'][level] / kanji_total:.1%}"
            for level in (5, 4, 3, 2, 1, 0)))
