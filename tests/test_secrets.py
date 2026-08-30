"""Tests secrets & intégrité des workflows (US-1.3.T) — volet secrets.

Vérifie que l'absence d'un secret requis échoue tôt et explicitement,
plutôt que de laisser `None` se propager silencieusement.
"""

import pytest

from src.core.secrets import SecretsError, require_env


def test_present_variable_returns_its_value() -> None:
    assert require_env("LLM_API_KEY", env={"LLM_API_KEY": "sk-abc"}) == "sk-abc"


def test_missing_llm_api_key_raises_explicit_error_naming_the_key() -> None:
    with pytest.raises(SecretsError) as excinfo:
        require_env("LLM_API_KEY", env={})

    assert "LLM_API_KEY" in str(excinfo.value)


def test_missing_github_token_raises_explicit_error_naming_the_key() -> None:
    with pytest.raises(SecretsError) as excinfo:
        require_env("GITHUB_TOKEN", env={})

    assert "GITHUB_TOKEN" in str(excinfo.value)


def test_empty_value_is_treated_as_absent_not_as_a_silent_empty_string() -> None:
    with pytest.raises(SecretsError):
        require_env("LLM_API_KEY", env={"LLM_API_KEY": ""})
