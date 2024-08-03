from django.db import models
from django.contrib.auth.models import User
from .utils.fetch_sentence import fetch_learning_sentence, fetch_test_sentence


class CoreDataProcessing(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    user_kanji_level = models.IntegerField(default=5) 

    learned_kanji = models.CharField(max_length=50000, blank=True)
    learning_kanji = models.CharField(max_length=50, blank=True)

    penalty_kanji = models.CharField(max_length=25, blank=True)
    character_type_counts = models.JSONField(default=dict)

    test_threshold = models.SmallIntegerField(default=5)

    
    def __str__(self):
        return self.user.username
    

    def process_onboarding(self, kanji_list):
        """
            takes a list of kanjis from the front end and updates it in the learned kanji. 
            plus processess for unlearned kanji and updates the learning kanji.
        """
        self.learned_kanji = kanji_list
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
                                                kanjis_ready_to_test)
            return test, sentence_dict
        else:
            sentence_dict = fetch_learning_sentence(self.user_kanji_level, 
                                                    self.learning_kanji,    
                                                    self.learned_kanji)
            return test, sentence_dict

