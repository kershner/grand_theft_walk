"""Renewable Twitch user tokens for Grand Theft Walk."""

import time

import requests

from core.files import atomic_json, read_json

REQUIRED_SCOPES = {"chat:read", "chat:edit"}


class OAuthTokens:
    def __init__(self, params, path):
        self.params, self.path = params, path
        self.tokens = None
        self.validated_at = 0

    def ensure_fresh(self):
        try:
            tokens = self.tokens or read_json(self.path)
        except FileNotFoundError:
            raise RuntimeError(
                "Twitch authorization is missing. Run venv\\Scripts\\python.exe "
                "-m chat.oauth (using the same --parameters file, if specified), "
                "sign in as TWITCH_BOT_LOGIN, then retry production startup."
            ) from None
        now = time.time()
        if now >= float(tokens.get("expires_at", 0)) - 60:
            if not tokens.get("refresh_token"):
                raise RuntimeError("Run venv\\Scripts\\python.exe -m chat.oauth to authorize Twitch first.")
            response = requests.post("https://id.twitch.tv/oauth2/token", data={
                "client_id": self.params["CLIENT_ID"],
                "client_secret": self.params["CLIENT_SECRET"],
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            }, timeout=20)
            if response.status_code != 200:
                raise RuntimeError(f"Twitch token refresh failed (HTTP {response.status_code}); reauthorize if revoked.")
            payload = response.json()
            if not payload.get("access_token") or int(payload.get("expires_in", 0)) <= 0:
                raise RuntimeError("Twitch returned an incomplete token response.")
            tokens = dict(payload, refresh_token=payload.get("refresh_token") or tokens["refresh_token"],
                          expires_at=int(now) + int(payload["expires_in"]))
            # Save rotated refresh tokens immediately, even if later validation fails.
            atomic_json(self.path, tokens)
            self.validated_at = 0
        if not self.validated_at or now - self.validated_at >= 3600:
            response = requests.get("https://id.twitch.tv/oauth2/validate", headers={
                "Authorization": f"OAuth {tokens['access_token']}"}, timeout=20)
            if response.status_code != 200:
                raise RuntimeError(f"Twitch token validation failed (HTTP {response.status_code}); reauthorize Twitch.")
            account = response.json()
            if (account.get("client_id") != self.params["CLIENT_ID"]
                    or account.get("login", "").lower() != self.params["TWITCH_BOT_LOGIN"].lower()
                    or not REQUIRED_SCOPES.issubset(account.get("scopes", []))):
                raise RuntimeError("Twitch token has the wrong app, login, or chat permissions; reauthorize.")
            self.validated_at = now
        self.tokens = tokens
        return tokens
