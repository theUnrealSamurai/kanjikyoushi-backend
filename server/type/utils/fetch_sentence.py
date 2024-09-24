import re
import json
import random
import pandas as pd
from .utils import contains_kanji, contains_only_allowed_kanji, fetch_kanji_data


def load_and_merge_json_files(directory: str):
    merged_data = {}
    
    # List all JSON files in the directory
    json_files = [f"{directory}/indexed_sentences_N{i}.json" for i in range(1, 6)]
    
    for file_path in json_files:
        with open(file_path, 'r', encoding='utf-8') as file:
            data = json.load(file)
            for key, value in data.items():
                if key in merged_data:
                    merged_data[key].extend(value)
                else:
                    merged_data[key] = value

    return merged_data

merged_data = load_and_merge_json_files("assets")


def fetch_practice_sentence(kanji: str):
    jp = random.choice(merged_data[kanji])
    return {
        "japanese": jp,
        "english": "This is a sample english sentence.",
        "romaji": "This is a sample romaji sentence.",
        "kanji": fetch_kanji_data([kanji]),
        "vocabulary": [
            {"日本語": ["Japanese", "Japanese Language"]},
            {"日本": ["Japan"]},
            {"語": ["Language"]},
        ],
    }


def fetch_learning_sentence(learning_kanji: str, learned_kanji: str):
    return {
        "japanese": "Not implemented yet",
        "english": "Not implemented yet",
        "romaji": "Not implemented yet",
        "kanji": "Not implemented yet",
        "vocabulary": [
            {"Nope": ["Nope", "Not implemented yet"]},
            {"Nope": ["Nay"]},
            {"Nah": ["Not Implemented yet"]},
        ],
    }


def fetch_test_sentence(learned_kanji: str, test_kanji: str):
    global df
    df = df[df['JLPT'] == "N5"]

    # Filter all the sentences containing the testing kanji
    indexes_with_kanji = df[df['jp'].apply(lambda x: contains_kanji(x, test_kanji))].index.tolist()

    # Filter sentences containing only testable kanji + learning kanji
    filtered_indexes = [index for index in indexes_with_kanji if contains_only_allowed_kanji(df.at[index, 'jp'], test_kanji + learned_kanji)]

    if filtered_indexes:
        random_index = random.choice(filtered_indexes)
        random_row = df.loc[random_index]
        return {
            "japanese": random_row['jp'],
            "english": random_row['en'],
        }
    else:
        raise Exception("No sentence found matching the criteria.")