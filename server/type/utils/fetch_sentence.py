import re
import json
import random
import pandas as pd
from .utils import *


def load_and_merge_json_files(directory: str):
    merged_data = {}
    
    # List all JSON files in the directory
    json_files = [f"{directory}/indexed_sentences_N{i}.json" for i in ["1", "2", "3", "4", "5", "1+"]]
    
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
    kanjis = re.findall(r'[\u4e00-\u9faf]', jp)
    return {
        "japanese": jp,
        "english": "This is a sample english sentence.",
        "romaji": "This is a sample romaji sentence.",
        "kanji": fetch_kanji_data(kanjis),
        "vocabulary": [
            {"日本語": ["Japanese", "Japanese Language"]},
            {"日本": ["Japan"]},
            {"語": ["Language"]},
        ],
    }


import psycopg2

def fetch_revision_sentence(due_kanji, maxrows):
    minrows = get_minrows(due_kanji, maxrows)
    
    # Database connection parameters
    db_params = {
        "dbname": "test_db",
        "user": "postgres",
        "password": "postgres",
        "host": "localhost"
    }

    try:
        # Connect to the database
        conn = psycopg2.connect(**db_params)
        cur = conn.cursor()

        # Prepare the SQL query
        query = """
        WITH numbered_sentences AS (
            SELECT 
                text,
                kanji,
                ROW_NUMBER() OVER () AS row_num
            FROM sentences
        ),
        filtered_sentences AS (
            SELECT 
                text,
                kanji,
                CARDINALITY(ARRAY(
                    SELECT UNNEST(kanji) 
                    INTERSECT 
                    SELECT UNNEST(%s)
                )) AS due_kanji_count
            FROM numbered_sentences
            WHERE row_num BETWEEN %s AND %s
        )
        SELECT text, kanji, due_kanji_count
        FROM filtered_sentences
        WHERE due_kanji_count > 0
        ORDER BY due_kanji_count DESC
        LIMIT 1;
        """

        # Convert due_kanji to a list if it's a string
        if isinstance(due_kanji, str):
            due_kanji = list(due_kanji)

        # Execute the query
        cur.execute(query, (due_kanji, minrows, maxrows))
        
        # Fetch the result
        result = cur.fetchone()

        if result:
            sentence, kanji, due_count = result
            return {
                "sentence": sentence,
                "kanji": kanji,
                "due_kanji_count": due_count
            }
        else:
            return None

    except (Exception, psycopg2.Error) as error:
        print("Error while connecting to PostgreSQL", error)
        return None

    finally:
        # Close the database connection
        if conn:
            cur.close()
            conn.close()



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