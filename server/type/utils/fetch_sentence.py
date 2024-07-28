import re
import random
from .utils import load_dfs

dfs = load_dfs()


def contains_kanji(sentence, kanji_list):
    return any(kanji in sentence for kanji in kanji_list)

# Function to check if a sentence contains only Kanji characters from the list
def contains_only_allowed_kanji(sentence, kanji_list):
    allowed_kanji_str = ''.join(kanji_list)
    # Create a regex pattern that matches any character that is not in the allowed Kanji list
    pattern = f'[^{re.escape(allowed_kanji_str)}]'
    # Remove all non-Kanji characters from the sentence for comparison
    kanji_only_sentence = re.sub(pattern, '', sentence)
    # Check if the kanji_only_sentence contains only allowed Kanji
    return all(char in allowed_kanji_str for char in kanji_only_sentence)

# Filter out the indexes where sentences contain Kanji not in the list

def fetch_learning_sentence(level: int, learning_kanji: str, learned_kanji: str):
    # if the learning kanji is not empty, fetch a sentence which contains any of the learning kanji. 
    # make sure the sentence that is fetched does not contain more than 3 new kanji. 
    # if the length of the learning kanji is 25 then make sure the sentence does not have any new kanji at all. 
    global dfs 
    df = dfs[f"N{level}"]
    indexes_with_kanji = df[df['jp'].apply(lambda x: contains_kanji(x, [i for i in learning_kanji]))].index.tolist()

    filtered_indexes = [index for index in indexes_with_kanji if contains_only_allowed_kanji(df.at[index, 'jp'], [i for i in learning_kanji + learned_kanji])]

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
        print("No sentences match the criteria.")
    

def fetch_test_sentence(user_kanji_level: int, learned_kanji: list, test_kanji: list):
    # condition for the function: the sentence must contain any of the test kanji. But may or may not contain any learned kanji.  
    #  retuns either a sentence or none. If none then the the sentence is not found which satisfied the condition. 
    print(test_kanji)
    print(learned_kanji)
    return {
        "japanese": " ".join(test_kanji),
        "english": "I am studying Japanese.",
    }
    
    pass