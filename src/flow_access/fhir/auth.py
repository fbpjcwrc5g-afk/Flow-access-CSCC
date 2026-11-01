"""SMART Backend Services (client_credentials + signed JWT assertion) as required by Epic."""

import json
import threading
import time
import uuid
from dataclasses import dataclass

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from jwt.algorithms import RSAAlgorithm

# Epic accepts RS384 or ES384 for backend-services client assertions.
ALGORITHM = "RS384"
# Epic rejects assertions whose exp is more than 5 minutes in the future.
ASSERTION_LIFETIME_SECONDS = 300
TOKEN_REFRESH_MARGIN_SECONDS = 60


class AuthError(RuntimeError):
    pass


@dataclass
class _CachedToken:
    value: str
    expires_at: float


class BackendServicesAuth:
    def __init__(
        self,
        client_id: str,
        token_url: str,
        private_key_pem: bytes,
        key_id: str,
        scopes: str,
        http: httpx.Client | None = None,
    ):
        self.client_id = client_id
        self.token_url = token_url
        self.key_id = key_id
        self.scopes = scopes
        self._private_key = serialization.load_pem_private_key(private_key_pem, password=None)
        self._http = http or httpx.Client(timeout=30)
        self._token: _CachedToken | None = None
        self._lock = threading.Lock()

    def client_assertion(self) -> str:
        now = int(time.time())
        claims = {
            "iss": self.client_id,
            "sub": self.client_id,
            "aud": self.token_url,
            "jti": str(uuid.uuid4()),
            "iat": now,
            "nbf": now,
            "exp": now + ASSERTION_LIFETIME_SECONDS,
        }
        return jwt.encode(claims, self._private_key, algorithm=ALGORITHM, headers={"kid": self.key_id})

    def access_token(self) -> str:
        with self._lock:
            if self._token and self._token.expires_at - TOKEN_REFRESH_MARGIN_SECONDS > time.time():
                return self._token.value
            resp = self._http.post(
                self.token_url,
                data={
                    "grant_type": "client_credentials",
                    "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                    "client_assertion": self.client_assertion(),
                    "scope": self.scopes,
                },
                headers={"Accept": "application/json"},
            )
            if resp.status_code != 200:
                raise AuthError(f"Token request failed with HTTP {resp.status_code}: {resp.text[:500]}")
            body = resp.json()
            self._token = _CachedToken(body["access_token"], time.time() + int(body.get("expires_in", 300)))
            return self._token.value

    def invalidate(self) -> None:
        with self._lock:
            self._token = None

    def jwks(self) -> dict:
        """Public key set to register with Epic (as a JWKS URL or uploaded key)."""
        jwk = json.loads(RSAAlgorithm.to_jwk(self._private_key.public_key()))
        jwk.update({"kid": self.key_id, "alg": ALGORITHM, "use": "sig"})
        return {"keys": [jwk]}
