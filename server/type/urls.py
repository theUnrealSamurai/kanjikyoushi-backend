from django.urls import path
from . import views


urlpatterns = [
    path("onboard", views.onboard, name="onboard"),
    path("next", views.next_sentence, name="next_sentence"),
    path("submit", views.submit, name="submit"),
]
