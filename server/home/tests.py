from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework.test import APIClient


class AuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_signup_returns_tokens(self):
        response = self.client.post("/home/signup", {"username": "taro", "email": "taro@example.com", "password": "a-long-password"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertIn("access", response.json())

    def test_login_with_username_or_email(self):
        User.objects.create_user("taro", "taro@example.com", "a-long-password")
        for name in ("taro", "taro@example.com"):
            response = self.client.post("/home/login", {"username": name, "password": "a-long-password"}, format="json")
            self.assertEqual(response.status_code, 200, name)

    def test_bad_login_gives_one_generic_error(self):
        User.objects.create_user("taro", "taro@example.com", "a-long-password")
        for name in ("taro", "nobody"):
            response = self.client.post("/home/login", {"username": name, "password": "wrong"}, format="json")
            self.assertEqual(response.status_code, 401)
