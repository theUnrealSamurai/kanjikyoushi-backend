from django.urls import path
from . import views


urlpatterns = [
    path("render_sentence", views.render_sentence, name="render_sentence"),
]