import requests
import json
import pandas as pd
from tqdm import tqdm
import os
from glob import glob


tqdm.pandas()

def translate(japanese_sentence):
    url = "http://127.0.0.1:5000/translate"
    
    payload = {
        "q": japanese_sentence,
        "source": "ja",
        "target": "en",
        "format": "text",
        "api_key": ""
    }
    
    headers = {
        "Content-Type": "application/json"
    }
    
    try:
        response = requests.post(url, data=json.dumps(payload), headers=headers)
        response.raise_for_status()
        
        data = response.json()
        return data.get("translatedText", "Translation not available")
    except requests.exceptions.RequestException as e:
        print(f"An error occurred: {e}")
        return None

def process_chunk(chunk_path):
    chunk = pd.read_csv(chunk_path, sep="\t")
    chunk['translation'] = chunk['text'].progress_apply(translate)
    chunk.to_csv(f"temp/processed/{os.path.basename(chunk_path)}", sep="\t", index=False)
    os.remove(chunk_path)

def split_and_save_df(df_path, output_dir, chunk_size=1000, file_prefix='chunk', file_format='tsv', sep='\t'):
    """
    Split a DataFrame into chunks and save each chunk as a separate file.

    Parameters:
    - df_path: path to df
    - output_dir: directory to save the chunk files
    - chunk_size: number of rows in each chunk (default 1000)
    - file_prefix: prefix for the output files (default 'chunk')
    - file_format: format to save the files in ('csv' or 'tsv', default 'csv')
    - sep: separator to use when saving files (default '\t')

    Returns:
    - List of file paths for the saved chunks
    """
    df = pd.read_csv(df_path, sep="\t")

    # Ensure the output directory exists
    os.makedirs(output_dir, exist_ok=True)

    # Calculate the number of chunks
    num_chunks = (len(df) - 1) // chunk_size + 1

    # Initialize list to store file paths
    chunk_files = []

    # Split and save chunks
    for i in tqdm(range(num_chunks), desc="Saving chunks"):
        start = i * chunk_size
        end = min((i + 1) * chunk_size, len(df))
        chunk = df.iloc[start:end]

        # Determine file extension
        ext = 'tsv' if file_format.lower() == 'tsv' else 'csv'
        
        # Generate file name
        file_name = f"{file_prefix}_{i}.{ext}"
        file_path = os.path.join(output_dir, file_name)

        # Save chunk
        if file_format.lower() == 'tsv':
            chunk.to_csv(file_path, sep=sep, index=False)
        else:
            chunk.to_csv(file_path, index=False)

        chunk_files.append(file_path)

    print(f"Split {len(df)} rows into {num_chunks} chunks of {chunk_size} rows each.")
    print(f"Chunks saved in {output_dir}")

    return chunk_files

def merge_dfs():
    files = glob("temp/processed/*.tsv")
    df = pd.concat([pd.read_csv(file, sep="\t") for file in tqdm(files)], ignore_index=True)
    df = df.sort_values(by="index")
    df.to_csv("/home/samurai/Downloads/kanjikyoushi_refreshed_data/df_n1n5_translated.tsv", sep="\t", index=False)




if __name__ == "__main__":
    output_file = "/home/samurai/Downloads/kanjikyoushi_refreshed_data/df_n1n5_translated.tsv"
    input_file = "/home/samurai/Downloads/kanjikyoushi_refreshed_data/df_n1n5.tsv"


    chunk_paths = glob("temp/chunks/chunk_*.tsv")

    for chunk_path in tqdm(chunk_paths):
        process_chunk(chunk_path)