import gzip
import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from .corpus import is_joyo_only
from .management.commands.build_corpus import Command as BuildCorpus
from .management.commands.build_passages import Command as BuildPassages, split_sentences
from .models import KanaWord, Passage, PassageSentence, Sentence, Word
from .tests import NO_DICTIONARY, build_corpus, write_dictionary


GOOD = "今日は雨です。先生は学校に来ました。私は水を飲みました。"
HARD = "剣は危険です。魔物は恐怖です。私は水を飲みました。"
DIALOGUE = "「今日は雨です。先生は学校に来ました。」と私は言いました。私は水を飲みました。明日も雨でしょう。"
LONG = "今日は雨です。" + "先生は学校に来ましたが" * 9 + "私は水を飲みました。私は水を飲みました。"
SHORT_DIALOGUE = "「雨です。」"

BAD = {
    "archaic": "今日は雨です。先生は学校へ行かうとしてゐた。私は水を飲みました。",
    "archaic ふ": "今日は雨です。先生は学校に来たと言ふ。私は水を飲みました。",
    "markup": "今日は雨です。先生は［＃ここから］学校に来ました。私は水を飲みました。",
    "ruby remnant": "今日は雨です。先生は学校《がっこう》に来ました。私は水を飲みました。",
    "latin": "今日は雨です。先生はABCの学校に来ました。私は水を飲みました。",
    "wakachi": "今日は　雨です。先生は　学校に　来ました。私は　水を　飲みました。",
    "non-joyo": "今日は雨だと云った。先生は学校に来ました。私は水を飲みました。",
    "too long": LONG,
    "few sentences": "今日は雨です。先生は学校に来ました。",
    "no end": "今日は雨です。先生は学校に来ました。私は水を飲みました、",
    "unbalanced": "今日は雨です。「先生は学校に来ました。私は水を飲みました。",
}


def meta(work_id, title, spelling="新字新仮名", family="山田", given="太郎"):
    return {"作品ID": work_id, "作品名": title, "姓": family, "名": given, "文字遣い種別": spelling,
            "図書カードURL": f"https://www.aozora.gr.jp/cards/{work_id}/card.html"}


def write_works(path, works):
    with gzip.open(path, "wt", encoding="utf-8") as file:
        for work_meta, lines in works:
            file.write(json.dumps({"text": "\n".join(lines), "footnote": "", "meta": work_meta}, ensure_ascii=False) + "\n")


FEW = ["--min-chars", "20", "--min-kanji", "3"]


def read_output(path):
    with gzip.open(path, "rt", encoding="utf-8") as file:
        rows = [json.loads(line) for line in file]
    return rows[0], rows[1], rows[2:]


class SplitTests(TestCase):
    def test_no_split_inside_quotes_or_brackets(self):
        self.assertEqual(split_sentences(DIALOGUE), [
            "「今日は雨です。先生は学校に来ました。」と私は言いました。", "私は水を飲みました。", "明日も雨でしょう。"])
        self.assertEqual(split_sentences("雨（本当だ。）です。ええ！？はい。"), ["雨（本当だ。）です。", "ええ！？", "はい。"])

    def test_back_to_back_quotes_are_separate_sentences(self):
        self.assertEqual(split_sentences("「雨だ。」「そうか。」"), ["「雨だ。」", "「そうか。」"])

    def test_unbalanced_brackets_are_rejected(self):
        self.assertIsNone(split_sentences("「雨です。"))
        self.assertIsNone(split_sentences("雨です。」"))

    def test_paragraphs_with_ellipses_are_dropped(self):
        for text in ("先生は学校に来ました……。私は水を飲みました。", "「雨です‥」と私は言いました。"):
            self.assertEqual(BuildPassages().reject(text, {}), "ellipsis (… or ‥)")


@NO_DICTIONARY
class BuildPassagesTests(TestCase):
    def build(self, works, *extra):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        source, output = Path(self.folder.name) / "in.jsonl.gz", Path(self.folder.name) / "out.jsonl.gz"
        write_works(source, works)
        out = StringIO()
        call_command("build_passages", str(source), str(output), *FEW, *extra, stdout=out)
        self.output = out.getvalue()
        return read_output(output)

    def works(self):
        lines_a = ["　" + GOOD, "", "　" + HARD, SHORT_DIALOGUE, "　" + DIALOGUE]
        lines_a += ["　" + text for text in BAD.values()]
        return [
            (meta("000001", "雨の日"), lines_a),
            (meta("000002", "古い話", spelling="旧字旧仮名"), ["　" + GOOD, "　" + HARD]),
            (meta("000003", "水", given=""), ["題", "", "　" + HARD, "　" + GOOD]),
        ]

    def test_fixture_is_what_the_test_thinks_it_is(self):
        self.assertTrue(is_joyo_only(GOOD) and is_joyo_only(HARD))
        self.assertFalse(is_joyo_only(BAD["non-joyo"]))

    def test_filters_keep_only_the_good_paragraphs(self):
        header, dictionary, passages = self.build(self.works())
        kept = {(p["work_id"], p["position"]): "".join(s["text"] for s in p["sentences"]) for p in passages}
        # Positions count every non-blank paragraph of the work, kept or not (000001: GOOD 0, HARD 1, SHORT 2, DIALOGUE 3 ...).
        self.assertEqual(kept, {
            ("000001", 0): GOOD, ("000001", 1): HARD, ("000001", 3): DIALOGUE,
            ("000003", 1): HARD, ("000003", 2): GOOD,
        })  # none of BAD, nothing from the old-spelling work 000002
        self.assertEqual(header["counts"]["works"], 2)
        self.assertEqual(header["counts"]["passages"], 5)

    def test_sentence_split_rule_and_output_shape(self):
        header, dictionary, passages = self.build(self.works())
        self.assertEqual((header["kind"], header["source"], dictionary["kind"]), ("header", "aozora", "dictionary"))
        self.assertIn("CC BY 4.0", header["licence_notes"])
        self.assertEqual(header["options"]["per_work"], 150)
        self.assertFalse(header["counts"]["meanings_included"])

        dialogue = next(p for p in passages if p["position"] == 3)
        self.assertEqual([s["text"] for s in dialogue["sentences"]][0], "「今日は雨です。先生は学校に来ました。」と私は言いました。")
        self.assertEqual(len(dialogue["sentences"]), 3)
        self.assertEqual(
            {k: v for k, v in dialogue.items() if k != "sentences"},
            {"source": "aozora", "work_id": "000001", "position": 3, "title": "雨の日", "author": "山田 太郎",
             "url": "https://www.aozora.gr.jp/cards/000001/card.html", "licence": "Public domain", "readability": None})
        self.assertEqual(next(p for p in passages if p["work_id"] == "000003")["author"], "山田")

        sentence = passages[0]["sentences"][0]
        self.assertTrue(sentence["romaji"])
        self.assertEqual("".join(token[0] for token in sentence["tokens"]), sentence["text"])
        by_surface = {token[0]: token for token in sentence["tokens"]}
        self.assertEqual(by_surface["雨"][2], ["雨", "あめ"])
        self.assertIsNone(by_surface["雨"][3])
        self.assertIsNone(by_surface["は"][2])

        words = {(text, reading): jlpt for text, reading, jlpt, meanings in dictionary["words"]}
        self.assertEqual(words[("雨", "あめ")], 5)
        self.assertEqual(words[("剣", "けん")], 1)
        referenced = {tuple(t[2]) for p in passages for s in p["sentences"] for t in s["tokens"] if t[2]}
        self.assertEqual(referenced, set(words))

    def test_easiest_paragraphs_come_first(self):
        _, _, passages = self.build(self.works())
        order = ["".join(s["text"] for s in p["sentences"]) for p in passages]
        self.assertEqual(order[-2:], [HARD, HARD])  # the N1-heavy text sits at the hard end
        self.assertIn(order[0], (GOOD, DIALOGUE))  # all N5-N4 kanji; the longer text wins a tie

    def test_limits(self):
        _, _, passages = self.build(self.works(), "--per-work", "1")
        self.assertEqual(sorted(p["work_id"] for p in passages), ["000001", "000003"])  # the best of each work
        _, _, passages = self.build(self.works(), "--max-sentences", "7")
        self.assertEqual(sum(len(p["sentences"]) for p in passages), 6)
        _, _, passages = self.build(self.works(), "--limit-works", "1")
        self.assertEqual({p["work_id"] for p in passages}, {"000001"})
        _, _, passages = self.build(self.works(), "--max-sentence-chars", "200")
        self.assertIn(LONG, {"".join(s["text"] for s in p["sentences"]) for p in passages})

    def test_meanings_come_from_a_cached_dictionary(self):
        with tempfile.TemporaryDirectory() as data:
            write_dictionary(data)
            with override_settings(DATA_DIR=Path(data)):
                header, dictionary, _ = self.build(self.works())
        self.assertTrue(header["counts"]["meanings_included"])
        meanings = {(text, reading): m for text, reading, _, m in dictionary["words"]}
        self.assertEqual(meanings[("雨", "あめ")], ["rain"])


@NO_DICTIONARY
class LoadPassagesTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        source, self.file = Path(folder.name) / "in.jsonl.gz", Path(folder.name) / "out.jsonl.gz"
        write_works(source, [
            (meta("000001", "雨の日"), ["　" + GOOD, "　" + HARD]),
            (meta("000003", "水"), ["　" + GOOD]),
        ])
        call_command("build_passages", str(source), str(self.file), *FEW, stdout=StringIO())
        # What the corpus already has: the first sentence (typed in sentence mode, translated) and the word 雨.
        self.known = Sentence.objects.create(text="今日は雨です。", translation="It is raining today.", tokens=[], standalone=True)
        self.rain = Word.objects.create(text="雨", reading="あめ", frequency=7, jlpt=5)

    def load(self, *args):
        out = StringIO()
        call_command("load_passages", str(self.file), *args, stdout=out)
        return out.getvalue()

    def test_loads_words_sentences_and_links(self):
        self.load()
        self.assertEqual(Passage.objects.count(), 3)  # GOOD and HARD of one work, GOOD of the other
        # GOOD and HARD share 私は水を飲みました。, and GOOD's first sentence already existed: 5 rows in all
        self.assertEqual(Sentence.objects.count(), 5)

        self.rain.refresh_from_db()
        self.assertEqual(self.rain.frequency, 7)  # existing words are left alone
        self.assertEqual(Word.objects.get(text="剣").frequency, 0)  # new ones are never queued for learning
        self.assertEqual(Word.objects.get(text="剣").jlpt, 1)
        self.assertEqual(Word.objects.filter(text="雨").count(), 1)

        self.known.refresh_from_db()
        self.assertTrue(self.known.standalone)  # reused, left as it was
        self.assertEqual(self.known.translation, "It is raining today.")
        self.assertEqual(self.known.tokens, [])
        fresh = Sentence.objects.get(text="私は水を飲みました。")
        self.assertFalse(fresh.standalone)
        self.assertEqual(fresh.translation, "")
        self.assertTrue(fresh.romaji)
        self.assertEqual(Sentence.objects.filter(standalone=False).count(), 4)

        # tokens hold ids, and sentence_words links match them
        word_ids = {t[2] for t in fresh.tokens if t[2]}
        self.assertEqual(set(fresh.words.values_list("id", flat=True)), word_ids)
        self.assertIn(Word.objects.get(text="水").id, word_ids)
        self.assertEqual(self.known.words.count(), 0)  # a reused sentence's links are not touched

        passage = Passage.objects.get(work_id="000001", position=0)
        self.assertEqual(passage.licence, "Public domain")
        self.assertEqual(passage.author, "山田 太郎")
        self.assertEqual(
            [(line.position, line.sentence.text) for line in passage.lines.all()],
            [(0, "今日は雨です。"), (1, "先生は学校に来ました。"), (2, "私は水を飲みました。")])
        self.assertEqual(PassageSentence.objects.count(), 9)
        self.assertEqual(Passage.objects.get(work_id="000003").lines.first().sentence_id, self.known.id)

    def test_loading_twice_changes_nothing(self):
        self.load()
        before = (Passage.objects.count(), Sentence.objects.count(), PassageSentence.objects.count(), Word.objects.count())
        self.assertIn("3 already there", self.load())
        self.assertEqual(
            before, (Passage.objects.count(), Sentence.objects.count(), PassageSentence.objects.count(), Word.objects.count()))

    def test_replace_swaps_the_sources_passages_and_drops_orphaned_sentences(self):
        self.load()
        standalone = Sentence.objects.create(text="明日も雨です。", tokens=[], standalone=True)
        orphan = Sentence.objects.create(text="これは古い。", tokens=[], standalone=False)
        # Shrink the file's world: drop the second work's passage by deleting it, then --replace brings it back.
        Passage.objects.filter(work_id="000003").delete()
        Passage.objects.filter(work_id="000001", position=1).delete()  # HARD: its sentences are now in no passage
        self.load("--replace")
        self.assertEqual(Passage.objects.count(), 3)
        self.assertFalse(Sentence.objects.filter(id=orphan.id).exists())  # passage-only and in no passage
        self.assertTrue(Sentence.objects.filter(id=standalone.id).exists())
        self.assertTrue(Sentence.objects.filter(id=self.known.id).exists())
        self.assertEqual(Sentence.objects.filter(passages__isnull=True, standalone=False).count(), 0)
        self.assertEqual(Sentence.objects.filter(standalone=False).count(), 4)

    def test_replace_leaves_other_sources_alone(self):
        other = Passage.objects.create(source="other", work_id="1", position=0, title="t", author="a", licence="l")
        PassageSentence.objects.create(passage=other, sentence=self.known, position=0)
        self.load("--replace")
        self.assertTrue(Passage.objects.filter(source="other").exists())
        self.assertEqual(Passage.objects.filter(source="aozora").count(), 3)

    def test_kana_words_get_rows_and_ids(self):
        sentence = {"text": "つぐむ。", "romaji": "tsugumu.", "tokens": [
            ["つぐむ", "", None, ["つぐむ", "つぐむ", "噤む"]], ["。", "", None, None]]}
        rows = [
            {"kind": "header", "source": "test", "counts": {}},
            {"kind": "dictionary", "words": [], "kana_words": [["つぐむ", "つぐむ", "噤む", ["to shut one's mouth"]]]},
            {"source": "test", "work_id": "1", "position": 0, "title": "t", "author": "a", "url": "", "licence": "l",
             "readability": None, "sentences": [sentence]},
        ]
        with tempfile.TemporaryDirectory() as folder:
            self.file = Path(folder) / "kana.jsonl"
            self.file.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
            self.load()
        kana = KanaWord.objects.get(text="つぐむ")
        self.assertEqual((kana.lemma, kana.meanings), ("噤む", ["to shut one's mouth"]))
        self.assertEqual(Sentence.objects.get(text="つぐむ。").tokens[0], ["つぐむ", "", None, kana.id])

    def test_rejects_other_files(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / "bad.jsonl.gz"
            with gzip.open(bad, "wt", encoding="utf-8") as file:
                file.write('{"hello": 1}\n{"x": 2}\n')
            with self.assertRaises(CommandError):
                call_command("load_passages", str(bad), stdout=StringIO())
            with self.assertRaises(CommandError):
                call_command("load_passages", str(Path(folder) / "missing.jsonl.gz"), stdout=StringIO())

    def test_loader_needs_no_fugashi_cutlet_or_pandas(self):
        code = (
            "import django, sys; django.setup();"
            "import type.management.commands.load_passages;"
            "bad = [m for m in ('fugashi', 'cutlet', 'pandas', 'unidic_lite') if m in sys.modules];"
            "sys.exit(','.join(bad) or 0)"
        )
        env = {**os.environ, "DJANGO_SETTINGS_MODULE": "server.settings"}
        result = subprocess.run([sys.executable, "-c", code], cwd=settings.BASE_DIR, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


@NO_DICTIONARY
class PassagesVersusCorpusTests(TestCase):
    def setUp(self):
        self.word = Word.objects.create(text="雨", reading="あめ", frequency=99)
        self.only_passage = Word.objects.create(text="剣", reading="けん", frequency=99)
        through = Sentence.words.through
        self.standalone = Sentence.objects.create(text="雨です。", tokens=[], standalone=True)
        self.in_passage = Sentence.objects.create(text="剣は雨です。", tokens=[], standalone=False)
        through.objects.create(sentence=self.standalone, word=self.word)
        through.objects.create(sentence=self.in_passage, word=self.word)
        through.objects.create(sentence=self.in_passage, word=self.only_passage)
        passage = Passage.objects.create(source="aozora", work_id="1", position=0, title="t", author="a", licence="l")
        PassageSentence.objects.create(passage=passage, sentence=self.in_passage, position=0)

    def test_frequencies_count_standalone_sentences_only(self):
        BuildCorpus().update_frequencies()
        self.word.refresh_from_db()
        self.only_passage.refresh_from_db()
        self.assertEqual(self.word.frequency, 1)  # not 2
        self.assertEqual(self.only_passage.frequency, 0)  # only in a passage: never taught

    def test_replace_keeps_passage_sentences_and_their_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.txt", "今日は雨です。", replace=True)
        self.assertFalse(Sentence.objects.filter(id=self.standalone.id).exists())  # old standalone corpus is gone
        self.assertTrue(Sentence.objects.filter(id=self.in_passage.id).exists())
        self.assertEqual(PassageSentence.objects.count(), 1)
        self.assertTrue(Sentence.objects.filter(text="今日は雨です。", standalone=True).exists())
        self.only_passage.refresh_from_db()
        self.assertEqual(self.only_passage.frequency, 0)

    def test_replace_turns_a_standalone_sentence_in_a_passage_into_passage_only_text(self):
        Sentence.objects.filter(id=self.in_passage.id).update(standalone=True)
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.txt", "今日は雨です。", replace=True)
        self.in_passage.refresh_from_db()
        self.assertFalse(self.in_passage.standalone)

    def test_loading_a_corpus_sentence_that_was_passage_only_makes_it_standalone(self):
        with tempfile.TemporaryDirectory() as folder:
            build_corpus(Path(folder) / "corpus.txt", "剣は雨です。")
        self.in_passage.refresh_from_db()
        self.assertTrue(self.in_passage.standalone)
        self.assertEqual(Sentence.objects.filter(text="剣は雨です。").count(), 1)
