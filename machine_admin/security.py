"""Hash de senhas/tokens e criptografia autenticada de segredos."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65_536, parallelism=4)
_CIPHER_VERSION = 1


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("A senha deve ter pelo menos 12 caracteres.")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _PASSWORD_HASHER.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False


class SecretCipher:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("A chave AES-GCM deve possuir 32 bytes.")
        self._cipher = AESGCM(key)

    def encrypt(self, value: str, *, context: str) -> bytes:
        return self.encrypt_bytes(value.encode("utf-8"), context=context)

    def encrypt_bytes(self, value: bytes, *, context: str) -> bytes:
        nonce = secrets.token_bytes(12)
        ciphertext = self._cipher.encrypt(
            nonce, value, context.encode("utf-8")
        )
        return bytes([_CIPHER_VERSION]) + nonce + ciphertext

    def decrypt(self, payload: bytes, *, context: str) -> str:
        return self.decrypt_bytes(payload, context=context).decode("utf-8")

    def decrypt_bytes(self, payload: bytes, *, context: str) -> bytes:
        if len(payload) < 30 or payload[0] != _CIPHER_VERSION:
            raise ValueError("Segredo cifrado inválido ou de versão incompatível.")
        return self._cipher.decrypt(
            payload[1:13], payload[13:], context.encode("utf-8")
        )


def generate_api_token() -> tuple[str, str, str]:
    prefix = secrets.token_hex(4)
    token = f"mc_{prefix}_{secrets.token_urlsafe(32)}"
    return token, prefix, hash_api_token(token)


def hash_api_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_matches(stored_hash: str, candidate: str) -> bool:
    return hmac.compare_digest(stored_hash, hash_api_token(candidate))


def fingerprint_identifier(master_key: bytes, normalized_value: str) -> str:
    return hmac.new(
        master_key,
        f"identifier:{normalized_value}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
