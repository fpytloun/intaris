"""Tests for the secret redactor."""

from __future__ import annotations

from intaris.redactor import redact


class TestAPIKeyRedaction:
    """Test API key pattern redaction."""

    def test_openai_key(self):
        args = {
            "command": "curl -H 'Authorization: Bearer sk-proj-Abc123Def456Ghi789Jkl012Mno345Pqr678'"
        }
        result = redact(args)
        assert "sk-proj" not in result["command"]
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


class TestFalsePositiveRegression:
    """Regression tests for known false positive scenarios.

    These test real-world tool call arguments that were incorrectly
    redacted by overly broad patterns.
    """

    # ── Pattern 3: AWS secret (40-char base64-like) ───────────────────

    def test_file_path_not_redacted_as_aws_secret(self):
        """File paths must not trigger aws_secret pattern.

        /Users/fpytloun/src/intaris/intaris/api/ is exactly 40 chars of
        [A-Za-z0-9+/] and was being matched as an AWS secret.
        """
        args = {
            "prompt": "Read files from /Users/fpytloun/src/intaris/intaris/api/ and return"
        }
        result = redact(args)
        assert result["prompt"] == args["prompt"]

    def test_another_40_char_path_not_redacted(self):
        """Other 40-char paths must not trigger aws_secret pattern."""
        args = {
            "prompt": "Read files from /Users/fpytloun/src/intaris/intaris/mcp/ and return"
        }
        result = redact(args)
        assert result["prompt"] == args["prompt"]

    def test_sha1_hash_not_redacted_as_aws_secret(self):
        """Git SHA-1 hashes (40 hex chars) must not trigger aws_secret."""
        args = {"command": "git show da39a3ee5e6b4b0d3255bfef95601890afd80709"}
        result = redact(args)
        assert "da39a3ee" in result["command"]
        assert "[REDACTED" not in result["command"]

    def test_url_path_not_redacted_as_aws_secret(self):
        """URL paths must not trigger aws_secret pattern."""
        args = {"url": "https://github.com/fpytloun/intaris/blob/main/intaris"}
        result = redact(args)
        assert result["url"] == args["url"]

    def test_real_aws_secret_still_redacted(self):
        """Real AWS secrets (random base64, mixed case + digits) are caught."""
        # AWS example key from docs
        args = {"command": "export AWS_SECRET=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"}
        result = redact(args)
        assert "wJalrXUtnFEMI" not in result["command"]
        assert "[REDACTED:" in result["command"]

    def test_all_uppercase_40_chars_not_redacted(self):
        """All-uppercase 40-char strings are not AWS secrets (no lowercase)."""
        args = {"content": " AAAAAAAAAAAABBBBBBBBBBBBCCCCCCCCCCCCDDDDDDDD "}
        result = redact(args)
        assert result["content"] == args["content"]

    # ── Pattern 1: OpenAI sk- prefix ─────────────────────────────────

    def test_git_branch_sk_prefix_not_redacted(self):
        """Git branch names with sk- prefix must not be redacted."""
        args = {"command": "git checkout feature/sk-cleanup-old-sessions"}
        result = redact(args)
        assert "sk-cleanup-old-sessions" in result["command"]
        assert "[REDACTED" not in result["command"]

    def test_lowercase_sk_identifier_not_redacted(self):
        """Lowercase-only sk- identifiers are not API keys."""
        args = {"command": "docker run sk-service-worker-container-name"}
        result = redact(args)
        assert "sk-service-worker-container-name" in result["command"]

    def test_real_openai_key_still_redacted(self):
        """Real OpenAI keys with mixed case + digits are still caught."""
        args = {"command": "export KEY=sk-proj-Abc123Def456Ghi789Jkl012Mno345Pqr678"}
        result = redact(args)
        assert "[REDACTED:api_key]" in result["command"]

    # ── Pattern 10: credential key=value ─────────────────────────────

    def test_python_code_api_key_not_redacted(self):
        """Python code with api_key=os.getenv() must not be redacted."""
        args = {"content": "client = OpenAI(api_key=os.getenv('OPENAI_API_KEY'))"}
        result = redact(args)
        assert "api_key=os" in result["content"]
        assert "[REDACTED:credential]" not in result["content"]

    def test_prefixed_variable_api_key_not_redacted(self):
        """Variable names containing api_key must not be redacted."""
        args = {"content": "validate_api_key=True"}
        result = redact(args)
        assert result["content"] == args["content"]

    def test_env_var_prefixed_key_not_redacted(self):
        """Env vars like OPENAI_API_KEY= must not be redacted."""
        args = {"command": "export OPENAI_API_KEY=test"}
        result = redact(args)
        assert "OPENAI_API_KEY=test" in result["command"]

    def test_grep_api_key_pattern_not_redacted(self):
        """Grep patterns searching for api_key= must not be redacted."""
        args = {"command": "grep -r 'api_key=' src/"}
        result = redact(args)
        assert "api_key=" in result["command"]

    def test_api_key_none_not_redacted(self):
        """api_key=None in code must not be redacted (too short)."""
        args = {"content": "api_key=None"}
        result = redact(args)
        assert result["content"] == args["content"]

    def test_real_credential_key_value_still_redacted(self):
        """Actual credential in key=value format must still be caught."""
        args = {"command": "curl -H 'api_key=sk1234abcdEFGH5678ijkl'"}
        result = redact(args)
        assert "[REDACTED:credential]" in result["command"]

    def test_access_token_short_code_value_not_redacted(self):
        """Python code with short value after access_token= not redacted."""
        args = {"content": "access_token=resp.json()"}
        result = redact(args)
        # "resp" is only 4 chars — below the 8-char minimum
        assert "[REDACTED:credential]" not in result["content"]

    def test_access_token_short_function_call_not_redacted(self):
        """Python code with short function call after access_token= not redacted."""
        args = {"content": "access_token=get()"}
        result = redact(args)
        # "get" is only 3 chars — below the 8-char minimum
        assert "[REDACTED:credential]" not in result["content"]

    # ── Pattern 11: password key=value ───────────────────────────────

    def test_shell_pwd_variable_not_redacted(self):
        """Shell PWD variable must not be redacted as password."""
        args = {"command": "PWD=/home/user/project make build"}
        result = redact(args)
        assert "PWD=/home/user/project" in result["command"]

    def test_change_password_not_redacted(self):
        """Compound variable names with password must not be redacted."""
        args = {"content": "change_password=True"}
        result = redact(args)
        assert result["content"] == args["content"]

    def test_reset_password_not_redacted(self):
        """Prefixed password variable names must not be redacted."""
        args = {"content": "reset_password=new_value"}
        result = redact(args)
        assert result["content"] == args["content"]

    def test_real_password_value_still_redacted(self):
        """Actual password in key=value format must still be caught."""
        args = {"command": "mysql -u root password=MyS3cretPass123"}
        result = redact(args)
        assert "MyS3cretPass123" not in result["command"]
        assert "[REDACTED:password]" in result["command"]

    def test_password_short_value_not_redacted(self):
        """Short password values (< 8 chars) are not redacted."""
        args = {"content": "password=test"}
        result = redact(args)
        assert result["content"] == args["content"]

    # ── Combined / edge cases ────────────────────────────────────────

    def test_task_tool_prompt_not_corrupted(self):
        """Task tool prompts with file paths must not be corrupted.

        This is the original bug report: a Task tool call with a file
        path in the prompt was having the path redacted as aws_secret.
        """
        args = {
            "description": "Explore intaris API routes",
            "prompt": (
                "Read the following files from /Users/fpytloun/src/intaris/intaris/api/ "
                "and return a comprehensive summary of all API endpoints."
            ),
            "subagent_type": "explore",
        }
        result = redact(args)
        assert result == args

    def test_webfetch_url_not_corrupted(self):
        """WebFetch tool URLs must not be corrupted.

        GitHub URLs with long paths were being partially redacted.
        """
        args = {
            "url": "https://github.com/fpytloun/intaris/blob/main/intaris/redactor.py",
            "format": "markdown",
        }
        result = redact(args)
        assert result == args

    def test_bash_git_operations_not_corrupted(self):
        """Common git operations must not be corrupted."""
        args = {"command": "git diff da39a3ee5e6b4b0d3255bfef95601890afd80709..HEAD"}
        result = redact(args)
        assert "da39a3ee" in result["command"]
        assert "[REDACTED" not in result["command"]
