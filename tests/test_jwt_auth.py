"""JWT authentication tests for Intaris."""

from __future__ import annotations

import importlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


def _make_keypair(tmp_path: Path) -> tuple[object, str]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_pem = (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("utf-8")
    )
    public_key_path = tmp_path / "cognis-public.pem"
    public_key_path.write_text(public_pem, encoding="utf-8")
    return private_key, str(public_key_path)


def _make_jwt(
    private_key: object, *, sub: str, aud: list[str], agent_id: str | None = None
) -> str:
    payload = {
        "sub": sub,
        "aud": aud,
        "iss": "cognis",
    }
    if agent_id is not None:
        payload["agent_id"] = agent_id
    return jwt.encode(
        payload, private_key, algorithm="ES256", headers={"kid": "test-key"}
    )


class _JWKSHandler(BaseHTTPRequestHandler):
    jwks_body = b"{}"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.jwks_body)))
        self.end_headers()
        self.wfile.write(self.jwks_body)

    def log_message(self, format, *args):  # noqa: A003
        return


class _JWKSFixture:
    def __init__(self, public_key: object) -> None:
        jwk = json.loads(ECAlgorithm.to_jwk(public_key))
        jwk["use"] = "sig"
        jwk["kid"] = "test-key"
        _JWKSHandler.jwks_body = json.dumps({"keys": [jwk]}).encode("utf-8")
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _JWKSHandler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/jwks.json"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return self.url

    def __exit__(self, exc_type, exc, tb) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _create_client(env: dict[str, str]) -> TestClient:
    with patch.dict(os.environ, env, clear=False):
        import intaris.auth as auth
        import intaris.server as srv

        auth._validator = None
        auth._validator_config = None
        srv._config = None
        srv._db = None
        srv._evaluator = None
        importlib.reload(srv)
        srv._config = srv.load_config()

        async def _whoami(request):
            return JSONResponse(
                {
                    "user_id": srv._session_user_id.get(),
                    "agent_id": srv._session_agent_id.get(),
                    "can_switch_user": not srv._session_user_bound.get(),
                }
            )

        app = Starlette(
            routes=[Route("/api/v1/whoami", _whoami)],
            middleware=[Middleware(srv.APIKeyMiddleware)],
        )
        return TestClient(app)


class TestJWTAuth:
    def test_public_key_jwt_sets_bound_identity(self, tmp_path):
        private_key, public_key_path = _make_keypair(tmp_path)
        token = _make_jwt(
            private_key,
            sub="alice@example.com",
            aud=["intaris"],
            agent_id="agent-1",
        )

        client = _create_client(
            {
                "LLM_API_KEY": "test-key",
                "DB_PATH": str(tmp_path / "intaris.db"),
                "INTARIS_JWT_PUBLIC_KEY": public_key_path,
                "INTARIS_API_KEY": "",
                "INTARIS_API_KEYS": "",
            }
        )
        with client:
            response = client.get(
                "/api/v1/whoami",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 200
        assert response.json() == {
            "user_id": "alice@example.com",
            "agent_id": "agent-1",
            "can_switch_user": False,
        }

    def test_claim_header_agent_mismatch_rejected(self, tmp_path):
        private_key, public_key_path = _make_keypair(tmp_path)
        token = _make_jwt(
            private_key,
            sub="alice@example.com",
            aud=["intaris"],
            agent_id="agent-claim",
        )

        client = _create_client(
            {
                "LLM_API_KEY": "test-key",
                "DB_PATH": str(tmp_path / "intaris.db"),
                "INTARIS_JWT_PUBLIC_KEY": public_key_path,
                "INTARIS_API_KEY": "",
                "INTARIS_API_KEYS": "",
            }
        )
        with client:
            response = client.get(
                "/api/v1/whoami",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Agent-Id": "agent-header",
                },
            )

        assert response.status_code == 401

    def test_wrong_audience_rejected(self, tmp_path):
        private_key, public_key_path = _make_keypair(tmp_path)
        token = _make_jwt(
            private_key,
            sub="alice@example.com",
            aud=["mnemory"],
        )

        client = _create_client(
            {
                "LLM_API_KEY": "test-key",
                "DB_PATH": str(tmp_path / "intaris.db"),
                "INTARIS_JWT_PUBLIC_KEY": public_key_path,
                "INTARIS_API_KEY": "",
                "INTARIS_API_KEYS": "",
            }
        )
        with client:
            response = client.get(
                "/api/v1/whoami",
                headers={"Authorization": f"Bearer {token}"},
            )

        assert response.status_code == 401

    def test_invalid_jwt_falls_back_to_api_key(self, tmp_path):
        _, public_key_path = _make_keypair(tmp_path)
        fallback_token = "not.a.jwt"

        client = _create_client(
            {
                "LLM_API_KEY": "test-key",
                "DB_PATH": str(tmp_path / "intaris.db"),
                "INTARIS_JWT_PUBLIC_KEY": public_key_path,
                "INTARIS_API_KEY": fallback_token,
                "INTARIS_API_KEYS": "",
            }
        )
        with client:
            response = client.get(
                "/api/v1/whoami",
                headers={
                    "Authorization": f"Bearer {fallback_token}",
                    "X-User-Id": "fallback-user",
                },
            )

        assert response.status_code == 200
        assert response.json()["user_id"] == "fallback-user"

    def test_jwks_url_validation(self, tmp_path):
        private_key, _ = _make_keypair(tmp_path)
        token = _make_jwt(
            private_key,
            sub="alice@example.com",
            aud=["intaris"],
            agent_id="agent-1",
        )

        with _JWKSFixture(private_key.public_key()) as jwks_url:
            client = _create_client(
                {
                    "LLM_API_KEY": "test-key",
                    "DB_PATH": str(tmp_path / "intaris.db"),
                    "INTARIS_JWKS_URL": jwks_url,
                    "INTARIS_JWT_PUBLIC_KEY": "",
                    "INTARIS_API_KEY": "",
                    "INTARIS_API_KEYS": "",
                }
            )
            with client:
                response = client.get(
                    "/api/v1/whoami",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 200
        assert response.json()["user_id"] == "alice@example.com"

    def test_websocket_auth_uses_shared_jwt_resolver(self, tmp_path):
        private_key, public_key_path = _make_keypair(tmp_path)
        token = _make_jwt(
            private_key,
            sub="ws-user@example.com",
            aud=["intaris"],
        )

        env = {
            "LLM_API_KEY": "test-key",
            "DB_PATH": str(tmp_path / "intaris.db"),
            "INTARIS_JWT_PUBLIC_KEY": public_key_path,
            "INTARIS_API_KEY": "",
            "INTARIS_API_KEYS": "",
        }
        with patch.dict(os.environ, env, clear=False):
            import intaris.auth as auth
            import intaris.server as srv
            from intaris.api.stream import _authenticate_token

            auth._validator = None
            auth._validator_config = None
            srv._config = None
            srv._db = None
            srv._evaluator = None

            result = _authenticate_token(MagicMock(), token)

        assert result == "ws-user@example.com"
