"""Adaptateur générique `JobPosting` : sitemap + schema.org (Feature 2.5).

C'est le **troisième mécanisme d'accès** du projet, après l'API JSON des
ATS (Feature 2.2) et les API/flux des agrégateurs (Feature 2.3), et le plus
rentable en couverture — pour une raison qui n'a rien d'un hasard : toute
source qui veut apparaître dans **Google for Jobs** est tenue de publier un
bloc JSON-LD `JobPosting` sur chaque page d'offre et de lister ces pages
dans son `sitemap.xml`. Ces deux formats sont standards, stables et faits
pour les machines. On lit ce que le site publie pour être lu.

**Ce qui distingue cet adaptateur d'un scraper, qu'on refuse** : il ne
connaît la structure HTML d'aucun site. Il lit un sitemap, il lit du
JSON-LD ; brancher une source de plus, c'est déclarer trois lignes
(`ats`, `site_url`, `sitemap_url`). Un scraper, lui, demanderait un
parseur HTML par site, à refaire à chaque refonte de leur front — c'est le
coût de maintenance que `docs/sources-audit.md` refuse explicitement.

Trois précautions gouvernent le crawl, et elles ne sont pas décoratives :

1. **`robots.txt` d'abord** (US-2.5.3, `robots.py`). Un `Disallow: /`
   désactive la source, sans une seule requête vers ses pages. Un
   `robots.txt` injoignable met la source en échec plutôt qu'en
   libre-service : on ne crawle pas sans avoir pu lire les règles.
2. **Incrémental** (US-2.5.0, `sitemap.py` + `core/state.py`). Le
   `<lastmod>` du sitemap dit ce qui a bougé depuis le dernier passage —
   quelques dizaines de pages au lieu d'un catalogue entier.
3. **Filtrage avant chargement** (US-2.5.1). Le slug de l'URL contient le
   titre du poste : une offre hors cible est écartée **sans** que sa page
   soit demandée. Sur Japan Dev, les 290 offres du sitemap tombent à 67
   pages candidates ; le reste ne coûte pas une requête.

S'y ajoute le plafond de pages par run et le délai entre deux requêtes :
un crawl doit avoir une durée et un volume connus d'avance.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar

import httpx

from src.adapters import base
from src.adapters.base import (
    REMOTE,
    USER_AGENT,
    AdapterError,
    AggregatorAdapter,
    RawJob,
    clean_str,
    html_to_text,
    infer_remote_type,
    origin_of,
    to_iso_utc,
)
from src.adapters.robots import NO_ROBOTS, RobotsRules, parse_robots
from src.adapters.sitemap import Sitemap, SitemapUrl, parse_sitemap, select_new
from src.core.config import ConfigError, Source, load_filters
from src.core.state import CrawlState

logger = logging.getLogger(__name__)

#: En-têtes d'un crawl. **On s'identifie**, et c'est le seul endroit du
#: projet où ce choix se discute.
#:
#: Les agrégateurs de la Feature 2.3 reçoivent des en-têtes de navigateur,
#: pour une raison précise : ils filtrent le User-Agent « JobRadar » à tort,
#: et leurs endpoints sont publiés *pour* être consommés. Un crawl, non. Il
#: charge des pages qu'un site n'a pas exposées comme une API, et se
#: présenter en Chrome reviendrait à empêcher cet hôte de nous voir dans ses
#: journaux, de nous limiter nommément, ou de nous exclure par un
#: `User-agent: JobRadar` — un droit que `robots.py` respecte déjà, et qui
#: serait décoratif si on ne se nommait pas.
#:
#: Ce n'est pas un pari : Japan Dev sert `robots.txt`, ses deux sitemaps et
#: ses pages d'offres en 200 à ce User-Agent (vérifié le 2026-09-06). Une
#: source qui, elle, le filtrerait ne serait pas atteignable par crawl —
#: elle serait à écarter, comme celles de `docs/sources-audit.md`.
#:
#: Pas de `Referer` non plus : on ne vient d'aucune page, l'inventer serait
#: le seul mensonge que ces en-têtes contiendraient.
CRAWL_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

#: Blocs `<script type="application/ld+json">` d'une page.
_JSON_LD_RE = re.compile(
    r"<script[^>]+type\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)

#: `employmentType` de schema.org → libellé lisible.
#:
#: `RawJob` n'a pas de champ « type de contrat », et lui en ajouter un
#: changerait le schéma pivot de dix-sept adaptateurs pour le bénéfice
#: d'un seul mécanisme. L'information est donc portée par la **première
#: ligne de la description**, où elle reste lisible par le filtre de
#: rétention (Feature 3.2) comme par le générateur de CV (Epic 6). Le code
#: schema.org est conservé à côté du libellé : `INTERN` doit rester
#: reconnaissable par un filtre de séniorité écrit en anglais.
_EMPLOYMENT_LABELS = {
    "FULL_TIME": "temps plein",
    "PART_TIME": "temps partiel",
    "CONTRACTOR": "prestation",
    "TEMPORARY": "mission temporaire",
    "INTERN": "stage",
    "VOLUNTEER": "bénévolat",
    "PER_DIEM": "vacation",
    "OTHER": "autre",
}

#: `jobLocationType` de schema.org : une seule valeur existe, et elle veut
#: dire « télétravail ».
_TELECOMMUTE = "TELECOMMUTE"

#: Fenêtre reprise au tout premier run, en jours.
#:
#: Sans curseur, « tout ce qui est postérieur au dernier run » désignerait
#: le catalogue entier — 240 000 pages chez Welcome to the Jungle. On
#: repart donc d'une semaine en arrière : assez pour ne rien manquer
#: d'utile, assez peu pour que le premier run reste borné.
INITIAL_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# US-2.5.2 — extraction du bloc schema.org `JobPosting`
# ---------------------------------------------------------------------------


def extract_job_posting(html: str) -> dict[str, Any] | None:
    """Retourne le bloc JSON-LD `JobPosting` de la page, ou `None`.

    Une page en contient rarement un seul : celle de Japan Dev en publie
    trois — `WebSite`, `Organization`, puis `JobPosting`. On parcourt donc
    tous les blocs et on retient **celui dont le `@type` est
    `JobPosting`**, jamais le premier venu.

    Les trois emballages rencontrés dans la nature sont dépliés : un objet
    seul, un tableau d'objets, et le `@graph` que produisent la plupart des
    extensions SEO. Un bloc au JSON cassé est sauté sans bruit — il ne
    disqualifie pas les autres blocs de la même page.
    """
    for brut in _JSON_LD_RE.findall(html):
        try:
            charge = json.loads(brut.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        trouve = _find_job_posting(charge)
        if trouve is not None:
            return trouve
    return None


def _find_job_posting(node: Any) -> dict[str, Any] | None:
    """Cherche un nœud `@type: JobPosting`, en dépliant listes et `@graph`."""
    if isinstance(node, list):
        for element in node:
            trouve = _find_job_posting(element)
            if trouve is not None:
                return trouve
        return None

    if not isinstance(node, dict):
        return None

    types = node.get("@type")
    types = types if isinstance(types, list) else [types]
    if any(isinstance(t, str) and t.lower() == "jobposting" for t in types):
        return node

    return _find_job_posting(node.get("@graph"))


def organization_name(value: Any) -> str:
    """Nom de `hiringOrganization`, qu'il soit objet ou simple chaîne."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return clean_str(value.get("name"))
    if isinstance(value, list):
        for element in value:
            nom = organization_name(element)
            if nom:
                return nom
    return ""


def job_location(value: Any) -> str:
    """Aplatit `jobLocation` en un libellé lisible, sans répétition.

    schema.org emboîte `Place` → `address` → `PostalAddress`, et accepte
    plusieurs lieux. Japan Dev publie `Tokyo / Tokyo / JP` : rendre
    « Tokyo, Tokyo, JP » serait fidèle et illisible, on déduplique.
    """
    morceaux: list[str] = []

    def collecter(noeud: Any) -> None:
        if isinstance(noeud, list):
            for element in noeud:
                collecter(element)
            return
        if isinstance(noeud, str):
            _ajouter(morceaux, noeud)
            return
        if not isinstance(noeud, dict):
            return
        if "address" in noeud:
            collecter(noeud["address"])
            return
        for cle in ("addressLocality", "addressRegion", "addressCountry", "name"):
            _ajouter(morceaux, noeud.get(cle))

    collecter(value)
    return ", ".join(morceaux)


def _ajouter(morceaux: list[str], valeur: Any) -> None:
    texte = clean_str(valeur)
    if texte and texte not in morceaux:
        morceaux.append(texte)


def describe_employment(value: Any) -> str:
    """Première ligne de description portant le `employmentType`, ou `""`."""
    code = clean_str(value).upper().replace("-", "_").replace(" ", "_")
    if not code:
        return ""
    libelle = _EMPLOYMENT_LABELS.get(code)
    return f"Type de contrat : {libelle} ({code})" if libelle else f"Type de contrat : {code}"


# ---------------------------------------------------------------------------
# US-2.5.1 — pré-filtrage sur le slug, avant toute requête
# ---------------------------------------------------------------------------

_SLUG_SEPARATORS = re.compile(r"[-_+]+")


def slug_text(url: str) -> str:
    """Le slug de `url`, rendu comparable à un titre de poste.

    `/jobs/alpaca/alpaca-senior-software-engineer---clearing-qb5uw2` donne
    `alpaca senior software engineer clearing qb5uw2`. Le suffixe aléatoire
    est laissé : il ne peut que faire échouer une correspondance, jamais en
    créer une à tort.
    """
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return _SLUG_SEPARATORS.sub(" ", slug).strip().lower()


@dataclass(frozen=True)
class SlugFilter:
    """Décide, **sur la seule URL**, si une page mérite d'être chargée.

    Les termes sont ceux du filtre de rétention (`filters.yaml`), et c'est
    volontaire : dupliquer la liste des postes visés dans une seconde
    section de configuration garantirait qu'un jour les deux divergent.

    C'est un pré-filtre, pas le filtre de rétention. Un slug est une
    version abîmée du titre — abréviations, troncature, suffixe aléatoire —
    donc la décision est prise **au bénéfice du doute** : `title_include`
    vide laisse tout passer, et une offre retenue ici sera de toute façon
    rejugée sur son vrai titre par la Feature 3.2. Ce qu'on gagne, c'est de
    ne pas charger les 223 pages de Japan Dev qui n'ont visiblement rien à
    voir avec le poste cherché.
    """

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()

    @classmethod
    def from_filters(cls, path: str | Path = "config/filters.yaml") -> SlugFilter:
        """Construit le filtre depuis `filters.yaml`.

        Un fichier absent ou invalide ne fait pas échouer la collecte : le
        filtre devient neutre et l'avertissement est journalisé. Le crawl
        reste borné par le plafond de pages par run.
        """
        try:
            filtres = load_filters(path)
        except ConfigError as exc:
            logger.warning(
                "pré-filtre de slug neutralisé, %s illisible — %s", path, exc
            )
            return cls()
        retention = filtres.retention
        return cls(
            include=tuple(t.lower() for t in retention.title_include),
            exclude=tuple(t.lower() for t in retention.title_exclude),
        )

    def matches(self, url: str) -> bool:
        """La page vaut-elle une requête ?"""
        texte = slug_text(url)
        if any(terme in texte for terme in self.exclude):
            return False
        if not self.include:
            return True
        return any(terme in texte for terme in self.include)


# ---------------------------------------------------------------------------
# L'adaptateur
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    """État mutable d'un `fetch`, remis à zéro à chaque appel.

    Un adaptateur est instancié par source et par run (`get_adapter`), mais
    on ne s'y fie pas : tout ce qui bouge vit ici et repart de zéro.
    """

    rules: RobotsRules = NO_ROBOTS
    #: URLs du sitemap dépourvues de `<lastmod>` — le repli s'appuie dessus.
    sans_lastmod: set[str] = field(default_factory=set)
    #: URLs dont la page a réellement été demandée pendant ce run.
    chargees: set[str] = field(default_factory=set)
    #: Curseur à enregistrer si le run va au bout.
    cursor: str = ""
    #: Une requête de page a-t-elle déjà eu lieu ? (délai entre deux, pas avant.)
    premiere_page: bool = True


class JobPostingAdapter(AggregatorAdapter):
    """Base des sources lues par sitemap + schema.org `JobPosting`.

    Brancher une source : sous-classer en déclarant `ats`, `site_url`,
    `sitemap_url` et le motif d'URL d'offre. Rien d'autre — c'est la
    promesse « un seul adaptateur couvre N sites ».
    """

    headers: ClassVar[dict[str, str]] = CRAWL_HEADERS

    #: Adresse du sitemap racine. Vide → celui déclaré par `robots.txt`.
    sitemap_url: ClassVar[str] = ""

    #: Motif que doit vérifier l'URL d'une **page d'offre**. Un sitemap liste
    #: aussi l'accueil, le blog et les pages entreprise : les charger pour y
    #: chercher un `JobPosting` absent serait autant de requêtes gâchées.
    job_url_pattern: ClassVar[str] = r"/jobs?/[^/]+/[^/]+$"

    #: Pages chargées au maximum par run. Borne la durée et le volume du
    #: crawl ; ce qui dépasse est repris au run suivant, sans être enjambé.
    max_pages_per_run: ClassVar[int] = 30

    #: Sitemaps enfants suivis au maximum par run (un index en déclare 24
    #: chez Welcome to the Jungle).
    max_sitemaps_per_run: ClassVar[int] = 25

    #: Un crawl est plus coûteux pour l'hôte qu'un appel d'API : trois
    #: requêtes de repérage par run au minimum, sur un site qui n'a rien
    #: demandé. Ces sources sont donc rangées d'office dans la **cadence
    #: réduite** (4 runs par jour au lieu de 96) — voir `registry.py`.
    max_calls_per_day: ClassVar[int | None] = 4

    def __init__(
        self,
        client: httpx.Client | None = None,
        timeout: float | None = None,
        *,
        state: CrawlState | None = None,
        slug_filter: SlugFilter | None = None,
    ) -> None:
        super().__init__(client, timeout)
        self._state = CrawlState.load() if state is None else state
        self._slug_filter = (
            SlugFilter.from_filters() if slug_filter is None else slug_filter
        )
        self._run = _Run()

    def _request_headers(self, url: str) -> dict[str, str]:
        """Les en-têtes de crawl, sans le `Referer` qu'ajoute un agrégateur."""
        return dict(self.headers)

    # -- orchestration ------------------------------------------------------

    def fetch(self, source: Source) -> list[RawJob]:  # type: ignore[override]
        """Crawle la source, puis n'enregistre l'avancement qu'en cas de succès.

        L'ordre compte : `AggregatorAdapter.fetch` peut lever une
        `AdapterError` (sitemap illisible, page en erreur serveur). Le
        curseur n'est alors **pas** avancé, et le run suivant reprend au
        même point plutôt que d'enjamber ce qu'il n'a pas su lire.
        """
        self._run = _Run()
        jobs = super().fetch(source)
        self._save_progress(source)
        return jobs

    def _save_progress(self, source: Source) -> None:
        connues = self._state.known_urls(source.ats) | self._run.chargees
        # Intersection avec le sitemap du jour : une offre retirée du
        # catalogue sort aussi de l'état, qui ne grossit donc pas
        # indéfiniment. Une URL n'y entre que si sa page a été demandée —
        # jamais une URL simplement vue, qu'on n'aurait alors plus jamais
        # chargée.
        self._state.record(
            source.ats,
            cursor=self._run.cursor,
            urls=connues & self._run.sans_lastmod,
        )
        self._state.save()

    # -- US-2.5.3 : robots.txt ---------------------------------------------

    def _read_robots(self, source: Source) -> RobotsRules:
        """Lit `robots.txt` avant tout, et s'y tient.

        Absent (**404**) → tout est autorisé, c'est le comportement
        standard. Injoignable ou en erreur → `AdapterError` remontée par
        `_request` : la source passe en échec, le run continue sans elle
        (US-2.4.1). On ne crawle jamais un site dont on n'a pas pu lire les
        règles.
        """
        racine = origin_of(self.site_url or self.sitemap_url)
        texte = self._request_text(source, f"{racine}robots.txt")
        return NO_ROBOTS if texte is None else parse_robots(texte)

    # -- US-2.5.0 : sitemap incrémental ------------------------------------

    def _read_sitemap(self, source: Source, rules: RobotsRules) -> list[SitemapUrl]:
        """Lit le sitemap racine et ses enfants, et rend toutes les URLs.

        Suit les index (`<sitemapindex>`), décompresse le `.gz` et plafonne
        le nombre d'enfants suivis par run.
        """
        racine = self.sitemap_url or _first_allowed(rules)
        if not racine:
            logger.warning(
                "source « %s » (%s) : aucun sitemap déclaré, ni par l'adaptateur "
                "ni par robots.txt — rien à crawler",
                source.nom,
                self.ats,
            )
            return []

        a_lire = [racine]
        vus: set[str] = set()
        urls: list[SitemapUrl] = []

        while a_lire and len(vus) < self.max_sitemaps_per_run:
            adresse = a_lire.pop(0)
            if adresse in vus:
                continue
            vus.add(adresse)

            document = self._fetch_sitemap(source, adresse)
            if document is None:
                continue
            urls.extend(document.urls)
            a_lire.extend(enfant for enfant in document.children if enfant not in vus)

        if a_lire:
            logger.info(
                "source « %s » (%s) : %d sitemap(s) laissés au prochain run "
                "(plafond de %d par run)",
                source.nom,
                self.ats,
                len(a_lire),
                self.max_sitemaps_per_run,
            )
        return urls

    def _fetch_sitemap(self, source: Source, url: str) -> Sitemap | None:
        response = self._request(source, url)
        if response is None:
            return None
        return parse_sitemap(source, self.ats, response.content)

    def _entries(self, source: Source) -> list[SitemapUrl]:
        """Sélectionne les pages à charger, sans en charger aucune.

        L'enchaînement des filtres est l'endroit où se joue la politesse :
        chaque étape retire des URLs **avant** qu'une requête ne parte.
        """
        rules = self._read_robots(source)
        self._run.rules = rules

        if rules.blocks_everything:
            # Refus explicite du site. Aucune requête vers ses pages : on ne
            # discute pas un `Disallow: /`, on désactive la source.
            logger.warning(
                "source « %s » (%s) : robots.txt interdit tout le site "
                "(Disallow: /) — source désactivée, aucune page chargée",
                source.nom,
                self.ats,
            )
            return []

        toutes = self._read_sitemap(source, rules)
        self._run.sans_lastmod = {url.loc for url in toutes if not url.lastmod}

        curseur = self._state.cursor(source.ats) or _initial_cursor()
        self._run.cursor = curseur

        nouvelles = select_new(
            toutes, cursor=curseur, known=self._state.known_urls(source.ats)
        )

        motif = re.compile(self.job_url_pattern)
        candidates = [
            url
            for url in nouvelles
            # `robots.txt` d'abord : un chemin interdit ne se charge pas,
            # même s'il figure au sitemap.
            if rules.allows(url.loc)
            and motif.search(url.loc)
            # US-2.5.1 : le slug porte le titre du poste, on tranche ici.
            and self._slug_filter.matches(url.loc)
        ]

        retenues = candidates[: self.max_pages_per_run]
        self._run.cursor = _advance(curseur, nouvelles, candidates, retenues)

        logger.info(
            "source « %s » (%s) : %d URL(s) au sitemap, %d nouvelle(s), "
            "%d page(s) à charger",
            source.nom,
            self.ats,
            len(toutes),
            len(nouvelles),
            len(retenues),
        )
        return retenues

    # -- US-2.5.2 : une page → un RawJob -----------------------------------

    def _parse(self, source: Source, entry: SitemapUrl) -> RawJob | None:
        """Charge la page et en extrait le `JobPosting`, ou l'ignore.

        Le délai entre deux requêtes est appliqué ici, juste avant la
        seconde et les suivantes : c'est le seul endroit où l'adaptateur
        frappe des pages du site.
        """
        if not self._run.premiere_page:
            # Passe par le module et non par un nom importé : c'est ce qui
            # permet à `tests/conftest.py` de neutraliser l'attente pour
            # toute la suite, comme il le fait déjà pour le backoff.
            base._wait(self._run.rules.delay)
        self._run.premiere_page = False
        self._run.chargees.add(entry.loc)

        html = self._request_text(source, entry.loc)
        if html is None:
            return None

        posting = extract_job_posting(html)
        if posting is None:
            # Une page peut avoir perdu son bloc, ou n'être pas une offre du
            # tout. Ce n'est pas une erreur : on la journalise et on passe à
            # la suivante. Elle reste marquée « chargée », donc elle ne sera
            # pas redemandée à chaque run.
            logger.warning(
                "source « %s » (%s) : aucun bloc JobPosting sur %s — page ignorée",
                source.nom,
                self.ats,
                entry.loc,
            )
            return None

        return self._to_rawjob(posting, entry)

    def _to_rawjob(self, posting: dict[str, Any], entry: SitemapUrl) -> RawJob:
        url = clean_str(posting.get("url")) or entry.loc
        titre = clean_str(posting.get("title"))
        localisation = job_location(posting.get("jobLocation"))

        corps = html_to_text(clean_str(posting.get("description")))
        contrat = describe_employment(posting.get("employmentType"))
        description = f"{contrat}\n\n{corps}".strip() if contrat else corps

        return RawJob(
            id=_identifier(posting) or _slug_id(url),
            ats=self.ats,
            # Un agrégateur lit l'employeur dans l'offre. Absent ici →
            # `AggregatorAdapter.fetch` écarte l'offre (règle anti-scam).
            entreprise=organization_name(posting.get("hiringOrganization")),
            titre=titre,
            localisation=localisation,
            remote_type=_remote_type(posting, localisation, titre),
            url=url,
            date=to_iso_utc(posting.get("datePosted")),
            description=description,
        )


def _remote_type(posting: dict[str, Any], localisation: str, titre: str) -> str:
    """`jobLocationType` fait foi ; sinon on devine sur le texte."""
    if clean_str(posting.get("jobLocationType")).upper() == _TELECOMMUTE:
        return REMOTE
    return infer_remote_type(localisation, titre)


def _identifier(posting: dict[str, Any]) -> str:
    """`identifier` de schema.org : chaîne, nombre ou `PropertyValue`."""
    valeur = posting.get("identifier")
    if isinstance(valeur, dict):
        valeur = valeur.get("value")
    if isinstance(valeur, (int, float)) and not isinstance(valeur, bool):
        return str(valeur)
    return clean_str(valeur)


def _slug_id(url: str) -> str:
    """Repli d'identifiant : le slug de l'URL, stable tant que l'offre existe."""
    return url.rstrip("/").rsplit("/", 1)[-1]


def _first_allowed(rules: RobotsRules) -> str:
    """Premier sitemap déclaré par `robots.txt`, quand l'adaptateur n'en fixe pas."""
    return rules.sitemaps[0] if rules.sitemaps else ""


def _initial_cursor() -> str:
    """Curseur du tout premier run : il y a `INITIAL_WINDOW_DAYS` jours."""
    depart = datetime.now(timezone.utc) - timedelta(days=INITIAL_WINDOW_DAYS)
    return depart.replace(microsecond=0).isoformat()


def _advance(
    curseur: str,
    nouvelles: list[SitemapUrl],
    candidates: list[SitemapUrl],
    retenues: list[SitemapUrl],
) -> str:
    """Jusqu'où le run a réellement traité le sitemap.

    Deux cas, et le second est celui qui évite de perdre des offres :

    - **rien n'a été coupé** par le plafond de pages : tout ce qui était
      postérieur au curseur a été examiné, le curseur saute donc à la date
      la plus récente vue — y compris celle d'URLs écartées par le
      pré-filtre, qu'il est inutile de réexaminer ;
    - **le plafond a coupé** : le run s'est arrêté à la dernière page
      chargée. Le curseur s'arrête là aussi, et le reste est repris au run
      suivant. Le faire avancer jusqu'au bout enjamberait en silence les
      offres qu'on n'a pas eu le temps de charger.
    """
    if len(candidates) <= len(retenues):
        return max((url.lastmod for url in nouvelles), default=curseur) or curseur
    return retenues[-1].lastmod if retenues and retenues[-1].lastmod else curseur


# ---------------------------------------------------------------------------
# Sources branchées
# ---------------------------------------------------------------------------


class JapanDevAdapter(JobPostingAdapter):
    """Japan Dev — postes anglophones au Japon, priorité géo ② (US-2.5.5).

    ~290 offres, un `JobPosting` complet par page, et un `robots.txt` qui
    n'interdit que deux PDF — les offres sont autorisées.

    C'est aussi le cas d'école du **repli sans `<lastmod>`** : son sitemap
    n'en publie aucun (vérifié le 2026-09-06 sur les 1 449 URLs). L'état
    mémorise donc les URLs déjà chargées, et l'incrémental se repère sur
    l'identité de l'URL plutôt que sur une date.
    """

    ats = "japandev"
    site_url = "https://japan-dev.com/"
    sitemap_url = "https://japan-dev.com/sitemap.xml"
    job_url_pattern = r"/jobs/[^/]+/[^/]+$"
