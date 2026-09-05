"""Contrat commun des adaptateurs (US-2.2.0).

Un adaptateur traduit la réponse d'un ATS en une liste de `RawJob`, le
schéma pivot de l'ingestion. Tout le reste du pipeline (normalisation,
rétention, dédup, scoring) ne connaît que `RawJob` : ajouter un ATS se
fait donc en écrivant un seul module ici, sans toucher au reste.

Voir `src/adapters/README.md` pour la marche à suivre pas à pas.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from typing import Any, ClassVar, Iterator

import httpx

from src.core.config import Source

logger = logging.getLogger(__name__)

#: Timeout par requête, en secondes. Le backoff/retry arrive avec la
#: Feature 2.4 (politesse réseau) ; ici on garantit juste qu'un ATS lent
#: ne bloque pas le run indéfiniment.
DEFAULT_TIMEOUT = 15.0

#: User-Agent explicite : on s'identifie plutôt que de se faire passer
#: pour un navigateur, pour ne pas se faire bloquer silencieusement.
USER_AGENT = "JobRadar/0.1 (+https://github.com/louisinayinde/jobs)"

DEFAULT_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

# Valeurs possibles de `RawJob.remote_type`.
REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"
#: Valeur par défaut quand la source ne dit rien de la localisation.
UNKNOWN_REMOTE_TYPE = "unknown"


class AdapterError(Exception):
    """Échec de collecte sur une source précise.

    Levée pour toute réponse inexploitable (JSON malformé, statut HTTP
    d'erreur, schéma inattendu). Le message nomme toujours la source, afin
    que le Collector puisse la marquer en échec et poursuivre avec les
    autres (isolation par source : Feature 2.4.1).
    """


@dataclass(frozen=True)
class RawJob:
    """Offre brute, dans le schéma commun à tous les adaptateurs.

    Tous les champs sont des `str` : un champ absent côté source vaut la
    chaîne vide (jamais `None`), pour qu'aucun consommateur n'ait à se
    protéger d'un type surprise.

    - `id`          : identifiant natif de l'offre côté ATS
    - `ats`         : identifiant de l'adaptateur (`greenhouse`, `lever`, ...)
    - `entreprise`  : `Source.nom`, tel qu'écrit dans `sources.yaml`
    - `titre`       : intitulé du poste
    - `localisation`: localisation telle qu'affichée par l'ATS
    - `remote_type` : `remote` | `hybrid` | `onsite` | `unknown`
    - `url`         : lien public vers l'offre
    - `date`        : date de publication, ISO-8601 UTC (`""` si inconnue)
    - `description` : description en texte brut, sans balises HTML
    """

    id: str
    ats: str
    entreprise: str
    titre: str
    localisation: str
    remote_type: str
    url: str
    date: str
    description: str


class Adapter(ABC):
    """Contrat unique : `fetch(source) -> list[RawJob]`.

    Comportement attendu de toute implémentation :

    - board introuvable (**404**) → liste vide, un avertissement journalisé,
      aucune exception (le token a probablement changé) ;
    - réponse inexploitable (JSON malformé, schéma inattendu, autre statut
      d'erreur) → `AdapterError` nommant la source ;
    - offre individuelle incomplète → offre ignorée et journalisée, les
      autres offres du board sont quand même retournées ;
    - champ manquant → valeur par défaut sûre, jamais de `KeyError`.
    """

    #: Identifiant de l'ATS, celui écrit dans le champ `ats` de `sources.yaml`.
    ats: ClassVar[str]

    @abstractmethod
    def fetch(self, source: Source) -> list[RawJob]:
        """Retourne les offres publiées sur le board de `source`."""


class HttpAdapter(Adapter):
    """Base des adaptateurs qui interrogent une API HTTP JSON.

    Le client `httpx` est injectable : les tests passent un
    `httpx.MockTransport` et tournent donc entièrement hors réseau.
    """

    def __init__(
        self, client: httpx.Client | None = None, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self._client = client
        self._timeout = timeout

    @contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        if self._client is not None:
            yield self._client
        else:
            with httpx.Client(follow_redirects=True) as client:
                yield client

    def _request_json(
        self,
        source: Source,
        url: str,
        *,
        method: str = "GET",
        json_body: Any | None = None,
    ) -> Any | None:
        """Appelle `url` et retourne le JSON décodé, ou `None` sur 404.

        Les en-têtes sont posés par requête (et non sur le client) pour que
        le User-Agent parte aussi quand un client est injecté par un test.
        """
        try:
            with self._session() as client:
                response = client.request(
                    method,
                    url,
                    headers=DEFAULT_HEADERS,
                    json=json_body,
                    timeout=self._timeout,
                )
        except httpx.HTTPError as exc:
            raise AdapterError(
                f"source « {source.nom} » ({self.ats}) : échec de la requête {url} — {exc}"
            ) from exc

        if response.status_code == 404:
            logger.warning(
                "source « %s » (%s) : board introuvable (404) sur %s — "
                "le token « %s » a peut-être changé",
                source.nom,
                self.ats,
                url,
                source.token,
            )
            return None

        if response.status_code >= 400:
            raise AdapterError(
                f"source « {source.nom} » ({self.ats}) : statut HTTP "
                f"{response.status_code} sur {url}"
            )

        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AdapterError(
                f"source « {source.nom} » ({self.ats}) : JSON malformé depuis {url} — {exc}"
            ) from exc

    def _expect_list(self, source: Source, payload: Any, path: str) -> list[Any]:
        """Vérifie qu'on a bien reçu une liste d'offres, sinon `AdapterError`."""
        if not isinstance(payload, list):
            raise AdapterError(
                f"source « {source.nom} » ({self.ats}) : schéma inattendu, "
                f"{path} devait être une liste, reçu {type(payload).__name__}"
            )
        return payload

    def _expect_mapping(self, source: Source, payload: Any, path: str) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AdapterError(
                f"source « {source.nom} » ({self.ats}) : schéma inattendu, "
                f"{path} devait être un mapping, reçu {type(payload).__name__}"
            )
        return payload


# ---------------------------------------------------------------------------
# Helpers de parsing partagés par les adaptateurs
# ---------------------------------------------------------------------------

_BLOCK_TAGS = frozenset(
    {
        "br", "p", "div", "section", "article", "header", "footer",
        "ul", "ol", "table", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
    }
)


class _TextExtractor(HTMLParser):
    """Extrait le texte d'un fragment HTML en gardant la structure en lignes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag == "li":
            self._parts.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        # Pas de saut après `</li>` : l'ouverture de l'item suivant en pose
        # déjà un, et un blanc entre chaque puce rendrait la liste illisible.
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


def html_to_text(raw: str) -> str:
    """Convertit du HTML (éventuellement échappé) en texte brut lisible.

    Greenhouse renvoie son `content` doublement encodé (`&lt;p&gt;`) : on
    déséchappe d'abord, puis on retire les balises. Les entités restantes
    (`&nbsp;`, `&amp;`) sont résolues par le parseur.
    """
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(unescape(raw))
    parser.close()
    text = parser.text().replace("\xa0", " ")
    lines = [line.strip() for line in text.splitlines()]
    # Écrase les lignes vides consécutives laissées par les balises imbriquées.
    cleaned: list[str] = []
    for line in lines:
        if line or (cleaned and cleaned[-1]):
            cleaned.append(line)
    return "\n".join(cleaned).strip()


def to_iso_utc(value: Any) -> str:
    """Normalise une date ATS en ISO-8601 UTC à la seconde, ou `""`.

    Accepte les formats rencontrés côté ATS : ISO avec offset
    (Greenhouse, Ashby), ISO suffixé `Z` (SmartRecruiters, Workable) et
    timestamp epoch en millisecondes (Lever). Une date naïve est
    considérée UTC. Toute valeur illisible donne `""` plutôt qu'une erreur.
    """
    if value is None or value == "":
        return ""

    if isinstance(value, bool):
        return ""

    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) > 1e11 else float(value)
        try:
            parsed = datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return ""
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return ""
    else:
        return ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


_REMOTE_HINTS = ("remote", "anywhere", "distributed", "télétravail", "teletravail")
_HYBRID_HINTS = ("hybrid", "hybride")
_ONSITE_HINTS = ("on-site", "onsite", "on site", "in-office", "sur site")


def infer_remote_type(*texts: str) -> str:
    """Devine le mode de travail à partir d'un texte de localisation.

    Utilisé par les ATS qui n'exposent aucun champ dédié (Greenhouse).
    Retourne `unknown` — la valeur par défaut — si rien ne ressort.
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return UNKNOWN_REMOTE_TYPE
    if any(hint in blob for hint in _REMOTE_HINTS):
        return REMOTE
    if any(hint in blob for hint in _HYBRID_HINTS):
        return HYBRID
    if any(hint in blob for hint in _ONSITE_HINTS):
        return ONSITE
    return UNKNOWN_REMOTE_TYPE


def normalize_remote_type(value: Any, fallback: str = UNKNOWN_REMOTE_TYPE) -> str:
    """Mappe le champ « mode de travail » d'un ATS sur notre vocabulaire.

    Couvre les variantes rencontrées : `Remote`, `hybrid`, `OnSite`,
    `on_site`, `unspecified`, ... Toute valeur non reconnue retombe sur
    `fallback`.
    """
    if not isinstance(value, str):
        return fallback
    key = value.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    return {
        "remote": REMOTE,
        "fullyremote": REMOTE,
        "hybrid": HYBRID,
        "onsite": ONSITE,
        "inoffice": ONSITE,
    }.get(key, fallback)


def clean_str(value: Any) -> str:
    """Retourne `value` en `str` nettoyée, ou `""` si ce n'en est pas une."""
    return value.strip() if isinstance(value, str) else ""


def join_locations(values: Any) -> str:
    """Assemble plusieurs localisations en une chaîne stable, sans doublon."""
    if not isinstance(values, (list, tuple)):
        return ""
    seen: list[str] = []
    for value in values:
        text = clean_str(value)
        if text and text not in seen:
            seen.append(text)
    return "; ".join(seen)
