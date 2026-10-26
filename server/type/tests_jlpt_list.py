import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from .corpus import has_kanji
from .jlpt_list import Entry, apply_list_levels, list_levels, parse_list
from .models import Word


def write_lists(folder, **rows):
    """Write n1.csv ... n5.csv into folder from {"n5": [(expression, reading), ...]}; levels not given are empty."""
    for level in range(1, 6):
        lines = ["expression,reading,meaning,tags,guid"]
        lines += [f'"{expression}","{reading}",meaning,JLPT,guid{number}'
                  for number, (expression, reading) in enumerate(rows.get(f"n{level}", []))]
        (Path(folder) / f"n{level}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")


def entries_of(**rows):
    with tempfile.TemporaryDirectory() as folder:
        write_lists(folder, **rows)
        return parse_list(Path(folder))


class ParseTests(SimpleTestCase):
    def only(self, expression, reading):
        (entry,) = entries_of(n5=[(expression, reading)])
        return entry

    def test_every_level_is_read_easiest_first(self):
        entries = entries_of(n5=[("水", "みず")], n4=[("犬", "いぬ")], n1=[("猫", "ねこ")])
        self.assertEqual([(e.level, e.variants) for e in entries], [(5, ["水"]), (4, ["犬"]), (1, ["猫"])])

    def test_prefix_in_brackets_is_dropped(self):
        entry = self.only("〜 (まる) ごと", "〜 (まる) ごと")
        self.assertEqual((entry.variants, entry.readings, entry.affix), (["ごと"], ["ごと"], True))

    def test_context_in_brackets_is_dropped(self):
        entry = self.only("(かさを～) さす", "(かさを～) さす")
        self.assertEqual((entry.variants, entry.readings, entry.affix), (["さす"], ["さす"], True))

    def test_fullwidth_brackets_are_dropped(self):
        self.assertEqual(self.only("（お）茶", "（お）ちゃ").variants, ["茶"])

    def test_suffix_marker_is_dropped(self):
        entry = self.only("～位", "～い")
        self.assertEqual((entry.variants, entry.readings, entry.affix), (["位"], ["い"], True))

    def test_plain_word_is_not_an_affix(self):
        self.assertFalse(self.only("水", "みず").affix)

    def test_spelling_variants_are_split(self):
        entry = self.only("足; 脚", "あし")
        self.assertEqual((entry.variants, entry.readings), (["足", "脚"], ["あし"]))

    def test_okurigana_is_shared_with_the_spellings_before_it(self):
        entry = self.only("堅; 硬; 固い", "かたい")
        self.assertEqual(entry.variants, ["堅", "堅い", "硬", "硬い", "固い"])
        self.assertEqual(self.only("伯父; 叔父さん", "おじさん").variants, ["伯父", "伯父さん", "叔父さん"])

    def test_readings_are_hiragana(self):
        self.assertEqual(self.only("頑張る; 頑張る", "ガンバる; がんばる").readings, ["がんばる", "がんばる"])

    def test_kana_only_entry_has_no_kanji_variant_to_match(self):
        entry = self.only("ああ", "ああ")
        self.assertEqual(entry.variants, ["ああ"])
        self.assertEqual(list_levels([entry], [(1, "ああ", "ああ", 5)]), {})


class MatchTests(SimpleTestCase):
    def entry(self, level, expression, reading):
        (entry,) = entries_of(**{f"n{level}": [(expression, reading)]})
        return entry

    def test_reading_match_beats_a_more_frequent_text_only_match(self):
        words = [(1, "上手", "じょうず", 5), (2, "上手", "うわて", 500)]
        self.assertEqual(list_levels([self.entry(4, "上手", "じょうず")], words), {1: 4})

    def test_frequency_above_zero_beats_zero_when_neither_reading_matches(self):
        words = [(1, "上手", "うわて", 0), (2, "上手", "かみて", 3)]
        self.assertEqual(list_levels([self.entry(4, "上手", "じょうず")], words), {2: 4})

    def test_more_frequent_wins_among_the_rest(self):
        words = [(1, "上手", "うわて", 2), (2, "上手", "かみて", 30)]
        self.assertEqual(list_levels([self.entry(4, "上手", "じょうず")], words), {2: 4})

    def test_one_word_per_entry(self):
        words = [(1, "足", "あし", 10), (2, "脚", "あし", 4), (3, "足", "そく", 8)]
        self.assertEqual(list_levels([self.entry(5, "足; 脚", "あし")], words), {1: 5})

    def test_either_spelling_matches(self):
        words = [(1, "脚", "あし", 4)]
        self.assertEqual(list_levels([self.entry(5, "足; 脚", "あし")], words), {1: 5})

    def test_okurigana_variant_matches(self):
        words = [(1, "堅い", "かたい", 4), (2, "固い", "かたい", 9)]
        self.assertEqual(list_levels([self.entry(4, "堅; 硬; 固い", "かたい")], words), {2: 4})

    def test_affix_needs_the_reading(self):
        words = [(1, "位", "くらい", 50), (2, "位", "い", 3)]
        self.assertEqual(list_levels([self.entry(2, "～位", "～い")], words), {2: 2})
        self.assertEqual(list_levels([self.entry(2, "～位", "～い")], words[:1]), {})

    def test_easiest_level_wins(self):
        words = [(1, "会う", "あう", 20)]
        entries = [self.entry(3, "会う", "あう"), self.entry(5, "会う", "あう"), self.entry(4, "会う", "あう")]
        self.assertEqual(list_levels(entries, words), {1: 5})

    def test_unlisted_words_are_absent(self):
        self.assertEqual(list_levels([self.entry(5, "水", "みず")], [(1, "火", "ひ", 9)]), {})

    def test_entries_can_be_built_by_hand(self):
        entry = Entry(level=2, expression="水", variants=["水"], readings=["みず"], affix=False)
        self.assertEqual(list_levels([entry], [(7, "水", "みず", 1)]), {7: 2})


class ApplyTests(TestCase):
    def make(self, text, reading, frequency=1, list_level=None):
        return Word.objects.create(text=text, reading=reading, frequency=frequency, jlpt=5, list_level=list_level)

    def lists(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        write_lists(folder.name, n5=[("水", "みず"), ("会う", "あう")], n4=[("会う", "あう"), ("犬", "いぬ")],
                    n3=[("～円", "～えん")])
        return Path(folder.name)

    def test_sets_levels_and_clears_the_rest(self):
        water, meet, dog = self.make("水", "みず"), self.make("会う", "あう"), self.make("犬", "いぬ", frequency=0)
        yen, other = self.make("円", "えん"), self.make("火", "ひ", list_level=2)
        stale = self.make("木", "き", list_level=5)

        counts = apply_list_levels(Word, self.lists())

        self.assertEqual(counts, {5: 2, 4: 1, 3: 1, 2: 0, 1: 0})
        levels = dict(Word.objects.values_list("id", "list_level"))
        self.assertEqual(levels, {water.id: 5, meet.id: 5, dog.id: 4, yen.id: 3, other.id: None, stale.id: None})

    def test_second_run_changes_nothing(self):
        self.make("水", "みず")
        folder = self.lists()
        apply_list_levels(Word, folder)
        with self.assertNumQueries(1):  # only the read: no row needs writing
            apply_list_levels(Word, folder)

    def test_many_rows_are_written_in_chunks(self):
        Word.objects.bulk_create([Word(text=f"水{number}", reading="みず", jlpt=5) for number in range(2000)])
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        write_lists(folder.name, n5=[(f"水{number}", "みず") for number in range(2000)])
        self.assertEqual(apply_list_levels(Word, Path(folder.name))[5], 2000)
        self.assertEqual(Word.objects.filter(list_level=5).count(), 2000)

    def test_command_reports_counts(self):
        self.make("水", "みず")
        self.make("犬", "いぬ", frequency=0)
        out = StringIO()
        call_command("load_jlpt_list", stdout=out)
        self.assertIn("words are on the list", out.getvalue())


class RealListTests(SimpleTestCase):
    def test_bundled_files_parse(self):
        entries = parse_list()
        for level in range(1, 6):
            self.assertGreater(sum(1 for entry in entries if entry.level == level), 100, level)
        # A few kana-only rows have an empty reading column; any row with a kanji spelling has one.
        self.assertTrue(all(entry.readings for entry in entries if any(has_kanji(v) for v in entry.variants)))
