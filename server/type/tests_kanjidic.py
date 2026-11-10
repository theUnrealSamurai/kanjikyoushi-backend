"""The kanji cards list each kanji's meanings and readings in KANJIDIC's order (main sense first), not alphabetically."""
from django.test import SimpleTestCase

from .corpus import kanji_dictionary, kanji_info


class KanjiInfoTests(SimpleTestCase):
    def test_main_sense_first(self):
        kanji = {item["kanji"]: item for item in kanji_info("結婚")}
        self.assertEqual(kanji["結"]["meanings"][0], "tie")
        self.assertEqual(list(kanji), ["結", "婚"])

    def test_common_reading_first(self):
        mei = kanji_info("説明")[1]
        self.assertEqual(mei["kanji"], "明")
        self.assertEqual(mei["on_readings"][0], "メイ")

    def test_same_shape_as_before(self):
        item = kanji_info("明")[0]
        self.assertEqual(set(item), {"kanji", "on_readings", "kun_readings", "meanings"})

    def test_every_kanji_of_the_old_data_has_a_card(self):
        self.assertEqual(len(kanji_info("".join(kanji_dictionary()))), len(kanji_dictionary()))
