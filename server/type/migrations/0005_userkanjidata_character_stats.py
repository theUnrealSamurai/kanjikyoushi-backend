# Generated by Django 4.2.11 on 2024-04-03 04:42

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('type', '0004_delete_userprofile'),
    ]

    operations = [
        migrations.AddField(
            model_name='userkanjidata',
            name='character_stats',
            field=models.JSONField(default=dict),
        ),
    ]
