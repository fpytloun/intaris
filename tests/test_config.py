"""Tests for configuration loading and validation."""

from __future__ import annotations

import os

import pytest

from intaris.config import Config, DBConfig, LLMConfig, ServerConfig, _parse_api_keys


class TestConfigDefaults:
    """Test default configuration values."""

    def test_llm_defaults(self):
        config = LLMConfig()
        assert config.model == "gpt-5.4-nano"
        assert config.temperature == 0.1
        assert config.timeout_ms == 4000

    def test_db_defaults(self):
        config = DBConfig()
        assert config.path.endswith("intaris.db")
        assert ".intaris" in config.path
        assert config.pool_min_conn == 1
        assert config.pool_max_conn == 20

    def test_server_defaults(self):
        config = ServerConfig()
        assert config.host == "0.0.0.0"
        assert config.port == 8060
        assert config.rate_limit == 60


class TestConfigValidation:
    """Test configuration validation."""

    def test_missing_api_key(self):
        # Clear any env vars that might provide a key
        env_backup = {}
        for key in ("LLM_API_KEY", "OPENAI_API_KEY"):
            if key in os.environ:
                env_backup[key] = os.environ.pop(key)

        try:
            # Create fresh config without API key
            fresh = Config(llm=LLMConfig())
            if not fresh.llm.api_key:
                with pytest.raises(ValueError, match="API key is required"):
                    fresh.validate()
        finally:
            os.environ.update(env_backup)

    def test_timeout_too_low(self):
        config = Config(llm=LLMConfig())
        config.llm.timeout_ms = 100
        # Only test if API key is available
        if config.llm.api_key:
            with pytest.raises(ValueError, match="too low"):
                config.validate()

    def test_negative_rate_limit(self):
        config = Config(llm=LLMConfig())
        config.server.rate_limit = -1
        if config.llm.api_key:
            with pytest.raises(ValueError, match="must be >= 0"):
                config.validate()

    def test_malformed_api_keys_fails_validation(self, monkeypatch):
        monkeypatch.setenv("INTARIS_API_KEYS", "not-json")
        config = Config(llm=LLMConfig())
        if config.llm.api_key:
            with pytest.raises(ValueError, match="could not be parsed"):
                config.validate()

    def test_jwt_verifier_sources_are_mutually_exclusive(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_API_KEY", "test-key")
        public_key = tmp_path / "public.pem"
        public_key.write_text("test", encoding="utf-8")
        monkeypatch.setenv("INTARIS_JWT_PUBLIC_KEY", str(public_key))
        monkeypatch.setenv("INTARIS_JWKS_URL", "https://example.com/jwks.json")
        with pytest.raises(ValueError, match="Configure only one JWT verifier source"):
            Config(llm=LLMConfig()).validate()

    def test_missing_jwt_public_key_path_raises(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "test-key")
        monkeypatch.setenv("INTARIS_JWT_PUBLIC_KEY", "/tmp/does-not-exist.pem")
        monkeypatch.delenv("INTARIS_JWKS_URL", raising=False)
        with pytest.raises(ValueError, match="does not exist"):
            Config(llm=LLMConfig()).validate()


class TestConfigEnvVars:
    """Test configuration from environment variables."""

    def test_custom_port(self, monkeypatch):
        monkeypatch.setenv("INTARIS_PORT", "9090")
        config = ServerConfig()
        assert config.port == 9090

    def test_custom_host(self, monkeypatch):
        monkeypatch.setenv("INTARIS_HOST", "127.0.0.1")
        config = ServerConfig()
        assert config.host == "127.0.0.1"

    def test_custom_db_path(self, monkeypatch):
        monkeypatch.setenv("DB_PATH", "/tmp/test.db")
        config = DBConfig()
        assert config.path == "/tmp/test.db"

    def test_custom_db_pool_size(self, monkeypatch):
        monkeypatch.setenv("DB_POOL_MIN_CONN", "2")
        monkeypatch.setenv("DB_POOL_MAX_CONN", "25")
        config = DBConfig()
        assert config.pool_min_conn == 2
        assert config.pool_max_conn == 25

    def test_invalid_db_pool_bounds(self, monkeypatch):
        monkeypatch.setenv("LLM_API_KEY", "test-key")
        config = Config(llm=LLMConfig())
        config.db.pool_min_conn = 5
        config.db.pool_max_conn = 4
        with pytest.raises(
            ValueError, match="DB_POOL_MIN_CONN cannot be greater than DB_POOL_MAX_CONN"
        ):
            config.validate()

    def test_custom_llm_model(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL", "gpt-4o")
        config = LLMConfig()
        assert config.model == "gpt-4o"

    def test_llm_api_key_fallback(self, monkeypatch):
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
        config = LLMConfig()
        assert config.api_key == "sk-test-key"

    def test_data_dir(self, monkeypatch):
        monkeypatch.setenv("DATA_DIR", "/custom/data")
        config = DBConfig()
        assert config.path == "/custom/data/intaris.db"

    def test_custom_timeout(self, monkeypatch):
        monkeypatch.setenv("LLM_TIMEOUT_MS", "3000")
        config = LLMConfig()
        assert config.timeout_ms == 3000


class TestParseApiKeys:
    """Test _parse_api_keys() helper."""

    def test_empty_env(self, monkeypatch):
        monkeypatch.delenv("INTARIS_API_KEYS", raising=False)
        assert _parse_api_keys() == {}

    def test_valid_json(self, monkeypatch):
        monkeypatch.setenv(
            "INTARIS_API_KEYS",
            '{"sk-key1": "alice", "sk-key2": "*"}',
        )
        result = _parse_api_keys()
        assert result == {"sk-key1": "alice", "sk-key2": "*"}

    def test_invalid_json(self, monkeypatch):
        monkeypatch.setenv("INTARIS_API_KEYS", "not-json")
        result = _parse_api_keys()
        assert result == {}

    def test_non_object_json(self, monkeypatch):
        monkeypatch.setenv("INTARIS_API_KEYS", '["a", "b"]')
        result = _parse_api_keys()
        assert result == {}

    def test_values_coerced_to_str(self, monkeypatch):
        monkeypatch.setenv("INTARIS_API_KEYS", '{"key": 123}')
        result = _parse_api_keys()
        assert result == {"key": "123"}

    def test_server_config_loads_api_keys(self, monkeypatch):
        monkeypatch.setenv(
            "INTARIS_API_KEYS",
            '{"sk-test": "testuser"}',
        )
        config = ServerConfig()
        assert config.api_keys == {"sk-test": "testuser"}
