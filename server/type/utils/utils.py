import pandas as pd
import re 
import json


kanji_data = {}

with open("assets/kanji_data.json", 'r', encoding='utf-8') as file:
    kanji_data = json.load(file)


def contains_kanji(sentence, kanji_list):
    return any(kanji in sentence for kanji in kanji_list)


def contains_only_allowed_kanji(sentence: str, kanji_list: str) -> bool:
    kanji_list = [i for i in kanji_list]  # type: ignore
    allowed_kanji_set = set(kanji_list)
    kanji_in_sentence = re.findall(r'[\u4e00-\u9faf]', sentence)
    return all(kanji in allowed_kanji_set for kanji in kanji_in_sentence)


def fetch_kanji_data(kanji_list):
    data = []
    for kanji in kanji_list:
        temp = kanji_data[kanji]
        temp["kanji"] = kanji
        data.append(temp)

    return data
    