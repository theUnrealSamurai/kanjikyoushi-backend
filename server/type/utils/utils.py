import pandas as pd
import re 
import json


kanji_data, max_row_threshold = {}, 300000

with open("assets/kanji_data.json", 'r', encoding='utf-8') as file:
    kanji_data = json.load(file)

with open("assets/kanji_end_index.json", 'r', encoding='utf-8') as file:
    kanji_end_index = json.load(file)


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
    

def get_minrows(due_kanji, maxrows):
    minrows = 0
    while True:
        minrows = min([kanji_end_index[kanji] for kanji in due_kanji])
        if maxrows - minrows <= max_row_threshold:
            break
        min_kanji = min(due_kanji, key=lambda k: kanji_end_index.get(k, maxrows))
        due_kanji.remove(min_kanji)

    return minrows