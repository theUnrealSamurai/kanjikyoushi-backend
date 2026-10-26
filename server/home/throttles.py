from rest_framework.throttling import SimpleRateThrottle


class LoginThrottle(SimpleRateThrottle):
    """Login attempts are counted per account name, so guessing one password is slow whatever IPs it comes from."""

    scope = "login"

    def get_cache_key(self, request, view):
        name = str(request.data.get("username", "")).strip().lower()
        return self.cache_format % {"scope": self.scope, "ident": name or self.get_ident(request)}
