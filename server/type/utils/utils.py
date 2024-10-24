import re, os, subprocess
import json
import pickle
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from django.db import connection
import translate


kanji_data, max_row_threshold = {}, 300000
translator = translate.Translator('en', from_lang='ja')

with open("assets/kanji_data.json", 'r', encoding='utf-8') as file:
    kanji_data = json.load(file)

with open("assets/n1n5_kanji_row_end_index.json", 'r', encoding='utf-8') as file:
    kanji_end_index = json.load(file)


def contains_kanji(sentence, kanji_list):
    return any(kanji in sentence for kanji in kanji_list)


def contains_only_allowed_kanji(sentence: str, kanji_list: str) -> bool:
    kanji_list = [i for i in kanji_list]  # type: ignore
    allowed_kanji_set = set(kanji_list)
    kanji_in_sentence = re.findall(r'[\u4e00-\u9faf]', sentence)
    return all(kanji in allowed_kanji_set for kanji in kanji_in_sentence)


def fetch_kanji_data(kanji_list):
    kanji_list = list(dict.fromkeys(kanji_list))
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


def fetch_psql_row(index):
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT * FROM n1n5 WHERE index = {index}")
        row = cursor.fetchone()
    return row


def load_kanji_sparse_matrix(input_file):
    """
    Load a sparse matrix and kanji list from a pickle file.

    :param input_file: name of the pickle file to load the sparse matrix and kanji list from
    :return: tuple containing (sparse_matrix, kanji_list)
    """
    print(f"Loading sparse matrix and kanji list from {input_file}...")
    
    # Check if the file exists
    if not os.path.exists(input_file):
        raise FileNotFoundError(f"The file {input_file} does not exist.")
    
    with open(input_file, 'rb') as f:
        sparse_matrix, kanji_list = pickle.load(f)
    
    if not isinstance(sparse_matrix, csr_matrix):
        raise ValueError("The loaded object is not a CSR matrix.")
    
    size_in_bytes = (sparse_matrix.data.nbytes + 
                     sparse_matrix.indices.nbytes + 
                     sparse_matrix.indptr.nbytes)
    size_in_mb = size_in_bytes / (1024 * 1024)
    print(f"Size of loaded sparse matrix in RAM: {size_in_mb:.2f} MB")
    
    print(f"Shape of sparse matrix: {sparse_matrix.shape}")
    print(f"Number of non-zero elements: {sparse_matrix.nnz}")
    print(f"Number of unique kanji: {len(kanji_list)}")
    
    print("Sparse matrix and kanji list loaded successfully.")
    
    return sparse_matrix, kanji_list


def search_max_kanji_match(sparse_matrix: csr_matrix, unique_kanji: list, target_kanji: list, exclude_kanji: list, max_rows: int = None) -> int:
    """
    Find the row in the sparse matrix with the maximum number of matching target kanji,
    while ensuring the excluded kanji are not present, up to a maximum number of rows.

    :param sparse_matrix: CSR sparse matrix where rows are sentences and columns are unique kanji
    :param unique_kanji: List of unique kanji corresponding to the columns of the sparse matrix
    :param target_kanji: List of target kanji to search for
    :param exclude_kanji: List of kanji that should not be present in the selected sentence
    :param max_rows: Maximum number of rows to search (default is None, which searches all rows)
    :return: Index of the row with the maximum number of matching target kanji and no excluded kanji
    """
    # Limit the number of rows to search
    sparse_matrix = sparse_matrix[:max_rows]

    # Create masks for the target and exclude kanji
    target_indices = [unique_kanji.index(k) for k in target_kanji if k in unique_kanji]
    exclude_indices = [unique_kanji.index(k) for k in exclude_kanji if k in unique_kanji]

    if not target_indices:
        raise ValueError("None of the target kanji are in the unique kanji list")

    # Extract the columns corresponding to the target and exclude kanji
    target_cols = sparse_matrix[:, target_indices]
    exclude_cols = sparse_matrix[:, exclude_indices] if exclude_indices else None

    # Sum along rows to get the count of matching kanji for each sentence
    match_counts = target_cols.sum(axis=1).A1[:max_rows]  # .A1 converts to 1D numpy array

    # Create a mask for sentences that don't contain any excluded kanji
    if exclude_cols is not None:
        exclude_mask = (exclude_cols.sum(axis=1).A1[:max_rows] == 0)
    else:
        exclude_mask = np.ones(max_rows, dtype=bool)

    # Apply the exclude mask to match_counts
    valid_match_counts = np.where(exclude_mask, match_counts, -1)

    # Find the index of the maximum value among valid sentences
    max_match_index = np.argmax(valid_match_counts)

    # Check if a valid sentence was found
    if valid_match_counts[max_match_index] == -1:
        raise ValueError("No sentence found that matches the criteria")

    return max_match_index


def get_ichiran_data(sentence):
    # takes a japanese sentences. runs ichiran-cli -i {sentence} and returns the output as a string
    result = subprocess.run(['ichiran-cli', '-i', sentence], 
                            capture_output=True, 
                            text=True, 
                            check=True)
    return result.stdout


def translate(sentence):
    return translator.translate(sentence)


sparse_matrix, sparse_matrix_kanji = load_kanji_sparse_matrix("assets/sparse_matrix_n1n5")
