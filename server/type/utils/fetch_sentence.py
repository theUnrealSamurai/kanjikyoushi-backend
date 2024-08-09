import random
import pandas as pd
from .utils import contains_kanji, contains_only_allowed_kanji


df = pd.read_csv('assets/sentence_db.tsv', sep='\t')



def fetch_learning_sentence(user_kanji_level: int, learning_kanji: str, learned_kanji: str):
    global df
    df = df[df['JLPT'] == "N" + str(user_kanji_level)] # Filter DataFrame by JLPT level

    # Filter all the sentences containing the learning kanji
    indexes_with_kanji = df[df['jp'].apply(lambda x: contains_kanji(x, learning_kanji))].index.tolist()

    # Filter sentences containing only learned kanji + learning kanji
    filtered_indexes = [index for index in indexes_with_kanji if contains_only_allowed_kanji(df.at[index, 'jp'], learning_kanji + learned_kanji)]


    if filtered_indexes:
        random_index = random.choice(filtered_indexes)
        random_row = df.loc[random_index]
        return {
            "japanese": random_row['jp'],
            "english": random_row['en'],
            "romaji": "this is a sample romaji sentence",
            "kanji": [
                {"kanji": "日", "meaning": "にち", "kunyomi": "ひ", "onyomi": "に"},
                {"kanji": "本", "meaning": "ほん", "kunyomi": "もと", "onyomi": "ほん"},
                {"kanji": "語", "meaning": "ご", "kunyomi": "かたる", "onyomi": "ご"},
            ],
            "vocabulary": [
                {"日本語": ["Japanese", "Japanese Language"]},
                {"日本": ["Japan"]},
                {"語": ["Language"]},
            ],
        }
    else:
        raise Exception("No sentence found matching the criteria.")
    

def fetch_test_sentence(user_kanji_level: int, learned_kanji: str, test_kanji: str):
    global df
    df = df[df['JLPT'] == "N" + str(user_kanji_level)] # Filter DataFrame by JLPT level

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