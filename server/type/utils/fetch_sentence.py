import re
from .utils import *


def fetch_practice_sentence(kanji: str, unknownkanji: str, maxrow: int):
    index = search_max_kanji_match(sparse_matrix, sparse_matrix_kanji, kanji, unknownkanji, maxrow)
    row = fetch_psql_row(index)
    ichiran_data = get_ichiran_data(row[2])

    return {
        "japanese": row[2],
        "english": translate(row[2]),
        "romaji": ichiran_data.split("\n\n*")[0].strip(),
        "kanji_data": fetch_kanji_data(re.findall(r'[\u4e00-\u9faf]', row[2])),
        "definitions": ichiran_data.split("\n\n*")[1:],
    }



def fetch_revision_sentence(due_kanji, unknown_kanji, maxrow):
    index = search_max_kanji_match(sparse_matrix, sparse_matrix_kanji, due_kanji, unknown_kanji, maxrow)
    row = fetch_psql_row(index)
    ichiran_data = get_ichiran_data(row[2])
    
    if not contains_kanji(row[2], due_kanji):
        return None

    return {
        "japanese": row[2],
        "english": translate(row[2]), 
        "romaji": ichiran_data.split("\n\n*")[0].strip(),
        "kanji_data": fetch_kanji_data(re.findall(r'[\u4e00-\u9faf]', row[2])),
        "definitions": ichiran_data.split("\n\n*")[1:],
        "due_kanji": due_kanji,
    }