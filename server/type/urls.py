from django.urls import path
from . import views


urlpatterns = [
    path("", views.type, name="type"),
    path("update_typed_learning/", views.update_typed_learning, name="update_typed_learning"),
    path("update_typed_test/", views.update_typed_test, name="update_typed_test"),
    path("onboard/", views.onboard, name="onboard"),
]