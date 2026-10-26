"""A TestClient that submits console forms the way a browser does: it
loads a form page first and sends back the CSRF token from its hidden
field (app/console/csrf.py). Tests of the CSRF check itself use a plain
TestClient, which sends no token."""

import re

import httpx2
from fastapi.testclient import TestClient

_TOKEN_FIELD = re.compile(r'name="csrf_token" value="([^"]+)"')


class ConsoleClient(TestClient):
    def csrf_token(self, headers: dict[str, str] | None = None) -> str:
        page = self.get("/console/login", headers=headers)
        match = _TOKEN_FIELD.search(page.text)
        assert match, "the login page has no CSRF token field"
        return match.group(1)

    def post(  # pyright: ignore[reportIncompatibleMethodOverride] -- tests only send forms
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx2.Response:
        if url.startswith("/console/"):
            data = {"csrf_token": self.csrf_token(headers), **(data or {})}
        return super().post(url, data=data, headers=headers)
