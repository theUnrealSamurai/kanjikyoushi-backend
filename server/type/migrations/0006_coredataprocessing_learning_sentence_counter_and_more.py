# Generated by Django 5.0.7 on 2024-09-06 00:59

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('type', '0005_coredataprocessing_penalty_char_counts_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='coredataprocessing',
            name='learning_sentence_counter',
            field=models.IntegerField(default=0),
        ),
        migrations.AddField(
            model_name='coredataprocessing',
            name='test_sentence_counter',
            field=models.IntegerField(default=0),
        ),
    ]
