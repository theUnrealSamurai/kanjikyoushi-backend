from io import StringIO

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from .models import InviteCode


class AuthTests(TestCase):
    def setUp(self):
        cache.clear()  # throttle counters
        self.client = APIClient()

    def signup(self, username, code):
        body = {"username": username, "email": f"{username}@example.com", "password": "a-long-password", "invite_code": code}
        return self.client.post("/home/signup", body, format="json")

    def test_signup_returns_tokens_and_uses_up_the_code(self):
        invite = InviteCode.objects.create()
        response = self.signup("taro", invite.code.lower())  # codes aren't case-sensitive
        self.assertEqual(response.status_code, 201)
        self.assertIn("access", response.json())
        invite.refresh_from_db()
        self.assertEqual(invite.used_by.username, "taro")

        response = self.signup("jiro", invite.code)
        self.assertEqual(response.status_code, 400)
        self.assertIn("invite_code", response.json())
        self.assertFalse(User.objects.filter(username="jiro").exists())

    def test_signup_needs_a_real_code(self):
        for code in ("", "NOTACODE42"):
            self.assertEqual(self.signup("taro", code).status_code, 400)
        self.assertFalse(User.objects.exists())

    def test_login_is_throttled_per_account(self):
        User.objects.create_user("taro", "taro@example.com", "a-long-password")
        codes = [self.client.post("/home/login", {"username": "taro", "password": "wrong"}, format="json").status_code
                 for _ in range(11)]
        self.assertEqual(codes[:10], [401] * 10)
        self.assertEqual(codes[10], 429)
        other = self.client.post("/home/login", {"username": "hanako", "password": "wrong"}, format="json")
        self.assertEqual(other.status_code, 401)  # other accounts are counted separately

    def test_invite_command_makes_and_lists_codes(self):
        out = StringIO()
        call_command("invite", "--count", "2", "--note", "for friends", stdout=out)
        made = out.getvalue().split()
        self.assertEqual(len(made), 2)
        out = StringIO()
        call_command("invite", "--list", stdout=out)
        self.assertTrue(all(code in out.getvalue() for code in made))

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
