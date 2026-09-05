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
import re
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.entities import html5
from html.parser import HTMLParser
from typing import Any, ClassVar, Iterator
from urllib.parse import urlsplit
from xml.etree import ElementTree

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

#: En-têtes de navigateur, réservés aux agrégateurs (US-2.3.0).
#:
#: Les ATS acceptent le User-Agent « JobRadar » ; les agrégateurs, non :
#: l'audit du 2026-09-05 a montré que 17 des 25 sites d'abord classés
#: « 403, inexploitable » répondent 200 dès qu'on envoie un `User-Agent`,
#: un `Accept-Language` et un `Referer` de navigateur. On ne se cache pas
#: derrière ces en-têtes pour contourner une interdiction — les sources qui
#: refusent explicitement le crawl (`Disallow: /`) restent hors périmètre,
#: voir `docs/sources-audit.md` — on évite seulement d'être filtré à tort.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BROWSER_HEADERS = {
    "User-Agent": BROWSER_USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "application/json, text/xml, text/plain, */*",
}

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

    #: L'entrée de configuration doit-elle porter un `token` ?
    #:
    #: Vrai pour un ATS : le token désigne le board de l'entreprise. Faux
    #: pour un agrégateur, qui n'a qu'un seul endpoint global — le registre
    #: (Feature 2.1) s'appuie dessus pour ne pas rejeter comme malformée une
    #: entrée légitimement sans token (US-2.3.0).
    requires_token: ClassVar[bool] = True

    #: Nombre maximum d'appels par jour autorisé par la plateforme, ou
    #: `None` quand elle n'annonce aucune limite.
    #:
    #: La contrainte appartient à la plateforme, pas à une ligne de YAML :
    #: la déclarer ici la rend impossible à perdre en éditant la config. Le
    #: registre s'en sert pour répartir les sources entre les deux cadences
    #: de collecte (`split_by_cadence`), et un test vérifie que le cron de
    #: la cadence lente respecte la plus basse de ces limites.
    max_calls_per_day: ClassVar[int | None] = None

    @abstractmethod
    def fetch(self, source: Source) -> list[RawJob]:
        """Retourne les offres publiées sur le board de `source`."""


class HttpAdapter(Adapter):
    """Base des adaptateurs qui interrogent une API HTTP (JSON ou XML).

    Le client `httpx` est injectable : les tests passent un
    `httpx.MockTransport` et tournent donc entièrement hors réseau.
    """

    #: Timeout appliqué quand l'appelant n'en impose pas. Surchargeable par
    #: adaptateur : RemoteOK répond en ~40 s, le défaut de 15 s l'exclurait.
    default_timeout: ClassVar[float] = DEFAULT_TIMEOUT

    #: En-têtes envoyés à chaque requête. `AggregatorAdapter` les remplace
    #: par des en-têtes de navigateur.
    headers: ClassVar[dict[str, str]] = DEFAULT_HEADERS

    def __init__(
        self, client: httpx.Client | None = None, timeout: float | None = None
    ) -> None:
        self._client = client
        self._timeout = self.default_timeout if timeout is None else timeout

    def _request_headers(self, url: str) -> dict[str, str]:
        """En-têtes de la requête vers `url`. Surchargée par les agrégateurs."""
        return dict(self.headers)

    @contextmanager
    def _session(self) -> Iterator[httpx.Client]:
        if self._client is not None:
            yield self._client
        else:
            with httpx.Client(follow_redirects=True) as client:
                yield client

    def _request(
        self,
        source: Source,
        url: str,
        *,
        method: str = "GET",
        json_body: Any | None = None,
    ) -> httpx.Response | None:
        """Appelle `url` et retourne la réponse, ou `None` sur 404.

        Les en-têtes sont posés par requête (et non sur le client) pour que
        le User-Agent parte aussi quand un client est injecté par un test.
        """
        try:
            with self._session() as client:
                response = client.request(
                    method,
                    url,
                    headers=self._request_headers(url),
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

        return response

    def _request_json(
        self,
        source: Source,
        url: str,
        *,
        method: str = "GET",
        json_body: Any | None = None,
    ) -> Any | None:
        """Appelle `url` et retourne le JSON décodé, ou `None` sur 404."""
        response = self._request(source, url, method=method, json_body=json_body)
        if response is None:
            return None

        try:
            return response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise AdapterError(
                f"source « {source.nom} » ({self.ats}) : JSON malformé depuis {url} — {exc}"
            ) from exc

    def _request_text(self, source: Source, url: str) -> str | None:
        """Appelle `url` et retourne le corps en texte, ou `None` sur 404.

        Utilisé par les flux RSS, que `response.json()` ne saurait pas lire.
        """
        response = self._request(source, url)
        return None if response is None else response.text

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
    timestamp epoch en millisecondes (Lever) ; plus, côté agrégateurs, le
    RFC-822 des `<pubDate>` RSS (`Mon, 17 Aug 2026 19:21:19 +0000`). Une
    date naïve est considérée UTC. Toute valeur illisible donne `""`
    plutôt qu'une erreur.
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
            try:
                parsed = parsedate_to_datetime(value.strip())
            except (TypeError, ValueError):
                return ""
    else:
        return ""

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()


_REMOTE_HINTS = ("remote", "anywhere", "distributed", "télétravail", "teletravail")
_HYBRID_HINTS = ("hybrid", "hybride")
_ONSITE_HINTS = ("on-site", "onsite", "on site", "in-office", "sur site")


def infer_remote_type(*texts: str, fallback: str = UNKNOWN_REMOTE_TYPE) -> str:
    """Devine le mode de travail à partir d'un texte de localisation.

    Utilisé par les ATS qui n'exposent aucun champ dédié (Greenhouse) et
    par les agrégateurs, dont aucun n'en a. Retourne `fallback` — `unknown`
    sauf mention contraire — si rien ne ressort du texte ; les plateformes
    qui ne publient que du télétravail y passent `remote`.
    """
    blob = " ".join(t for t in texts if t).lower()
    if not blob:
        return fallback
    if any(hint in blob for hint in _REMOTE_HINTS):
        return REMOTE
    if any(hint in blob for hint in _HYBRID_HINTS):
        return HYBRID
    if any(hint in blob for hint in _ONSITE_HINTS):
        return ONSITE
    return fallback


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


# ---------------------------------------------------------------------------
# Socle agrégateur (US-2.3.0)
# ---------------------------------------------------------------------------


class AggregatorAdapter(HttpAdapter):
    """Base des adaptateurs d'agrégateurs.

    Un agrégateur diffère d'un ATS sur trois points, réglés ici une fois
    pour toutes plutôt que répétés dans chacun des onze adaptateurs :

    1. **L'entreprise vient de l'offre**, pas de `Source.nom`. Un board
       Greenhouse appartient à une entreprise ; RemoteOK publie pour des
       milliers. `Source.nom` ne nomme donc que la plateforme, et sert
       uniquement à identifier la source dans les journaux.
    2. **Pas de `token`** : l'endpoint est global. `requires_token = False`
       le dit au registre (Feature 2.1), qui accepte alors une entrée sans
       token au lieu de la rejeter comme malformée.
    3. **En-têtes de navigateur** : voir `BROWSER_HEADERS`. Le `Referer`
       est dérivé de `site_url`, car plusieurs agrégateurs filtrent sur son
       absence.

    S'y ajoute la règle anti-scam de `website.md` : une offre dont
    l'entreprise n'est pas identifiable est **écartée** et le rejet
    journalisé. Une offre anonyme ne permet ni de juger l'employeur, ni
    d'adapter un CV, et c'est le premier signal d'une annonce frauduleuse.

    Une sous-classe implémente `_entries` (récupérer les enregistrements
    bruts) et `_parse` (traduire un enregistrement en `RawJob`) ; `fetch`
    orchestre les deux et applique les règles ci-dessus.
    """

    requires_token: ClassVar[bool] = False
    headers: ClassVar[dict[str, str]] = BROWSER_HEADERS

    #: Racine du site, envoyée en `Referer`.
    site_url: ClassVar[str] = ""

    def _request_headers(self, url: str) -> dict[str, str]:
        headers = dict(self.headers)
        headers["Referer"] = self.site_url or origin_of(url)
        return headers

    def fetch(self, source: Source) -> list[RawJob]:
        jobs: list[RawJob] = []
        for entry in self._entries(source):
            try:
                job = self._parse(source, entry)
            except AdapterError:
                raise
            except Exception as exc:  # noqa: BLE001 — une offre cassée n'arrête pas le flux
                logger.warning(
                    "agrégateur « %s » (%s) : offre ignorée, parsing impossible — %s",
                    source.nom,
                    self.ats,
                    exc,
                )
                continue

            if job is None:
                continue

            if not job.entreprise:
                # Règle anti-scam : sans employeur identifiable, l'offre est
                # inexploitable (ni jugement, ni CV adapté) et souvent fausse.
                logger.warning(
                    "agrégateur « %s » (%s) : offre « %s » écartée, "
                    "entreprise non identifiable (%s)",
                    source.nom,
                    self.ats,
                    job.titre or "sans titre",
                    job.url or "sans url",
                )
                continue

            jobs.append(job)
        return jobs

    @abstractmethod
    def _entries(self, source: Source) -> list[Any]:
        """Récupère les enregistrements bruts publiés par l'agrégateur."""

    @abstractmethod
    def _parse(self, source: Source, entry: Any) -> RawJob | None:
        """Traduit un enregistrement en `RawJob`, ou `None` pour l'ignorer."""


def origin_of(url: str) -> str:
    """`https://host/chemin?x=1` → `https://host/`, pour le `Referer`."""
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/" if parts.netloc else ""


# ---------------------------------------------------------------------------
# Parseur RSS partagé (US-2.3.0) — 4 des 11 agrégateurs sont des flux RSS
# ---------------------------------------------------------------------------

#: Entités nommées valides en XML. Toutes les autres (`&nbsp;`, `&rsquo;`)
#: sont légales en HTML mais font échouer un parseur XML strict : NoDesk et
#: Jobspresso en publient, on les résout donc avant de parser.
_XML_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})
_NAMED_ENTITY_RE = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")
#: `{http://ns}tag` → `tag` : on indexe les éléments par nom local, pour ne
#: pas dépendre des espaces de noms propres à chaque flux.
_NAMESPACE_RE = re.compile(r"^\{[^}]*\}")


@dataclass(frozen=True)
class RssItem:
    """Un `<item>` de flux RSS, réduit à ce dont les adaptateurs ont besoin.

    - `title` / `link` / `guid` / `description` : les éléments standards
    - `date`   : `<pubDate>` normalisé en ISO-8601 UTC (`""` si absent)
    - `content`: `<content:encoded>`, la description complète que les flux
      WordPress publient à côté d'un `<description>` tronqué
    - `extras` : tout autre élément, indexé par **nom local** — c'est là que
      vivent les champs propres à chaque flux (`region` chez WWR,
      `company` et `location` chez les flux WP Job Manager)
    """

    title: str = ""
    link: str = ""
    guid: str = ""
    description: str = ""
    content: str = ""
    date: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> str:
        """La description la plus complète disponible."""
        return self.content or self.description


def resolve_html_entities(xml_text: str) -> str:
    """Remplace les entités HTML non-XML par leur caractère.

    `&nbsp;` et `&rsquo;` sont valides en HTML mais indéfinies en XML : un
    flux qui en contient fait échouer `ElementTree` avec « undefined
    entity ». On les résout en amont ; les cinq entités XML légales et les
    références numériques (`&#8217;`) sont laissées telles quelles.
    """

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in _XML_ENTITIES:
            return match.group(0)
        return html5.get(f"{name};", match.group(0))

    return _NAMED_ENTITY_RE.sub(replace, xml_text)


def parse_rss(source: Source, ats: str, xml_text: str) -> list[RssItem]:
    """Parse un flux RSS 2.0 en `RssItem`, dans l'ordre du flux.

    Lève `AdapterError` — nommant la source — si le document n'est pas du
    XML exploitable, pour que le Collector traite un flux corrompu comme
    n'importe quelle autre réponse inexploitable.
    """
    try:
        root = ElementTree.fromstring(resolve_html_entities(xml_text))
    except ElementTree.ParseError as exc:
        raise AdapterError(
            f"source « {source.nom} » ({ats}) : flux RSS illisible — {exc}"
        ) from exc

    channel = root.find("channel")
    items = (channel if channel is not None else root).findall("item")

    parsed: list[RssItem] = []
    for item in items:
        fields: dict[str, str] = {}
        for element in item:
            name = _NAMESPACE_RE.sub("", element.tag)
            text = (element.text or "").strip()
            # Un flux peut répéter un élément (`<category>`) : le premier
            # non vide gagne, les suivants n'apportent rien aux adaptateurs.
            if text and name not in fields:
                fields[name] = text

        parsed.append(
            RssItem(
                title=fields.pop("title", ""),
                link=fields.pop("link", ""),
                guid=fields.pop("guid", ""),
                description=fields.pop("description", ""),
                content=fields.pop("encoded", ""),
                date=to_iso_utc(fields.pop("pubDate", "")),
                extras=fields,
            )
        )
    return parsed
