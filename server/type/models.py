import re
from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from fsrs import FSRS, Card, Rating 
from django.db.models import JSONField
from .utils.fetch_sentence import fetch_learning_sentence


f = FSRS()

class CoreDataProcessing(models.Model):
    
    user = models.OneToOneField(User, on_delete=models.CASCADE)

    known_kanji = models.TextField(max_length=10000, default="")
    learned_kanji = models.TextField(max_length=10000, default="")
    upcomming_kanji = models.TextField(max_length=10000, default="")
    char_type_counts = JSONField(default=dict)
    temp_char_type_counts = JSONField(default=dict)

    learning_count = models.IntegerField(default=3) # The revision for the kanji doesn't start until the user typed it for 3 times. 

    kanji_json = JSONField(default=dict)


    def __str__(self):
        return self.user.username


    def onboard(self, kanji_list):
        """Takes a list of kanji as a string and then stores in the known kanji field. Also sets up the other variables
        This function is called only once when the user first creates an account, 
        The spaced repetition will not be applied to these kanjis until the user makes a mistakes and picks to learn them. 
        """
        self.known_kanji = kanji_list
        with open("assets/kanjiJLPT.txt", 'r', encoding='utf-8') as file:
            self.upcomming_kanji = ''.join(line.strip() for line in file.readlines())

        # Making sure the user doesn't have to learn the kanjis that they know already
        for kanji in self.known_kanji:
            self.upcomming_kanji = self.upcomming_kanji.replace(kanji, "")

        self.save()
        return self.known_kanji
    

    def render_practice(self):
        "randomly render a sentence using the first 5 kanjis in the upcomming kanji list"
        kanjis_for_sentence = self.upcomming_kanji[:5]
        response_json = fetch_learning_sentence(kanjis_for_sentence, self.learned_kanji + self.known_kanji)
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
        kanji_to_remove = []
        for kanji, count in self.temp_char_type_counts.items():
            if count >= self.learning_count:
                self.learned_kanji += kanji
                self.upcomming_kanji = self.upcomming_kanji.replace(kanji, "")
                kanji_to_remove.append(kanji)
                # create a card for the kanji and then add it to the json 
                card = Card()
                card, _ = f.review_card(card, Rating.Good)
                self.kanji_json[kanji] = card.to_dict()

        # Remove processed kanji from temp_char_type_counts
        for kanji in kanji_to_remove:
            self.temp_char_type_counts.pop(kanji, None)

        self.save()


    def render_revision(self):
        now = timezone.now()
        due_cards = []
        for kanji, card_json in self.kanji_json.items():
            card = Card.from_dict(card_json)
            if card.due <= now:
                due_cards.append((kanji, card.due))
        
        # Sort by due date, oldest first
        due_cards.sort(key=lambda x: x[1])
        
        if not due_cards:
            return {"message": "No cards to revise."}
        
        kanjis = ''.join([kanji for kanji, _ in due_cards])
        response_json = fetch_learning_sentence(kanjis, self.learned_kanji + self.known_kanji)
        return response_json


    def update_revision(self, sentence):
        kanjis = re.findall(r'[\u4e00-\u9faf]', sentence)
        for kanji in kanjis:
            card = Card.from_dict(self.kanji_json[kanji])
            card, _ = f.review_card(card, Rating.Easy)
            self.kanji_json[kanji] = card.to_dict()

        self.save()