import re
import random
import pandas as pd
from .utils import contains_kanji, contains_only_allowed_kanji, fetch_kanji_data


df = pd.read_csv('assets/sentence_db.tsv', sep='\t')

def fetch_learning_sentence(learning_kanji: str, learned_kanji: str):
    global df
    df = df[df['JLPT'] == "N5"]

    # Filter all the sentences containing the learning kanji
    indexes_with_kanji = df[df['jp'].apply(lambda x: contains_kanji(x, learning_kanji))].index.tolist()

    # Filter sentences containing only learned kanji + learning kanji
    filtered_indexes = [index for index in indexes_with_kanji if contains_only_allowed_kanji(df.at[index, 'jp'], learning_kanji + learned_kanji)]


    if filtered_indexes:
        random_index = random.choice(filtered_indexes)
        random_row = df.loc[random_index]

        fetched_sentence = random_row['jp']
        kanji_in_fetched_sentence = re.findall(r'[\u4e00-\u9faf]', fetched_sentence)
        kanji_data = fetch_kanji_data(kanji_in_fetched_sentence)

        return {
            "japanese": random_row['jp'],
            "english": random_row['en'],
            "romaji": "this is a sample romaji sentence",
            "kanji": kanji_data,
            "vocabulary": [
                {"日本語": ["Japanese", "Japanese Language"]},
                {"日本": ["Japan"]},
                {"語": ["Language"]},
            ],
        }
    else:
        raise Exception("No sentence found matching the criteria.")
    

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