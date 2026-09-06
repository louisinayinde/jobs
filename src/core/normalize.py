"""Normalisation : `RawJob` → `Job` (Feature 3.1).

Fin de l'ingestion, début du traitement. Les dix-huit adaptateurs
produisent déjà un schéma pivot commun — `RawJob` — mais ce schéma décrit
ce qu'une **plateforme** a répondu. `Job` décrit ce que le **pipeline**
manipule : filtre de rétention, dédup, scoring, publication et génération
de CV ne connaissent que lui.

Trois différences, et chacune a une raison :

1. **`ats` devient `source`.** Le champ ne nomme plus « l'ATS qui a
   répondu » mais « d'où vient cette offre » — un agrégateur n'est pas un
   ATS, et le tableau de revue affiche une provenance, pas une famille
   d'API.
2. **`tech[]` apparaît.** Aucune source ne publie la stack dans un champ
   dédié : elle est **déduite** du titre et de la description contre un
   vocabulaire (voir `TECH_VOCABULARY`). C'est le seul champ de `Job` qui
   n'existe nulle part en amont.
3. **Les valeurs sont garanties, pas espérées.** Un adaptateur *devrait*
   avoir nettoyé le HTML et normalisé la date ; ici on le **vérifie**.
   C'est la dernière porte avant que la donnée ne devienne une fiche, un
   `seen.json` et un CV : elle ne fait pas confiance à l'amont.

**Ce que la normalisation ne fait pas.** Elle ne juge pas l'offre : aucun
titre, aucune localisation, aucune séniorité n'est écartée ici. Trier,
c'est le travail du filtre de rétention (Feature 3.2), et les mélanger
rendrait impossible de dire *pourquoi* une offre a disparu. Elle ne
fabrique pas non plus l'identifiant de dédup `ats:entreprise:job_id`
(US-3.3.1) : elle en conserve les trois ingrédients — `source`,
`entreprise`, `id` — intacts et non vides, ce qui est exactement ce dont
la Feature 3.3 aura besoin.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable, Mapping

from src.adapters.base import (
    HYBRID,
    ONSITE,
    REMOTE,
    UNKNOWN_REMOTE_TYPE,
    RawJob,
    clean_str,
    html_to_text,
    infer_remote_type,
    to_iso_utc,
)

logger = logging.getLogger(__name__)

#: Les seules valeurs que `Job.remote_type` peut prendre. Toute autre —
#: champ absent, valeur exotique d'une source — est rejouée depuis la
#: localisation plutôt que propagée telle quelle.
REMOTE_TYPES = frozenset({REMOTE, HYBRID, ONSITE, UNKNOWN_REMOTE_TYPE})

#: Longueur de l'identifiant de repli dérivé de l'URL (voir `_derive_id`).
DERIVED_ID_LENGTH = 12


@dataclass(frozen=True)
class Job:
    """Une offre, dans le schéma unifié du pipeline (US-3.1.1).

    Tous les champs texte sont des `str` — jamais `None` — et `tech` est un
    tuple, ce qui rend la fiche **hashable** : deux offres identiques
    peuvent aller dans un `set` sans cérémonie, ce dont la dédup
    (Feature 3.3) se servira.

    - `id`          : identifiant natif de l'offre chez sa source. Avec
      `source` et `entreprise`, il compose l'identifiant stable
      `ats:entreprise:job_id` de l'US-3.3.1 — c'est pour ça qu'il reste
      brut ici plutôt que déjà concaténé
    - `source`      : d'où vient l'offre (`greenhouse`, `remoteok`, ...)
    - `entreprise`  : l'employeur, jamais la plateforme
    - `titre`       : intitulé du poste, sur une seule ligne
    - `localisation`: localisation telle qu'affichée par la source
    - `remote_type` : `remote` | `hybrid` | `onsite` | `unknown`
    - `url`         : lien public vers l'offre
    - `date`        : date de publication, ISO-8601 UTC (`""` si inconnue)
    - `description` : texte brut, sans balises HTML
    - `tech`        : technologies détectées, en ordre alphabétique stable
    """

    id: str
    source: str
    entreprise: str
    titre: str
    localisation: str
    remote_type: str
    url: str
    date: str
    description: str
    tech: tuple[str, ...] = ()

    @property
    def est_valide(self) -> bool:
        """L'offre porte-t-elle de quoi être dédupliquée et publiée ?

        Le contrat que `normalize` garantit sur toute offre qu'il retourne,
        écrit une fois ici plutôt que répété dans chaque appelant : une
        identité (`source` + `id`), un lien, et un mode de travail connu du
        vocabulaire. Le titre, l'entreprise ou la date peuvent manquer —
        une source avare ne rend pas l'offre inexploitable, elle la rend
        pauvre, et c'est au filtre de rétention d'en décider.
        """
        return bool(self.id and self.source and self.url) and (
            self.remote_type in REMOTE_TYPES
        )


# ---------------------------------------------------------------------------
# Vocabulaire technique (US-3.1.1 — le champ `tech[]`)
# ---------------------------------------------------------------------------

#: Technologies reconnues : nom canonique → variantes écrites rencontrées.
#:
#: **Règle de casse, et c'est toute la mécanique** : une variante écrite
#: tout en minuscules est cherchée sans tenir compte de la casse
#: (`python` trouve « Python », « PYTHON », « python ») ; une variante qui
#: porte une majuscule est cherchée **à la casse exacte**. Cette seconde
#: forme est réservée aux mots qui existent aussi en anglais courant : sans
#: elle, `go` ferait de « we go to production » une offre Go, et `rest` de
#: « the rest of the team » une offre REST.
#:
#: Le vocabulaire reste volontairement court et vérifiable plutôt
#: qu'exhaustif : il couvre la stack visée par `website.md` (backend
#: remote), et `normalize(..., vocabulaire=...)` permet d'en passer un
#: autre sans toucher à ce module. La Feature 3.2.4 y ajoutera les termes
#: de `retention.tech_include_any`, pour qu'un terme sur lequel on filtre
#: ne puisse jamais être invisible au détecteur.
#:
#: Deux absences assumées : **C** et **R**, dont la forme d'une seule
#: lettre produit plus de faux positifs (« option C », « R. Dupont ») que
#: d'offres réellement pertinentes. `C++` et `C#`, eux, sont sans
#: ambiguïté et présents.
#:
#: **Le reste des ambiguïtés est tranché en faveur du faux positif**, et
#: c'est un choix, pas un renoncement : `tech_include_any` (Feature 3.2.4)
#: est un filtre d'**inclusion**. Une offre taguée `Spark` à tort — « Spark
#: Capital » est un fonds d'investissement, vu sur une offre réelle — se
#: repère d'un coup d'œil sur la fiche ; une offre Spark **non taguée**
#: disparaîtrait du tableau sans que rien ne le dise. Rater coûte plus cher
#: que sur-signaler.
#:
#: **Ce que `tech[]` dit exactement : « mentionnée », pas « requise ».** Un
#: run réel sur 3 492 offres (2026-09-06) le montre sans détour : les 396
#: `Elasticsearch` viennent à 365 des offres d'**Elastic**, dont chaque
#: description se termine par « Elasticsearch develops and distributes… » ;
#: les 175 `Grafana`, à 132 de **Grafana Labs**. Le détecteur ne se trompe
#: pas — le texte le dit —, mais il tague aussi le commercial de Grafana
#: Labs. C'est sans conséquence : ce poste-là est écarté sur son **titre**
#: par le filtre de rétention (Feature 3.2), bien avant que sa stack ne
#: compte. Ce champ éclaire une offre déjà retenue ; il ne la sélectionne
#: pas.
TECH_VOCABULARY: Mapping[str, tuple[str, ...]] = {
    # --- Langages --------------------------------------------------------
    "Python": ("python",),
    "Java": ("java",),
    "JavaScript": ("javascript", "JS"),
    "TypeScript": ("typescript",),
    "Go": ("golang", "Go"),
    "Rust": ("rust",),
    "Kotlin": ("kotlin",),
    "Swift": ("swift",),
    "Ruby": ("ruby",),
    "PHP": ("php",),
    "Scala": ("scala",),
    "Elixir": ("elixir",),
    "C++": ("c++", "cpp"),
    "C#": ("c#", "csharp"),
    "SQL": ("sql",),
    "Bash": ("bash", "shell scripting"),
    # --- Frameworks & runtimes -------------------------------------------
    "Django": ("django",),
    "Flask": ("flask",),
    "FastAPI": ("fastapi", "fast api"),
    "Spring": ("spring boot", "springboot", "Spring"),
    "Rails": ("rails", "ruby on rails"),
    "Laravel": ("laravel",),
    "Symfony": ("symfony",),
    "Node.js": ("node.js", "nodejs"),
    "Express": ("express.js", "expressjs"),
    "NestJS": ("nestjs", "nest.js"),
    "React": ("react", "react.js", "reactjs"),
    "Vue": ("vue.js", "vuejs", "Vue"),
    "Angular": ("angular",),
    "Svelte": ("svelte", "sveltekit"),
    "Next.js": ("next.js", "nextjs"),
    ".NET": (".net", "dotnet", "asp.net"),
    # --- Interfaces ------------------------------------------------------
    "GraphQL": ("graphql",),
    "gRPC": ("grpc",),
    "REST": ("REST", "restful"),
    # --- Données ---------------------------------------------------------
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql",),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Elasticsearch": ("elasticsearch",),
    "Cassandra": ("cassandra",),
    "DynamoDB": ("dynamodb",),
    "Kafka": ("kafka",),
    "RabbitMQ": ("rabbitmq",),
    "Snowflake": ("snowflake",),
    "BigQuery": ("bigquery",),
    "Spark": ("apache spark", "pyspark", "Spark"),
    "Airflow": ("airflow",),
    "dbt": ("dbt",),
    # --- Cloud & infrastructure ------------------------------------------
    "AWS": ("aws", "amazon web services"),
    "GCP": ("gcp", "google cloud"),
    "Azure": ("azure",),
    "Kubernetes": ("kubernetes", "k8s"),
    "Docker": ("docker",),
    "Terraform": ("terraform",),
    "Ansible": ("ansible",),
    "Helm": ("helm",),
    "Linux": ("linux",),
    # `github` / `gitlab` sont volontairement absents : ce sont d'abord des
    # noms d'entreprises — GitLab est la première ligne de `sources.yaml` —
    # et tagger « Git » sur chacune de ses offres n'apprend rien.
    "Git": ("git",),
    "CI/CD": ("ci/cd", "cicd", "continuous integration"),
    "Jenkins": ("jenkins",),
    "Prometheus": ("prometheus",),
    "Grafana": ("grafana",),
}


def _compile_alias(alias: str) -> re.Pattern[str]:
    """Compile une variante en motif borné, à la casse voulue.

    Les bornes ne peuvent pas être des `\\b` : `c++` finit par un caractère
    non-mot, et `\\b` y placerait la frontière au mauvais endroit. On
    exclut donc explicitement, de part et d'autre, ce qui ferait du motif
    un morceau d'un mot plus long — lettres, chiffres, `_`, `+` et `#`.

    Un chiffre est en revanche **autorisé après** le motif : « python3 »
    et « C++11 » nomment bien Python et C++, et les couper serait perdre
    des offres pour rien.
    """
    motif = rf"(?<![A-Za-z0-9_+#]){re.escape(alias)}(?![A-Za-z_+#])"
    # Minuscules seules = mot sans ambiguïté en anglais courant, cherché
    # sans tenir compte de la casse. Une majuscule = mot ambigu, cherché à
    # la casse exacte (voir `TECH_VOCABULARY`).
    drapeaux = 0 if any(c.isupper() for c in alias) else re.IGNORECASE
    return re.compile(motif, drapeaux)


#: Un vocabulaire compilé : nom canonique → motifs prêts à chercher.
Motifs = tuple[tuple[str, tuple[re.Pattern[str], ...]], ...]


@lru_cache(maxsize=8)
def _compile_vocabulary(entrees: tuple[tuple[str, tuple[str, ...]], ...]) -> Motifs:
    """Compile un vocabulaire, une fois par contenu distinct.

    La clé du cache est le **contenu** du vocabulaire, pas l'objet qui le
    porte : un `id()` de mapping serait réattribué à un autre dict après un
    passage du ramasse-miettes, et un run se mettrait alors à chercher les
    technologies de quelqu'un d'autre.
    """
    return tuple(
        (nom, tuple(_compile_alias(alias) for alias in alias_list))
        for nom, alias_list in entrees
    )


def _patterns_for(vocabulaire: Mapping[str, tuple[str, ...]] | None) -> Motifs:
    """Motifs du vocabulaire demandé, compilés au plus une fois.

    Un run normalise quelques milliers d'offres contre soixante motifs :
    les recompiler à chaque offre coûterait plus cher que tout le reste de
    la normalisation réunie.
    """
    if vocabulaire is None or vocabulaire is TECH_VOCABULARY:
        return _DEFAULT_PATTERNS
    return _compile_vocabulary(tuple(sorted(vocabulaire.items())))


#: Vocabulaire par défaut, compilé au chargement du module : c'est celui
#: que tout le pipeline utilise, autant ne jamais repasser par le cache.
_DEFAULT_PATTERNS = _compile_vocabulary(tuple(sorted(TECH_VOCABULARY.items())))


def extract_tech(
    *textes: str, vocabulaire: Mapping[str, tuple[str, ...]] | None = None
) -> tuple[str, ...]:
    """Technologies citées dans `textes`, dédupliquées et triées.

    Le tri alphabétique n'est pas cosmétique : `tech[]` finira sur une
    fiche du tableau et dans un `seen.json` versionné. Un ordre qui
    dépendrait de la place du mot dans la description ferait bouger le
    diff Git à chaque reformulation de l'annonce.
    """
    blob = "\n".join(texte for texte in textes if texte)
    if not blob:
        return ()
    trouves = {
        nom
        for nom, motifs in _patterns_for(vocabulaire)
        if any(motif.search(blob) for motif in motifs)
    }
    return tuple(sorted(trouves, key=str.lower))


# ---------------------------------------------------------------------------
# Nettoyage des champs
# ---------------------------------------------------------------------------

#: Signature d'un balisage résiduel : une balise ouvrante ou fermante, la
#: même échappée (`&lt;p&gt;`, tel que Greenhouse la sert), ou une entité
#: nommée / numérique restée en place.
_MARKUP_RE = re.compile(r"</?[A-Za-z!?]|&lt;/?[A-Za-z!?]|&[A-Za-z][A-Za-z0-9]{1,31};|&#\d+;")

#: Une vraie balise, pour la vérification d'après-nettoyage. Distinguer les
#: deux importe : un `<` isolé (« latency < 10 ms ») est du texte légitime,
#: pas du HTML, et le confondre avec une balise ferait perdre le caractère.
_TAG_RE = re.compile(r"</?[A-Za-z!?][^>]*>")

_SPACES_RE = re.compile(r"\s+")


def strip_markup(raw: str) -> str:
    """Retourne `raw` en texte brut, en ne le retouchant qu'au besoin.

    Les adaptateurs nettoient déjà leurs descriptions ; cette fonction est
    le filet, pas le tamis principal. D'où la condition : on ne repasse
    `html_to_text` que si le texte porte encore une signature de balisage.

    Repasser systématiquement serait un bug discret — `html_to_text`
    s'appuie sur `HTMLParser`, qui mange ce qu'il prend pour une balise. Un
    « scale to <5 ms p99 » déjà propre y perdrait la fin de sa phrase à
    chaque tour de pipeline.
    """
    texte = clean_str(raw)
    if not texte or not _MARKUP_RE.search(texte):
        return texte
    return html_to_text(texte)


def _flatten(raw: str) -> str:
    """Aplatit un champ court sur une seule ligne, espaces normalisés.

    Les flux RSS indentent leurs `<title>` et leurs `<company>` sur
    plusieurs lignes ; un titre à rallonge casserait le tableau de revue et
    ferait échouer une comparaison exacte dans le filtre de rétention pour
    une raison invisible à l'œil.
    """
    return _SPACES_RE.sub(" ", clean_str(raw)).strip()


def _derive_id(url: str) -> str:
    """Identifiant de repli, dérivé de l'URL de l'offre.

    Une source qui ne publie pas d'identifiant natif n'a pas pour autant
    des offres interchangeables : leur URL, elle, les distingue. Le hash
    est **déterministe** — même URL, même identifiant, d'un run à l'autre
    et d'une machine à l'autre — sans quoi la dédup (Feature 3.3)
    republierait l'offre à chaque passage.

    L'URL entière ferait aussi bien office d'identifiant, mais donnerait
    une clé de dédup de deux cents caractères contenant `:` et `/` : le
    format `ats:entreprise:job_id` de l'US-3.3.1 y deviendrait illisible.
    """
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:DERIVED_ID_LENGTH]


# ---------------------------------------------------------------------------
# US-3.1.2 — la conversion
# ---------------------------------------------------------------------------


def normalize_one(
    raw: RawJob, *, vocabulaire: Mapping[str, tuple[str, ...]] | None = None
) -> Job | None:
    """Convertit une offre brute en `Job`, ou `None` si elle n'a pas d'identité.

    Aucun champ manquant ne fait échouer la conversion : chacun a un défaut
    sûr (`""` pour un texte, `unknown` pour le mode de travail, `()` pour
    la stack). Le seul cas qui n'en a pas est celui d'une offre **sans
    `id` ni `url`** : sans identifiant natif, et sans lien d'où en dériver
    un, elle ne peut être ni dédupliquée, ni publiée, ni postulée. La
    retenir la ferait republier à chaque run comme une offre neuve — elle
    est donc écartée et journalisée, comme l'est une offre sans employeur
    côté agrégateur (US-2.3.0).
    """
    identifiant = _flatten(raw.id)
    url = _flatten(raw.url)
    source = _flatten(raw.ats)

    if not identifiant and not url:
        logger.warning(
            "offre écartée à la normalisation : ni « id » ni « url » "
            "(source « %s », titre « %s »)",
            source or "inconnue",
            _flatten(raw.titre) or "sans titre",
        )
        return None

    if not identifiant:
        identifiant = _derive_id(url)
        logger.warning(
            "source « %s » : offre sans identifiant natif, identité dérivée "
            "de l'URL (%s → %s)",
            source or "inconnue",
            url,
            identifiant,
        )

    titre = _flatten(raw.titre)
    localisation = _flatten(raw.localisation)
    description = strip_markup(raw.description)

    # Une valeur hors vocabulaire vaut une valeur absente : on la rejoue
    # depuis la localisation plutôt que de la propager. Sans quoi un
    # `remote_type` exotique traverserait le filtre de localisation
    # (Feature 3.2.3) sans jamais correspondre à rien.
    remote_type = clean_str(raw.remote_type)
    if remote_type not in REMOTE_TYPES or remote_type == UNKNOWN_REMOTE_TYPE:
        remote_type = infer_remote_type(localisation)

    return Job(
        id=identifiant,
        source=source,
        entreprise=_flatten(raw.entreprise),
        titre=titre,
        localisation=localisation,
        remote_type=remote_type,
        url=url,
        # Date rejouée même quand l'adaptateur l'a déjà normalisée :
        # `to_iso_utc` est idempotente sur sa propre sortie, et c'est ce qui
        # garantit un format unique quelle que soit la source (US-3.1.T).
        date=to_iso_utc(raw.date),
        description=description,
        # Le titre porte souvent la stack (« Senior Python Engineer ») et
        # pèse peu face à une description de 4 000 signes : les deux sont
        # lus, sans pondération — `tech[]` dit « mentionnée », pas
        # « centrale ».
        tech=extract_tech(titre, description, vocabulaire=vocabulaire),
    )


def normalize(
    raws: Iterable[RawJob], *, vocabulaire: Mapping[str, tuple[str, ...]] | None = None
) -> list[Job]:
    """Convertit toutes les offres brutes d'un run, dans l'ordre reçu.

    L'ordre est conservé : c'est celui des sources telles que le registre
    les a chargées, et le scoring (Feature 3.4) s'appuiera dessus pour
    départager deux offres à égalité de façon reproductible.
    """
    jobs = [normalize_one(raw, vocabulaire=vocabulaire) for raw in raws]
    return [job for job in jobs if job is not None]
