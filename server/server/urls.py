from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

urlpatterns = [
    # Admin URL
    path('admin/', admin.site.urls),

    # JWT Authentication URLs
    path('api/token/', TokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('api/token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),

    # Home URL
    path("home/", RedirectView.as_view(url='/')),
    path("home/", include("home.urls"), name="home"),

    # Type URL
    path("type/", include("type.urls"), name="type"),
]
