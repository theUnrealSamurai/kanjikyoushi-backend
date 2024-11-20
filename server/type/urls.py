from django.urls import path
from . import views


urlpatterns = [
    path("onboard", views.onboard, name="onboard"),
    path("render_practice", views.render_practice, name="render_practice"),
    path("update_practice", views.update_practice, name="update_practice"),
    path("render_revision", views.render_revision, name="render_revision"),
    path("update_revision", views.update_revision, name="udpate_test"),
]