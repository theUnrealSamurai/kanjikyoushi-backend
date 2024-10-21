import re
import json
import random
import pandas as pd
from .utils import *


def load_and_merge_json_files(directory: str):
    merged_data = {}
    
    # List all JSON files in the directory
    json_files = [f"{directory}/kanji_indices_N{i}.json" for i in ["1", "2", "3", "4", "5"]]
    
    for file_path in json_files:
        with open(file_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
            for key, value in data.items():
                if key in merged_data:
                    merged_data[key].extend(value)
                else:
                    merged_data[key] = value

    return merged_data

kanji_indices = load_and_merge_json_files("assets")


def fetch_practice_sentence(kanji: str):
    index = random.choice(kanji_indices[kanji])
    row = fetch_psql_row(index)
    ichiran_data = get_ichiran_data(row[1])

    return {
        "japanese": row[1],
        "english": translate(row[1]),
        "romaji": ichiran_data.split("\n\n*")[0].strip(),
        "kanji_data": fetch_kanji_data(re.findall(r'[\u4e00-\u9faf]', row[1])),
        "definitions": ichiran_data.split("\n\n")[1:],
    }



def fetch_revision_sentence(due_kanji, unknown_kanji, maxrow):
    index = search_max_kanji_match(sparse_matrix, sparse_matrix_kanji, due_kanji, unknown_kanji, maxrow)
    row = fetch_psql_row(index)
    ichiran_data = get_ichiran_data(row[1])
    
    return {
        "japanese": row[1],
        "english": translate(row[1]), 
        "romaji": ichiran_data.split("\n\n*")[0].strip(),
        "kanji_data": fetch_kanji_data(re.findall(r'[\u4e00-\u9faf]', row[1])),
        "definitions": ichiran_data.split("\n\n")[1:],
    }