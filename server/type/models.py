import re, random, json
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from fsrs import FSRS, Card, Rating 
from rest_framework.response import Response
from django.db.models import JSONField
from .utils.fetch_sentence import fetch_practice_sentence, fetch_revision_sentence


f = FSRS()
with open("assets/n1n5_kanji_row_end_index.json", 'r', encoding='utf-8') as file:
    kanji_end_index = json.load(file)


class CoreDataProcessing(models.Model):
    
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    known_kanji = models.TextField(max_length=10000, default="")
    learned_kanji = models.TextField(max_length=10000, default="")
    upcomming_kanji = models.TextField(max_length=10000, default="")
    char_type_counts = JSONField(default=dict)
    temp_char_type_counts = JSONField(default=dict)

    learned_kanji_counter = models.IntegerField(default=0)

    learning_count = models.IntegerField(default=3) # The revision for the kanji doesn't start until the user typed it for 3 times. 
    max_rows = models.IntegerField(default=0)

    kanji_json = JSONField(default=dict)


    def __str__(self):
        return self.user.username


    def onboard(self, kanji_list):
        """Takes a list of kanji as a string and then stores in the known kanji field. Also sets up the other variables
        This function is called only once when the user first creates an account, 
        The spaced repetition will not be applied to these kanjis until the user makes a mistakes and picks to learn them. 
        """
        if not kanji_list:
            kanji_list = "一二三四五六七八九十百千万"
        else:
            for kanji in "一二三四五六七八九十百千万":
                if kanji not in kanji_list:
                    kanji_list += kanji

        self.known_kanji = kanji_list
        with open("assets/jlpt_kanji_lists.json", 'r', encoding='utf-8') as file:
            kanji_json = json.load(file)
            kanjis = kanji_json["N5"] + kanji_json["N4"] + kanji_json["N3"] + kanji_json["N2"] + kanji_json["N1"]
            self.upcomming_kanji = ''.join(kanjis)

        # Making sure the user doesn't have to learn the kanjis that they know already
        for kanji in self.known_kanji:
            self.upcomming_kanji = self.upcomming_kanji.replace(kanji, "")

        # reset to defaults
        self.char_type_counts = {}
        self.temp_char_type_counts = {}
        self.learned_kanji = ""
        self.kanji_json = {}
        self.max_rows = 0

        self.save()
        return self.known_kanji
    

    def render_practice(self):
        "randomly render a sentence using the first 5 kanjis in the upcomming kanji list"

        if not self.upcomming_kanji:
            return {"redirect": "true", "url": "/congratulations/"}
        
        # Logic to make sure the user always completes typing the first kanji 3 times before other kanjis 3 times. 
        kanji_for_sentence = ""

        while True:
            kanji_for_sentence = random.choice(self.upcomming_kanji[:5])
            if kanji_for_sentence == self.upcomming_kanji[0]:
                break
            if self.temp_char_type_counts.get(self.upcomming_kanji[0], 0) == self.learning_count-1 and self.temp_char_type_counts.get(kanji_for_sentence, 0) == self.learning_count-1:
                continue
            else:
                break
        response_json = fetch_practice_sentence(kanji_for_sentence)
        response_json["learned_kanji"] = self.learned_kanji_counter
        return response_json
    

    def update_practice(self, sentence):
        """Take the sentence that user typed, update the main character count for the character, 
        update the temprorary character count for the character, in the end if the temp character 
        count is greater than self.learning_count, then move the character to the learned kanji list. 
        Only the characters that are in the upcomming kanji list. """

        kanjis_in_sentence = re.findall(r'[\u4e00-\u9faf]', sentence)

        # Update character counts:
        for kanji in kanjis_in_sentence:
            self.char_type_counts[kanji] = self.char_type_counts.get(kanji, 0) + 1
            if kanji in self.upcomming_kanji:
                self.temp_char_type_counts[kanji] = self.temp_char_type_counts.get(kanji, 0) + 1

        # Move the kanji to learned kanji and erase data from the temp count for a fresh restart if the user forgets the kanji. 
        kanji_to_remove, set_max_rows = [], False
        for kanji, count in self.temp_char_type_counts.items():
            if count >= self.learning_count:
                self.learned_kanji_counter += 1
                self.learned_kanji += kanji
                self.upcomming_kanji = self.upcomming_kanji.replace(kanji, "")
                kanji_to_remove.append(kanji)
                # create a card for the kanji and then add it to the json 
                card = Card()
                card, _ = f.review_card(card, Rating.Good)
                self.kanji_json[kanji] = card.to_dict()
                set_max_rows = True

        # Remove processed kanji from temp_char_type_counts
        for kanji in kanji_to_remove:
            self.temp_char_type_counts.pop(kanji, None)

        # If len of self.upcomming_kanji is 0, then redirect to a different page saying Congratulations.
        if not self.upcomming_kanji:
            return {"redirect": "true", "url": "/congratulations/"}

        if set_max_rows:
            self.max_rows = kanji_end_index[self.learned_kanji[-1]]

        self.save()


    def render_revision(self):
        now = timezone.now()
        due_cards = []
        for kanji, card_json in self.kanji_json.items():
            card = Card.from_dict(card_json)
            if card.due <= now:
                due_cards.append((kanji, card.due))
        
        if not due_cards:
            return None
        
        due_kanjis = [kanji for kanji, _ in due_cards]
        response_json = fetch_revision_sentence(due_kanjis, self.upcomming_kanji, self.max_rows)
        if not response_json:
            return None
        return response_json


    def update_revision(self, kanji_rating_dict):
        user_rating = {
            "easy": Rating.Easy,
            "good": Rating.Good,
            "hard": Rating.Hard,
            "again": Rating.Again,
        }
        for kanji, rating in kanji_rating_dict.items():
            self.char_type_counts[kanji] = self.char_type_counts.get(kanji, 0) + 1
            if kanji in self.known_kanji and rating == "easy":
                continue
            if kanji in self.kanji_json:
                card = Card.from_dict(self.kanji_json[kanji])
            else: 
                card = Card()
                self.learned_kanji += kanji
                self.known_kanji = self.known_kanji.replace(kanji, "")
            card, _ = f.review_card(card, user_rating[rating])
            self.kanji_json[kanji] = card.to_dict()
        self.save()