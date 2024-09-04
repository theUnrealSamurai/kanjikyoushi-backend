import pandas as pd
import re 
import json

df = pd.read_csv("assets/kanji_data.tsv", sep='\t')
kanji_data = {}


def safe_eval(x):
    try:
        return eval(x)
    except:
        return x


for _, row in df.iterrows():
    kanji = row['kanji']
    kanji_data[kanji] = {
        'on_readings': safe_eval(row['on_readings']),
        'kun_readings': safe_eval(row['kun_readings']),
        'stroke_count': row['stroke_count'],
        'meanings': safe_eval(row['meanings'])
    }


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
    