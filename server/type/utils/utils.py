import pandas as pd
import re 

kanji = ''.join(line.strip() for line in open('assets/kanjiJLPT.txt', 'r', encoding='utf-8').readlines())


def contains_kanji(sentence, kanji_list):
    return any(kanji in sentence for kanji in kanji_list)


def contains_only_allowed_kanji(sentence: str, kanji_list: str) -> bool:
    kanji_list = [i for i in kanji_list]  # type: ignore
    allowed_kanji_set = set(kanji_list)
    kanji_in_sentence = re.findall(r'[\u4e00-\u9faf]', sentence)
    return all(kanji in allowed_kanji_set for kanji in kanji_in_sentence)