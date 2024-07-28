import re
from django.db import models
from django.contrib.auth.models import User
from .utils.fetch_sentence import fetch_learning_sentence, fetch_test_sentence



class UserKanjiData(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    user_kanji_level = models.IntegerField(default=5) 

    learned_kanji = models.CharField(max_length=50000, blank=True)
    learning_kanji = models.CharField(max_length=50, blank=True)

    penalty_kanji = models.CharField(max_length=25, blank=True)
    character_type_counts = models.JSONField(default=dict)

    test_threshold = models.BigIntegerField(default=5)


    """
        1. If the kanji is typed for the first time it is added to the learning kanji list. 
        2. If the kanji is typed for more than 5 times it is ready to be tested. 
        3. If the kanji is typed correctly in the test, it is added to the learned kanji list.
        4. If a kanji is typed for more than 250 times, it is added to the perfected kanji list.

        * perfected kanji will not contain duplicates of learned kanji
    """


    def __str__(self):
        return self.user.username
    

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
                                                self.learned_kanji, 
                                            )
            return test, sentence_dict
        
    
    def update_typed_learning(self, sentence):
        """
            1. Update the character_type_counts. 
        """
        kanji_characters = self.get_kanji(sentence)
        for kanji in kanji_characters:
            try:
                self.character_type_counts[kanji] += 1
            except KeyError:
                self.character_type_counts[kanji] = 1

        self.save()


    def update_typed_test(self, sentence, passed):
        """
            1. If user passes the test, add the kanji to the learned kanji list. and udpate character type counts
            2. If user fails the test, add penalty of 2 to all the kanji. in the sentence. unless if it is in perfected kanji. 
            3. If user skips the test, add penalty of 3. 
        """
        print("function yet to be implemented")

        pass

    def get_kanji(self, sentence):
        kanji_pattern = re.compile(r'[\u4e00-\u9faf]')
        kanji_characters = kanji_pattern.findall(sentence)
        return ''.join(kanji_characters)

# After each test, remember to check the user level. Upgrade if needed. 