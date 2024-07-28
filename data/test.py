import pandas as pd

df = pd.read_csv("/Users/mits-mac-001/Code/server/data/kanji_lists/csv/Kanji-N1.csv", sep=",")
df['Kanji'] = df['Kanji'].str.split(' ')
kanji = df['Kanji'].tolist()
kanji = [item for sublist in kanji for item in sublist]
kanji = ''.join(kanji)
print(kanji, len(kanji))