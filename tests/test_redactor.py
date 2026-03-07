"""Tests for the secret redactor."""

from __future__ import annotations

from intaris.redactor import redact


class TestAPIKeyRedaction:
    """Test API key pattern redaction."""

    def test_openai_key(self):
        args = {
            "command": "curl -H 'Authorization: Bearer sk-abc123def456ghi789jkl012mno345pqr678stu901'"
        }
        result = redact(args)
        assert "sk-abc" not in result["command"]
        assert "[REDACTED:api_key]" in result["command"]

    def test_aws_access_key(self):
        args = {"env": "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"}
        result = redact(args)
        assert "AKIAIOSFODNN7EXAMPLE" not in result["env"]
        assert "[REDACTED:aws_key]" in result["env"]

    def test_github_token(self):
        args = {
            "command": "git clone https://ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx@github.com/repo.git"
        }
        result = redact(args)
        assert "ghp_" not in result["command"]
        assert "[REDACTED:github_token]" in result["command"]

    def test_github_pat(self):
        args = {
            "command": "export GITHUB_TOKEN=github_pat_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        }
        result = redact(args)
        assert "github_pat_" not in result["command"]
        assert "[REDACTED:github_token]" in result["command"]

    def test_slack_token(self):
        args = {"token": "xoxb-123456789012-123456789012-abcdefghijklmnop"}
        result = redact(args)
        assert "xoxb-" not in result["token"]

    def test_gitlab_token(self):
        args = {"command": "export GL_TOKEN=glpat-xxxxxxxxxxxxxxxxxxxx"}
        result = redact(args)
        assert "glpat-" not in result["command"]
        assert "[REDACTED:gitlab_token]" in result["command"]


class TestConnectionStringRedaction:
    """Test connection string redaction."""

    def test_postgresql(self):
        args = {"dsn": "postgresql://user:pass@host:5432/db"}
        # "dsn" is a sensitive key, so entire value is redacted
        result = redact(args)
        assert "postgresql://" not in result["dsn"]

    def test_mongodb(self):
        args = {"command": "mongosh mongodb://user:pass@host:27017/db"}
        result = redact(args)
        assert "mongodb://" not in result["command"]
        assert "[REDACTED:connection_string]" in result["command"]

    def test_redis(self):
        args = {"url": "redis://user:pass@host:6379/0"}
        result = redact(args)
        assert "redis://" not in result["url"]


class TestPasswordRedaction:
    """Test password pattern redaction."""

    def test_password_in_command(self):
        args = {"command": "mysql -u root password=secret123"}
        result = redact(args)
        assert "secret123" not in result["command"]
        assert "[REDACTED:password]" in result["command"]

    def test_api_key_in_env(self):
        args = {"command": "export api_key=my_secret_key_123"}
        result = redact(args)
        assert "my_secret_key_123" not in result["command"]
        assert "[REDACTED:credential]" in result["command"]


class TestSensitiveKeys:
    """Test sensitive key name detection."""

    def test_password_key(self):
        args = {"password": "my_secret"}
        result = redact(args)
        assert result["password"] == "[REDACTED:credential]"

    def test_token_key(self):
        args = {"token": "abc123"}
        result = redact(args)
        assert result["token"] == "[REDACTED:credential]"

    def test_api_key_key(self):
        args = {"api_key": "sk-something"}
        result = redact(args)
        assert result["api_key"] == "[REDACTED:credential]"

    def test_secret_key(self):
        args = {"secret": "very_secret"}
        result = redact(args)
        assert result["secret"] == "[REDACTED:credential]"

    def test_authorization_key(self):
        args = {"authorization": "Bearer token123"}
        result = redact(args)
        assert result["authorization"] == "[REDACTED:credential]"


class TestNestedRedaction:
    """Test redaction in nested structures."""

    def test_nested_dict(self):
        args = {
            "config": {
                "password": "secret",
                "host": "localhost",
            }
        }
        result = redact(args)
        assert result["config"]["password"] == "[REDACTED:credential]"
        assert result["config"]["host"] == "localhost"

    def test_list_values(self):
        args = {
            "commands": [
                "echo hello",
                "export api_key=secret123",
            ]
        }
        result = redact(args)
        assert result["commands"][0] == "echo hello"
        assert "[REDACTED:credential]" in result["commands"][1]

    def test_deeply_nested(self):
        args = {
            "level1": {
                "level2": {
                    "password": "deep_secret",
                }
            }
        }
        result = redact(args)
        assert result["level1"]["level2"]["password"] == "[REDACTED:credential]"


class TestJWTRedaction:
    """Test JWT token redaction."""

    def test_jwt_token(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        args = {"header": f"Authorization: Bearer {jwt}"}
        result = redact(args)
        assert "eyJ" not in result["header"]


class TestPrivateKeyRedaction:
    """Test private key block redaction."""

    def test_rsa_private_key(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAKCAQEA...\n-----END RSA PRIVATE KEY-----"
        args = {"content": f"Key: {key}"}
        result = redact(args)
        assert "BEGIN RSA PRIVATE KEY" not in result["content"]
        assert "[REDACTED:private_key]" in result["content"]


class TestImmutability:
    """Test that redaction never mutates the input."""

    def test_original_unchanged(self):
        original = {
            "password": "secret",
            "nested": {"token": "abc123"},
        }
        # Keep a reference to original values
        original_password = original["password"]
        original_token = original["nested"]["token"]

        result = redact(original)

        # Original should be unchanged
        assert original["password"] == original_password
        assert original["nested"]["token"] == original_token

        # Result should be redacted
        assert result["password"] == "[REDACTED:credential]"
        assert result["nested"]["token"] == "[REDACTED:credential]"


class TestFalsePositives:
    """Test that non-secret values are not redacted."""

    def test_normal_commands(self):
        args = {"command": "ls -la /home/user/project"}
        result = redact(args)
        assert result["command"] == args["command"]

    def test_normal_paths(self):
        args = {"path": "/usr/local/bin/python"}
        result = redact(args)
        assert result["path"] == args["path"]

    def test_normal_text(self):
        args = {"content": "This is a normal text with no secrets."}
        result = redact(args)
        assert result["content"] == args["content"]

    def test_non_string_values(self):
        args = {"count": 42, "enabled": True, "ratio": 3.14}
        result = redact(args)
        assert result == args
