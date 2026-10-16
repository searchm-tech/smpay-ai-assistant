import base64
import hashlib
import hmac
import re

import pytest

from dbtools.scram import scram_verifier


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_verifier_has_postgres_scram_format():
    v = scram_verifier("a" * 30)
    assert re.fullmatch(r"SCRAM-SHA-256\$4096:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+", v)


def test_verifier_matches_independent_computation():
    salt = bytes(range(16))
    pw = "correct-horse-battery-staple-1234"
    salted = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 4096)
    client_key = hmac.new(salted, b"Client Key", "sha256").digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", "sha256").digest()
    expected = f"SCRAM-SHA-256$4096:{b64(salt)}${b64(stored_key)}:{b64(server_key)}"
    assert scram_verifier(pw, salt=salt) == expected


def test_random_salt_makes_each_verifier_different():
    assert scram_verifier("a" * 30) != scram_verifier("a" * 30)


def test_verifier_never_contains_plaintext():
    pw = "plaintext-password-should-not-appear-1"
    assert pw not in scram_verifier(pw)


def test_non_ascii_password_is_rejected():
    with pytest.raises(ValueError):
        scram_verifier("비밀번호" * 10)
