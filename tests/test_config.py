"""Tests for configuration loading and validation."""

from __future__ import annotations

import os

import pytest

from intaris.config import Config, DBConfig, LLMConfig, ServerConfig, _parse_api_keys


class TestConfigDefaults:
    """Test default configuration values."""

    def test_llm_defaults(self):
        config = LLMConfig()
        assert config.model == "gpt-4.1-nano"
        assert config.temperature == 0.1
        assert config.timeout_ms == 4000

    def test_db_defaults(self):
        config = DBConfig()
        assert config.path.endswith("intaris.db")
        assert ".intaris" in config.path

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
