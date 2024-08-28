import re
from django.db import models
from django.contrib.auth.models import User
from .utils.fetch_sentence import fetch_learning_sentence, fetch_test_sentence


class CoreDataProcessing(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    user_kanji_level = models.IntegerField(default=5) 

    learned_kanji = models.CharField(max_length=50000, blank=True)
    learning_kanji = models.CharField(max_length=50, blank=True)

    penalty_kanji = models.CharField(max_length=25, blank=True)
    penalty_char_counts = models.JSONField(default=dict)
    character_type_counts = models.JSONField(default=dict)

    test_threshold = models.SmallIntegerField(default=5)
    penalty_threshold = models.SmallIntegerField(default=5)

    all_kanjis = models.CharField(max_length=50000, blank=True)

    def __init__(self, *args, **kwargs):
        super(CoreDataProcessing, self).__init__(*args, **kwargs)
        # Initialize all_kanjis only if it's empty
        if not self.all_kanjis:
            try:
                with open('assets/kanjiJLPT.txt', 'r', encoding='utf-8') as file:
                    self.all_kanjis = ''.join(line.strip() for line in file.readlines())
            except FileNotFoundError:
                # Handle the error if the file is not found
                self.all_kanjis = ''
                print("Error: kanjiJLPT.txt file not found.")    

    def __str__(self):
        return self.user.username
    

    def process_onboarding(self, kanji_list):
        """
        Takes a list of kanjis from the front end and updates it in the learned kanji. 
        Plus processes for unlearned kanji and updates the learning kanji.
        """
        try:
            with open('assets/kanjiJLPT.txt', 'r', encoding='utf-8') as file:
                self.all_kanjis = ''.join(line.strip() for line in file.readlines())
        except FileNotFoundError:
            # Handle the error if the file is not found
            self.all_kanjis = ''
            print("Error: kanjiJLPT.txt file not found.")    

        # self.__init__(force_onboard=True)

        self.learned_kanji = ''.join(kanji_list)
        for kanji in kanji_list:
            self.all_kanjis = self.all_kanjis.replace(kanji, "")

        self.learning_kanji = self.all_kanjis[:10]
        self.all_kanjis = self.all_kanjis[10:]
        self.save()



    def fetch_sentence(self):
        """
            1. Check if the user can be tested on anything. 
                a. if yes, proceed with the test. 
            2. If the user can't be tested, fetch a learning sentence and return. 
        """

        kanjis_ready_to_test = []

        for kanji in self.learning_kanji:
            try:
                if self.character_type_counts[kanji] > self.test_threshold and kanji not in self.penalty_kanji:
                    kanjis_ready_to_test.append(kanji)
            except KeyError:
                self.character_type_counts[kanji] = 0

        test = len(kanjis_ready_to_test) > 0

        if test:
            sentence_dict = fetch_test_sentence(self.user_kanji_level, 
                                                self.learned_kanji, 
                                                ''.join(kanjis_ready_to_test))
            return test, sentence_dict
        else:
            sentence_dict = fetch_learning_sentence(self.user_kanji_level, 
                                                    self.learning_kanji,    
                                                    self.learned_kanji)
            return test, sentence_dict

    def update_character_type_count(self, sentence: str) -> None:
        kanjis_in_sentence = re.findall(r'[\u4e00-\u9faf]', sentence)
        for kanji in kanjis_in_sentence:
            self.character_type_counts[kanji] = self.character_type_counts.get(kanji, 0) + 1
            if kanji in self.penalty_kanji:
                self.penalty_char_counts[kanji] = self.penalty_char_counts.get(kanji, 0) + 1

        for kanji in list(self.penalty_char_counts.keys()):
            if self.penalty_char_counts[kanji] > self.penalty_threshold:
                self.penalty_kanji = self.penalty_kanji.replace(kanji, '')
                del self.penalty_char_counts[kanji]

        self.save()


    def test_passed(self, sentence):
        self.update_character_type_count(sentence)
        kanjis = re.findall(r'[\u4e00-\u9faf]', sentence)

        for kanji in kanjis:
            if kanji in self.penalty_kanji:
                self.penalty_kanji = self.penalty_kanji.replace(kanji, '')
            if kanji in self.penalty_char_counts:
                del self.penalty_char_counts[kanji]


        for kanji in list(self.learning_kanji):
            if kanji in kanjis:
                self.learned_kanji += kanji
                self.learning_kanji = self.learning_kanji.replace(kanji, '')
                self.all_kanjis = self.all_kanjis.replace(kanji, '')

        while len(self.learning_kanji) < 10 and self.all_kanjis:
            new_kanji = self.all_kanjis[0]
            self.learning_kanji += new_kanji
            self.all_kanjis = self.all_kanjis[1:]

        self.save()

    def skip_test(self, skipped_kanji):
        for kanji in skipped_kanji:
            if kanji not in self.penalty_kanji:
                self.penalty_kanji += kanji
            
            self.penalty_char_counts[kanji] = 0
            
            if kanji in self.learned_kanji:
                self.learned_kanji = self.learned_kanji.replace(kanji, '')
                self.learning_kanji += kanji
                
                if len(self.learning_kanji) > 23:
                    removed_kanji = self.learning_kanji[0]
                    self.learning_kanji = self.learning_kanji[1:]
                    
                    # Add the removed kanji back to all_kanjis if it's not in learned_kanji
                    if removed_kanji not in self.learned_kanji:
                        self.all_kanjis = removed_kanji + self.all_kanjis

        self.save()
