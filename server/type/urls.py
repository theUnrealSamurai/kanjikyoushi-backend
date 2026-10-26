from django.urls import path
from . import views


urlpatterns = [
    path("onboard", views.onboard, name="onboard"),
    path("next", views.next_sentence, name="next_sentence"),
    path("submit", views.submit, name="submit"),
    path("mode", views.mode, name="mode"),
    path("due", views.due, name="due"),
    path("stats", views.stats, name="stats"),
]
