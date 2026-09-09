"""Redaction of secrets and regulated data."""

from __future__ import annotations

from app.safety.redaction import REDACTED, Redactor


def test_known_secret_values_are_replaced_everywhere() -> None:
    redactor = Redactor(["teller-pass", "sk-live-abcdef123456"])
    text = "typed teller-pass; key=sk-live-abcdef123456; again teller-pass"
    assert redactor.redact_text(text) == f"typed {REDACTED}; key={REDACTED}; again {REDACTED}"


def test_short_secrets_are_ignored_to_avoid_false_positives() -> None:
    redactor = Redactor(["ab"])
    assert redactor.secret_count == 0
    assert redactor.redact_text("cab") == "cab"


def test_secret_like_keys_are_masked_in_structures() -> None:
    redactor = Redactor()
    data = {
        "access_code": "x",
        "Authorization": "Bearer y",
        "cookie": "z",
        "nested": {"password": "p", "api-key": "k", "member_id": "12345"},
        "list": [{"token": "t"}],
        "empty_password": "",
    }
    out = redactor.redact(data)
    assert out["access_code"] == REDACTED and out["Authorization"] == REDACTED
    assert out["cookie"] == REDACTED and out["nested"]["password"] == REDACTED
    assert out["nested"]["api-key"] == REDACTED and out["list"][0]["token"] == REDACTED
    assert out["nested"]["member_id"] == "12345"
    assert out["empty_password"] == ""


def test_token_counters_are_evidence_not_secrets() -> None:
    redactor = Redactor()
    usage = {
        "prompt_tokens": 1362,
        "completion_tokens": 88,
        "id_token": "eyJ...",
        "auth-token": "x",
    }
    out = redactor.redact(usage)
    assert out["prompt_tokens"] == 1362 and out["completion_tokens"] == 88
    assert out["id_token"] == REDACTED and out["auth-token"] == REDACTED


def test_pii_patterns_are_masked_but_business_values_survive() -> None:
    redactor = Redactor()
    text = (
        "acct 8801238765 ssn 123-45-6789 card 4111 1111 1111 1111 mail a.b@example.com "
        "phone 555-123-4567 member 12345 balance $8,432.17"
    )
    out = redactor.redact_text(text)
    assert "[REDACTED:account]" in out and "[REDACTED:ssn]" in out
    assert "[REDACTED:card]" in out and "[REDACTED:email]" in out and "[REDACTED:phone]" in out
    assert "12345" in out and "$8,432.17" in out
    assert "8801238765" not in out


def test_hash_hint_is_deterministic_and_non_reversible() -> None:
    hint = Redactor.hash_hint("teller-pass")
    assert hint == Redactor.hash_hint("teller-pass") and hint.startswith("sha256:")
    assert "teller" not in hint
