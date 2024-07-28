from django.db import models
from django.contrib.auth.models import User
import os

class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE)
    picture = models.ImageField(upload_to='profile_pictures', blank=True)

    def __str__(self):
        return f'{self.user.username} Profile'

    def user_profile_picture_path(self, filename):
        ext = filename.split('.')[-1]
        new_filename = f'{self.user.username}.{ext}'
        return os.path.join('profile_pictures', new_filename)
    
    def save(self, *args, **kwargs):
        if self.picture:
            self.picture.upload_to = self.user_profile_picture_path
        super().save(*args, **kwargs)
