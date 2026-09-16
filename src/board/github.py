"""Client GitHub : Issues et Projects (Feature 4.1).

Le tableau de revue est un **GitHub Project** (v2) dont chaque carte est une
**Issue** du dépôt. Publier une offre, c'est donc deux gestes distincts, sur
deux API distinctes :

1. **créer l'Issue** (US-4.1.2) — API REST, `POST /repos/{o}/{r}/issues` ;
2. **la placer sur le tableau** avec le statut « Nouveau » (US-4.1.3) — API
   GraphQL, la seule qui connaisse les Projects v2 : ajout de l'item, puis
   écriture de son champ de statut.

**L'authentification (US-4.1.1) passe par `GITHUB_TOKEN`**, mais pas par le
jeton qu'Actions fabrique tout seul : celui-là est limité au dépôt, et un
Project appartient à un compte. Il faut un jeton personnel avec les scopes
`repo` et `project`, rangé dans un secret et exposé au run sous ce nom —
voir `docs/github-projects.md`. `verifier_acces` le contrôle avant tout
envoi, et `python -m src.board check` le fait depuis un terminal.

**Les erreurs sont nommées, jamais avalées.** Un 401 lève
`GitHubAuthError`, un 403 de droits `GitHubPermissionError`, une ressource
absente `GitHubNotFoundError`. Un appel qui échoue ne rend jamais `None` :
une publication muette qui « réussit » sans rien publier est la pire panne
possible pour un tableau de revue, parce qu'elle ressemble à un marché calme.

**Le débit est attendu, pas subi.** GitHub signale une limite atteinte par un
403 (ou 429) accompagné de `x-ratelimit-reset` ou `retry-after` ; le client
attend l'heure dite puis rejoue, dans la limite de `MAX_RATE_LIMIT_RETRIES`
et de `ATTENTE_MAX`. Au-delà, `GitHubRateLimitError` : mieux vaut un run en
échec que bloquer un runner une heure.

**Une création n'est jamais rejouée à l'aveugle.** Un 502 ou un timeout sur
`POST /issues` ne dit pas si l'Issue existe : la requête a pu aboutir avant
que la réponse se perde. La rejouer créerait un doublon sur le tableau. Seuls
les appels sans effet ou idempotents (lectures, ajout d'item — GitHub rend
l'item existant —, écriture du statut) reçoivent une seconde tentative sur
panne passagère. Un refus de débit, lui, garantit que rien n'a été fait : il
est rejoué pour tous les appels.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from src.adapters.base import USER_AGENT
from src.board.fiche import corps_issue, lire_id, titre_issue
from src.core.config import BoardConfig
from src.core.scoring import ScoredJob
from src.core.secrets import require_env

logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"

#: Version d'API REST épinglée : sans elle, GitHub sert la plus ancienne.
API_VERSION = "2022-11-28"

#: Variable d'environnement qui porte le jeton.
TOKEN_ENV = "GITHUB_TOKEN"

#: Scope OAuth sans lequel un jeton classique ne voit aucun Project.
SCOPE_PROJECT = "project"

TIMEOUT = 30.0

#: Longueur maximale d'un titre d'Issue acceptée par GitHub.
TITRE_MAX = 256

#: Taille de page des listes (Issues, cartes du tableau) : le maximum permis.
PAGE = 100

#: Attentes avant chaque nouvelle tentative sur panne passagère (réseau,
#: 5xx), pour les seuls appels rejouables — voir la docstring du module.
RETRY_DELAYS: tuple[float, ...] = (2.0,)
RETRYABLE_STATUS = frozenset({500, 502, 503, 504})

#: Nombre de fois qu'un même appel peut attendre la fin d'une limite de débit.
MAX_RATE_LIMIT_RETRIES = 3

#: Attente la plus longue qu'on accepte pour une limite de débit, en
#: secondes. La collecte tourne tous les quarts d'heure : attendre davantage
#: ferait chevaucher le run suivant, qui attendrait la même chose.
ATTENTE_MAX = 900.0

#: Attente quand GitHub signale une limite sans dire jusqu'à quand. Sa
#: documentation demande alors « au moins une minute ».
ATTENTE_SANS_INDICATION = 60.0

#: Marge ajoutée à l'heure de réinitialisation : l'horloge du runner et
#: celle de GitHub ne tombent pas pile ensemble.
MARGE_RESET = 1.0


class GitHubError(Exception):
    """Échec d'un appel à GitHub, avec l'appel et le motif dans le message.

    `peut_avoir_abouti` est vrai quand une écriture non rejouable (création
    d'Issue) a échoué sans réponse nette — panne réseau, 5xx : GitHub a pu
    la traiter quand même. Le registre des fiches (US-4.2.2) s'en sert pour
    vérifier au run suivant plutôt que recréer.
    """

    def __init__(self, message: str, *, peut_avoir_abouti: bool = False) -> None:
        super().__init__(message)
        self.peut_avoir_abouti = peut_avoir_abouti


class GitHubAuthError(GitHubError):
    """Jeton refusé (401) : absent, invalide, expiré ou révoqué."""


class GitHubPermissionError(GitHubError):
    """Jeton reconnu mais insuffisant (403 hors limite de débit, scope manquant)."""


class GitHubNotFoundError(GitHubError):
    """Ressource introuvable — ou invisible pour ce jeton, GitHub ne distingue pas."""


class GitHubRateLimitError(GitHubError):
    """Limite de débit toujours atteinte après les attentes permises."""


def _wait(seconds: float) -> None:
    """Attente avant de rejouer. Isolée pour que les tests la neutralisent."""
    time.sleep(seconds)


def _maintenant() -> float:
    """Horloge en secondes epoch. Isolée pour que les tests la fixent."""
    return time.time()


@dataclass(frozen=True)
class Acces:
    """Ce que `verifier_acces` a constaté."""

    repository: str
    #: Scopes du jeton, ou `None` quand GitHub ne les annonce pas (jeton
    #: fine-grained, jeton d'installation d'Actions).
    scopes: tuple[str, ...] | None


@dataclass(frozen=True)
class Issue:
    """Une Issue créée. `node_id` est son identifiant GraphQL, celui qu'attend le Project."""

    number: int
    node_id: str
    url: str
    titre: str


@dataclass(frozen=True)
class Projet:
    """Un Project v2, réduit à ce qu'il faut pour y poser une fiche."""

    id: str
    titre: str
    url: str
    champ_statut_id: str
    #: Nom d'option → identifiant, dans l'ordre des colonnes du tableau.
    options_statut: dict[str, str]

    def option(self, nom: str) -> str | None:
        """Identifiant de l'option `nom`, casse et espaces autour ignorés."""
        cible = nom.strip().casefold()
        for option, identifiant in self.options_statut.items():
            if option.strip().casefold() == cible:
                return identifiant
        return None


@dataclass(frozen=True)
class ItemProjet:
    """La carte d'une Issue sur le tableau, et le statut qu'on lui a donné."""

    id: str
    statut: str


# ---------------------------------------------------------------------------
# Requêtes GraphQL
# ---------------------------------------------------------------------------

#: `repositoryOwner` couvre utilisateur et organisation d'une seule requête :
#: `board.yaml` n'a pas à dire lequel des deux possède le Project.
QUERY_PROJET = """
query($owner: String!, $number: Int!, $champ: String!) {
  repositoryOwner(login: $owner) {
    ... on ProjectV2Owner {
      projectV2(number: $number) {
        id
        title
        url
        field(name: $champ) {
          ... on ProjectV2SingleSelectField { id name options { id name } }
        }
      }
    }
  }
}
"""

#: Les cartes du tableau, page par page, avec le numéro de l'Issue portée.
#: Une carte peut porter une Issue d'un autre dépôt, un brouillon ou une
#: pull request : seules les Issues de `board.yaml` sont gardées.
QUERY_ITEMS = """
query($projet: ID!, $apres: String) {
  node(id: $projet) {
    ... on ProjectV2 {
      items(first: 100, after: $apres) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          content { ... on Issue { number repository { nameWithOwner } } }
        }
      }
    }
  }
}
"""

MUTATION_AJOUT = """
mutation($projet: ID!, $contenu: ID!) {
  addProjectV2ItemById(input: {projectId: $projet, contentId: $contenu}) {
    item { id }
  }
}
"""

MUTATION_STATUT = """
mutation($projet: ID!, $item: ID!, $champ: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(
    input: {projectId: $projet, itemId: $item, fieldId: $champ,
            value: {singleSelectOptionId: $option}}
  ) {
    projectV2Item { id }
  }
}
"""


# ---------------------------------------------------------------------------
# Le client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Accès authentifié aux Issues du dépôt et au Project de `board.yaml`.

    Le client `httpx` est injectable : les tests passent un
    `httpx.MockTransport` et ne touchent jamais GitHub.
    """

    def __init__(
        self,
        token: str,
        config: BoardConfig,
        *,
        client: httpx.Client | None = None,
        api_url: str = API_URL,
    ) -> None:
        if not token:
            raise GitHubAuthError(f"jeton GitHub vide — renseignez {TOKEN_ENV}")
        self._token = token
        self.config = config
        self._api_url = api_url.rstrip("/")
        self._http = client if client is not None else httpx.Client()
        self._possede_http = client is None
        self._projet: Projet | None = None
        #: Scopes annoncés par la dernière réponse, `None` si jamais annoncés.
        self.scopes: tuple[str, ...] | None = None

    @classmethod
    def from_env(
        cls,
        config: BoardConfig,
        *,
        env: Mapping[str, str] | None = None,
        client: httpx.Client | None = None,
    ) -> GitHubClient:
        """Client authentifié par `GITHUB_TOKEN` ; `SecretsError` s'il manque."""
        return cls(require_env(TOKEN_ENV, env), config, client=client)

    def close(self) -> None:
        if self._possede_http:
            self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        # Jamais le jeton : un `repr` finit toujours dans un journal.
        return f"GitHubClient(repository={self.config.repository!r})"

    # -- US-4.1.1 · authentification ----------------------------------------

    def verifier_acces(self) -> Acces:
        """Vérifie que le jeton ouvre le dépôt et que ses Issues sont actives.

        Lecture seule : rien n'est créé. Lève `GitHubAuthError` sur un jeton
        refusé, `GitHubNotFoundError` sur un dépôt invisible, `GitHubError`
        quand les Issues sont désactivées.
        """
        depot = self._appeler("GET", self._chemin_depot(), json=None, rejouable=True).json()
        if not depot.get("has_issues", False):
            raise GitHubError(
                f"les Issues sont désactivées sur « {self.config.repository} » — "
                "à activer dans Settings → General → Features → Issues"
            )
        return Acces(repository=self.config.repository, scopes=self.scopes)

    # -- US-4.1.2 · Issue ----------------------------------------------------

    def creer_issue(self, titre: str, corps: str) -> Issue:
        """Crée une Issue et la rend. **Jamais rejouée sur panne** : voir le module."""
        payload = {"title": _titre_borne(titre), "body": corps}
        reponse = self._appeler(
            "POST", f"{self._chemin_depot()}/issues", json=payload, rejouable=False
        )
        data = reponse.json()
        issue = Issue(
            number=data["number"],
            node_id=data["node_id"],
            url=data["html_url"],
            titre=data.get("title", payload["title"]),
        )
        logger.info("Issue #%d créée : %s", issue.number, issue.titre)
        return issue

    def creer_issue_offre(self, offre: ScoredJob) -> Issue:
        """L'Issue d'une offre classée : « Entreprise — Poste », la fiche en corps."""
        return self.creer_issue(titre_issue(offre), corps_issue(offre))

    def lister_fiches(self, depuis: datetime | None = None) -> dict[str, Issue]:
        """Les Issues du dépôt qui portent une fiche, par identifiant stable.

        Lecture seule, ouvertes et fermées, pull requests exclues. `depuis`
        restreint aux Issues modifiées après cet instant — une Issue créée
        après l'est forcément. Deux Issues pour la même offre : la plus
        ancienne est gardée, et les autres sont signalées dans le journal.
        Sert à reconstruire le registre des fiches (US-4.2.2).
        """
        parametres = f"state=all&sort=created&direction=asc&per_page={PAGE}"
        if depuis is not None:
            parametres += "&since=" + depuis.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        fiches: dict[str, Issue] = {}
        page = 1
        while True:
            chemin = f"{self._chemin_depot()}/issues?{parametres}&page={page}"
            lot = self._appeler("GET", chemin, json=None, rejouable=True).json()
            for data in lot:
                identifiant = lire_id(data.get("body"))
                if "pull_request" in data or identifiant is None:
                    continue
                issue = Issue(
                    number=data["number"],
                    node_id=data["node_id"],
                    url=data.get("html_url", ""),
                    titre=data.get("title", ""),
                )
                deja = fiches.get(identifiant)
                if deja is None or issue.number < deja.number:
                    fiches[identifiant] = issue
                if deja is not None:
                    logger.warning(
                        "offre %s publiée deux fois : Issues #%d et #%d — la plus "
                        "ancienne est gardée",
                        identifiant,
                        min(deja.number, issue.number),
                        max(deja.number, issue.number),
                    )
            if len(lot) < PAGE:
                return fiches
            page += 1

    # -- US-4.1.3 · Project + statut -----------------------------------------

    def projet(self) -> Projet:
        """Le Project de `board.yaml`, lu une fois par client puis gardé.

        Lève `GitHubNotFoundError` si le Project ou son champ de statut
        n'existe pas : ce sont des erreurs de mise en place, et le message
        dit laquelle.
        """
        if self._projet is not None:
            return self._projet

        owner, number = self.config.project_owner, self.config.project_number
        nom = f"Project n°{number} de « {owner} »"
        try:
            data = self._graphql(
                QUERY_PROJET,
                {"owner": owner, "number": number, "champ": self.config.status_field},
            )
        except (GitHubNotFoundError, GitHubPermissionError) as exc:
            raise type(exc)(f"{nom} inaccessible — {exc}{self._conseil_scope()}") from exc

        proprietaire = data.get("repositoryOwner")
        projet = (proprietaire or {}).get("projectV2")
        if not projet:
            raise GitHubNotFoundError(
                f"{nom} introuvable, ou invisible pour ce jeton{self._conseil_scope()}"
            )

        champ = projet.get("field") or {}
        # Un champ du bon nom mais d'un autre type (texte, date) ne répond pas
        # au fragment `ProjectV2SingleSelectField` : il arrive vide.
        if "id" not in champ:
            raise GitHubNotFoundError(
                f"{nom} : pas de champ « sélection unique » nommé "
                f"« {self.config.status_field} » — à créer dans le Project, ou "
                "`status.field` à corriger dans board.yaml"
            )

        self._projet = Projet(
            id=projet["id"],
            titre=projet.get("title", ""),
            url=projet.get("url", ""),
            champ_statut_id=champ["id"],
            options_statut={o["name"]: o["id"] for o in champ.get("options") or []},
        )
        return self._projet

    def ajouter_au_projet(self, issue: Issue, statut: str | None = None) -> ItemProjet:
        """Pose `issue` sur le tableau avec `statut` (défaut : `status.initial`).

        L'option est cherchée **avant** l'ajout : une option absente ne doit
        pas laisser une carte sans colonne sur le tableau.
        """
        statut = self.config.status_initial if statut is None else statut
        projet = self.projet()
        option = projet.option(statut)
        if option is None:
            disponibles = ", ".join(f"« {nom} »" for nom in projet.options_statut) or "aucune"
            raise GitHubNotFoundError(
                f"le champ « {self.config.status_field} » du Project « {projet.titre} » "
                f"n'a pas d'option « {statut} » (options : {disponibles}) — "
                "à ajouter dans les réglages du champ"
            )

        ajout = self._graphql(
            MUTATION_AJOUT, {"projet": projet.id, "contenu": issue.node_id}
        )
        item_id = ajout["addProjectV2ItemById"]["item"]["id"]
        self._graphql(
            MUTATION_STATUT,
            {
                "projet": projet.id,
                "item": item_id,
                "champ": projet.champ_statut_id,
                "option": option,
            },
        )
        logger.info("Issue #%d ajoutée au Project, statut « %s »", issue.number, statut)
        return ItemProjet(id=item_id, statut=statut)

    def items_du_projet(self) -> dict[int, str]:
        """Numéro d'Issue du dépôt → identifiant de sa carte sur le tableau.

        Lecture seule, toutes les pages. Sert à reconstruire le registre des
        fiches (US-4.2.2) sans reposer une carte déjà triée en « Nouveau ».
        """
        projet = self.projet()
        depot = self.config.repository.casefold()
        items: dict[int, str] = {}
        apres: str | None = None
        while True:
            data = self._graphql(QUERY_ITEMS, {"projet": projet.id, "apres": apres})
            page = ((data.get("node") or {}).get("items")) or {}
            for noeud in page.get("nodes") or []:
                contenu = (noeud or {}).get("content") or {}
                nom = ((contenu.get("repository") or {}).get("nameWithOwner") or "").casefold()
                if "number" in contenu and nom == depot:
                    items[contenu["number"]] = noeud["id"]
            infos = page.get("pageInfo") or {}
            if not infos.get("hasNextPage"):
                return items
            apres = infos.get("endCursor")

    # -- transport -----------------------------------------------------------

    def _chemin_depot(self) -> str:
        return f"/repos/{self.config.owner}/{self.config.repo}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }

    def _graphql(self, requete: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Envoie une requête GraphQL et rend `data`, ou lève sur `errors`.

        GraphQL répond 200 même en échec : l'erreur est dans le corps, et un
        client qui ne regarderait que le statut prendrait un refus pour un
        succès. Toutes nos requêtes sont des lectures ou des mutations
        idempotentes : elles sont rejouables.
        """
        reponse = self._appeler(
            "POST",
            "/graphql",
            json={"query": requete, "variables": variables},
            rejouable=True,
        )
        corps = _json(reponse)
        erreurs = corps.get("errors") or []
        if erreurs:
            types = {erreur.get("type") for erreur in erreurs}
            messages = "; ".join(erreur.get("message", "?") for erreur in erreurs)
            if types <= {"NOT_FOUND"}:
                raise GitHubNotFoundError(messages)
            if types & {"FORBIDDEN", "INSUFFICIENT_SCOPES"}:
                raise GitHubPermissionError(messages)
            raise GitHubError(f"GraphQL : {messages}")
        return corps.get("data") or {}

    def _appeler(
        self, methode: str, chemin: str, *, json: Any, rejouable: bool
    ) -> httpx.Response:
        """Émet la requête ; attend les limites de débit ; rejoue les pannes permises."""
        appel = f"{methode} {chemin}"
        attentes_debit = 0
        pannes = 0
        while True:
            try:
                reponse = self._http.request(
                    methode,
                    self._api_url + chemin,
                    headers=self._headers(),
                    json=json,
                    timeout=TIMEOUT,
                )
            except httpx.HTTPError as exc:
                motif = f"{type(exc).__name__} — {exc}"
                if rejouable and pannes < len(RETRY_DELAYS):
                    self._attendre_panne(appel, motif, pannes)
                    pannes += 1
                    continue
                raise GitHubError(
                    f"{appel} : échec réseau — {motif}{_avertir(rejouable)}",
                    peut_avoir_abouti=not rejouable,
                ) from exc

            self._noter_scopes(reponse)

            attente = _attente_debit(reponse)
            if attente is not None:
                if attentes_debit >= MAX_RATE_LIMIT_RETRIES:
                    raise GitHubRateLimitError(
                        f"{appel} : limite de débit GitHub toujours atteinte après "
                        f"{attentes_debit} attente(s)"
                    )
                if attente > ATTENTE_MAX:
                    reprise = datetime.fromtimestamp(_maintenant() + attente, timezone.utc)
                    raise GitHubRateLimitError(
                        f"{appel} : limite de débit GitHub atteinte, levée à "
                        f"{reprise:%H:%M:%S} UTC — attente de {attente:.0f} s, au-delà "
                        f"des {ATTENTE_MAX:.0f} s permises"
                    )
                logger.warning(
                    "%s : limite de débit GitHub, nouvelle tentative dans %.0f s",
                    appel,
                    attente,
                )
                _wait(attente)
                attentes_debit += 1
                continue

            if reponse.status_code in RETRYABLE_STATUS and rejouable and pannes < len(RETRY_DELAYS):
                self._attendre_panne(appel, f"statut HTTP {reponse.status_code}", pannes)
                pannes += 1
                continue

            return self._verifier(appel, reponse, rejouable)

    def _attendre_panne(self, appel: str, motif: str, pannes: int) -> None:
        delai = RETRY_DELAYS[pannes]
        logger.warning("%s : %s — nouvelle tentative dans %.1f s", appel, motif, delai)
        _wait(delai)

    def _noter_scopes(self, reponse: httpx.Response) -> None:
        entete = reponse.headers.get("x-oauth-scopes")
        if entete is not None:
            self.scopes = tuple(s.strip() for s in entete.split(",") if s.strip())

    def _conseil_scope(self) -> str:
        if self.scopes is not None and SCOPE_PROJECT not in self.scopes:
            actuels = ", ".join(self.scopes) or "aucun"
            return (
                f" ; le jeton n'a pas le scope « {SCOPE_PROJECT} » (scopes : {actuels})"
            )
        return ""

    def _verifier(self, appel: str, reponse: httpx.Response, rejouable: bool) -> httpx.Response:
        """Rend la réponse si elle est un succès, lève l'erreur nommée sinon."""
        statut = reponse.status_code
        if statut < 400:
            return reponse

        message = _message(reponse)
        if statut == 401:
            raise GitHubAuthError(
                f"{appel} : jeton GitHub refusé (401) — {TOKEN_ENV} invalide, expiré "
                f"ou révoqué ({message})"
            )
        if statut == 403:
            attendues = reponse.headers.get("x-accepted-github-permissions")
            precision = f" ; permissions attendues : {attendues}" if attendues else ""
            raise GitHubPermissionError(
                f"{appel} : accès refusé (403) — {message}{precision}"
            )
        if statut == 404:
            raise GitHubNotFoundError(
                f"{appel} : introuvable (404), ou invisible pour ce jeton — {message}"
            )
        incertain = statut >= 500 and not rejouable
        suite = _avertir(rejouable) if statut >= 500 else ""
        raise GitHubError(
            f"{appel} : statut HTTP {statut} — {message}{suite}", peut_avoir_abouti=incertain
        )


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------


def _attente_debit(reponse: httpx.Response) -> float | None:
    """Secondes à attendre si la réponse signale une limite de débit, sinon `None`.

    Trois formes, dans l'ordre où GitHub les documente :

    - `retry-after` — limite secondaire (trop d'écritures rapprochées) ;
    - `x-ratelimit-remaining: 0` + `x-ratelimit-reset` — quota horaire épuisé ;
    - un 403/429 qui parle de *rate limit* sans rien dire de plus, ou une
      erreur GraphQL `RATE_LIMITED` servie en 200 : une minute.
    """
    statut = reponse.status_code
    entetes = reponse.headers

    if statut in (403, 429):
        retry_after = entetes.get("retry-after", "").strip()
        if retry_after.isdigit():
            return float(retry_after)
        if entetes.get("x-ratelimit-remaining") == "0":
            return _jusqua_reset(entetes)
        if statut == 429 or "rate limit" in _message(reponse).lower():
            return ATTENTE_SANS_INDICATION
        return None

    if statut == 200 and reponse.request.url.path.endswith("/graphql"):
        erreurs = _json(reponse).get("errors") or []
        if any(erreur.get("type") == "RATE_LIMITED" for erreur in erreurs):
            if entetes.get("x-ratelimit-reset"):
                return _jusqua_reset(entetes)
            return ATTENTE_SANS_INDICATION
    return None


def _jusqua_reset(entetes: httpx.Headers) -> float:
    try:
        reset = float(entetes["x-ratelimit-reset"])
    except (KeyError, ValueError):
        return ATTENTE_SANS_INDICATION
    return max(reset - _maintenant(), 0.0) + MARGE_RESET


def _json(reponse: httpx.Response) -> dict[str, Any]:
    try:
        corps = reponse.json()
    except ValueError:
        return {}
    return corps if isinstance(corps, dict) else {}


def _message(reponse: httpx.Response) -> str:
    """Le `message` que GitHub met dans ses erreurs, ou le début du corps brut."""
    message = _json(reponse).get("message")
    if isinstance(message, str) and message:
        return message
    return reponse.text[:200] or "réponse vide"


def _avertir(rejouable: bool) -> str:
    if rejouable:
        return ""
    return " ; la requête a pu aboutir malgré tout — vérifier avant de relancer"


def _titre_borne(titre: str) -> str:
    """Titre sur une ligne, borné à la limite de GitHub (`TITRE_MAX`)."""
    titre = " ".join(titre.split())
    if len(titre) <= TITRE_MAX:
        return titre
    return titre[: TITRE_MAX - 1].rstrip() + "…"
