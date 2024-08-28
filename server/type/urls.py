from django.urls import path
from . import views


urlpatterns = [
    path("onboard", views.onboard, name="onboard"),
    path("render_sentence", views.render_sentence, name="render_sentence"),
    path("update_learning_sentence", views.update_learning_sentence, name="update_learning_sentence"),
    path("test_passed", views.test_passed, name="test_passed"),
    path("skip_test", views.skip_test, name="skip_test" )
]