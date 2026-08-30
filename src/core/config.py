"""Chargement et validation de la configuration déclarative.

Lit `sources.yaml` et `filters.yaml` et les convertit en objets typés.
Toute config invalide (clé manquante, type incorrect, fichier absent ou
YAML corrompu) lève une `ConfigError` explicite plutôt que de propager une
valeur par défaut silencieuse ou une stacktrace PyYAML brute.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_RETENTION_DAYS = 7


class ConfigError(Exception):
    """Configuration invalide : clé manquante, type incorrect, fichier absent."""


@dataclass(frozen=True)
class Source:
    """Une entreprise à surveiller et l'ATS sur lequel interroger ses offres."""

    nom: str
    ats: str
    token: str


@dataclass(frozen=True)
class SeniorityFilter:
    keep: list[str] = field(default_factory=list)
    drop: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class LocationFilter:
    require_any: list[str] = field(default_factory=list)
    relocation_regions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RetentionConfig:
    title_include: list[str] = field(default_factory=list)
    title_exclude: list[str] = field(default_factory=list)
    seniority: SeniorityFilter = field(default_factory=SeniorityFilter)
    location: LocationFilter = field(default_factory=LocationFilter)
    tech_include_any: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ScoringConfig:
    geo_priority: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FiltersConfig:
    retention: RetentionConfig
    scoring: ScoringConfig
    retention_days: int = DEFAULT_RETENTION_DAYS


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise ConfigError(f"fichier de configuration introuvable : {path}")
    try:
        with path.open("r", encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML invalide dans {path} : {exc}") from exc


def _require_str(data: dict, key: str, context: str) -> str:
    if key not in data:
        raise ConfigError(f"clé requise manquante : « {key} » ({context})")
    value = data[key]
    if not isinstance(value, str):
        raise ConfigError(
            f"type invalide pour « {key} » ({context}) : attendu str, reçu {type(value).__name__}"
        )
    return value


def _optional_str_list(data: dict, key: str, context: str) -> list[str]:
    """Liste de str optionnelle. Absente ou explicitement vide → `[]` dans les
    deux cas (comportement documenté et identique : aucune contrainte)."""
    if key not in data or data[key] is None:
        return []
    value = data[key]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"type invalide pour « {key} » ({context}) : attendu liste de chaînes")
    return list(value)


def _optional_int(data: dict, key: str, default: int, context: str) -> int:
    if key not in data or data[key] is None:
        return default
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"type invalide pour « {key} » ({context}) : attendu int, reçu {type(value).__name__}"
        )
    return value


def _require_mapping(data: dict, key: str, context: str) -> dict:
    if key not in data:
        raise ConfigError(f"clé requise manquante : « {key} » ({context})")
    value = data[key]
    if not isinstance(value, dict):
        raise ConfigError(f"type invalide pour « {key} » ({context}) : attendu mapping")
    return value


def _optional_mapping(data: dict, key: str, context: str) -> dict:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"type invalide pour « {key} » ({context}) : attendu mapping")
    return value


def load_sources(path: str | Path) -> list[Source]:
    """Charge `sources.yaml` en une liste de `Source`.

    Échoue avec `ConfigError` sur toute entrée invalide (voir Feature 2.1
    pour un chargement tolérant, qui journalise et ignore les entrées
    cassées plutôt que d'échouer).
    """
    raw = _read_yaml(Path(path))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ConfigError(f"sources.yaml doit contenir une liste, reçu {type(raw).__name__}")

    sources: list[Source] = []
    for index, entry in enumerate(raw):
        context = f"sources.yaml[{index}]"
        if not isinstance(entry, dict):
            raise ConfigError(f"entrée invalide dans {context} : attendu un mapping")
        sources.append(
            Source(
                nom=_require_str(entry, "nom", context),
                ats=_require_str(entry, "ats", context),
                token=_require_str(entry, "token", context),
            )
        )
    return sources


def _parse_retention(data: dict) -> RetentionConfig:
    context = "filters.yaml:retention"
    seniority_raw = _optional_mapping(data, "seniority", context)
    location_raw = _optional_mapping(data, "location", context)

    return RetentionConfig(
        title_include=_optional_str_list(data, "title_include", context),
        title_exclude=_optional_str_list(data, "title_exclude", context),
        seniority=SeniorityFilter(
            keep=_optional_str_list(seniority_raw, "keep", f"{context}.seniority"),
            drop=_optional_str_list(seniority_raw, "drop", f"{context}.seniority"),
        ),
        location=LocationFilter(
            require_any=_optional_str_list(location_raw, "require_any", f"{context}.location"),
            relocation_regions=_optional_str_list(
                location_raw, "relocation_regions", f"{context}.location"
            ),
        ),
        tech_include_any=_optional_str_list(data, "tech_include_any", context),
    )


def _parse_scoring(data: dict) -> ScoringConfig:
    context = "filters.yaml:scoring"
    geo_raw = _optional_mapping(data, "geo_priority", context)

    geo_priority: dict[str, int] = {}
    for key, value in geo_raw.items():
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(
                f"type invalide pour « geo_priority.{key} » ({context}) : "
                f"attendu int, reçu {type(value).__name__}"
            )
        geo_priority[key] = value

    return ScoringConfig(geo_priority=geo_priority)


def load_filters(path: str | Path) -> FiltersConfig:
    """Charge `filters.yaml` en `FiltersConfig`.

    Lève `ConfigError` si `retention` ou `scoring` est absent, si un champ
    a un type incorrect (ex : `retention_days: "sept"`), ou si le fichier
    est absent/corrompu.
    """
    raw = _read_yaml(Path(path))
    if not isinstance(raw, dict):
        kind = "vide" if raw is None else type(raw).__name__
        raise ConfigError(f"filters.yaml doit contenir un mapping, reçu {kind}")

    context = "filters.yaml"
    retention_raw = _require_mapping(raw, "retention", context)
    scoring_raw = _require_mapping(raw, "scoring", context)

    return FiltersConfig(
        retention=_parse_retention(retention_raw),
        scoring=_parse_scoring(scoring_raw),
        retention_days=_optional_int(raw, "retention_days", DEFAULT_RETENTION_DAYS, context),
    )
