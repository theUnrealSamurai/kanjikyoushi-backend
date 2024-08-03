import pandas as pd
import re 

kanji = ''.join(line.strip() for line in open('assets/kanjiJLPT.txt', 'r', encoding='utf-8').readlines())


def load_dfs():
    # returns 5 dataframes. each dataframe contains sentences belonging to a certain level.
    return {
        "N5": pd.read_csv('assets/N5.csv'),
        "N4": pd.read_csv('assets/N4.csv'),
        "N3": pd.read_csv('assets/N3.csv'),
        "N2": pd.read_csv('assets/N2.csv'),
        "N1": pd.read_csv('assets/N1.csv'),
    }


def contains_kanji(sentence, kanji_list):
    return any(kanji in sentence for kanji in kanji_list)


# Function to check if a sentence contains only Kanji characters from the list
def contains_only_allowed_kanji(sentence, kanji_list):
    allowed_kanji_str = ''.join(kanji_list)
    pattern = f'[^{re.escape(allowed_kanji_str)}]'
    kanji_only_sentence = re.sub(pattern, '', sentence)
    return all(char in allowed_kanji_str for char in kanji_only_sentence)