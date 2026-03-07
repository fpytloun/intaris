"""Fernet encryption for secrets at rest.

Provides stateless encrypt/decrypt functions for protecting sensitive
configuration values (env vars, HTTP headers) stored in the database.
Uses Fernet (AES-128-CBC + HMAC-SHA256) from the cryptography package.

Key management:
- Key is provided via INTARIS_ENCRYPTION_KEY environment variable
- Generate a key: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
- Key is URL-safe base64 encoded, 32 bytes decoded
"""

from __future__ import annotations

import logging

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def encrypt(plaintext: str, key: str) -> str:
    """Encrypt a string using Fernet.

    Args:
        plaintext: The string to encrypt.
        key: Fernet key (URL-safe base64, 32 bytes).

    Returns:
        Fernet token as a string.

    Raises:
        ValueError: If the key is invalid.
    """
    try:
        f = Fernet(key.encode())
    except Exception as e:
        raise ValueError(f"Invalid encryption key: {e}") from e
    return f.encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, key: str) -> str:
    """Decrypt a Fernet token.

    Args:
        ciphertext: Fernet token string.
        key: Fernet key (URL-safe base64, 32 bytes).

    Returns:
        Decrypted plaintext string.

    Raises:
        ValueError: If the key is invalid or decryption fails.
    """
    try:
        f = Fernet(key.encode())
    except Exception as e:
        raise ValueError(f"Invalid encryption key: {e}") from e
    try:
        return f.decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise ValueError("Decryption failed — wrong key or corrupted data") from e


def validate_key(key: str) -> bool:
    """Check if a string is a valid Fernet key.

    Args:
        key: Candidate Fernet key.

    Returns:
        True if valid, False otherwise.
    """
    try:
        Fernet(key.encode())
        return True
    except Exception:
        return False


def generate_key() -> str:
    """Generate a new Fernet encryption key.

    Returns:
        URL-safe base64 encoded key string.
    """
    return Fernet.generate_key().decode()
