"""Point d'entrée du Collector.

Deux cadences, deux workflows, une seule commande :

- `--cadence fast` (défaut) — `collect.yml`, toutes les 15 min, soit 96
  appels par jour et par source ;
- `--cadence slow` — `collect-slow.yml`, toutes les 6 heures, soit 4.

La cadence lente existe pour les plateformes qui plafonnent leurs appels
sous ce que la boucle normale consommerait : aujourd'hui Remotive, qui n'en
autorise que quatre par jour. C'est le **planificateur** qui garantit le
compte, pas un compteur applicatif : le runner Actions est éphémère, il ne
mémoriserait rien d'un run à l'autre. Chaque source appartient donc à
exactement une cadence, et sa cadence se déduit de son adaptateur
(`Adapter.max_calls_per_day`), jamais d'une ligne de configuration qu'on
pourrait perdre de vue.

**Isolation par source (US-2.4.1).** Une source en échec ne fait pas
échouer le run : l'erreur est journalisée, la source marquée en échec, et
les suivantes sont interrogées quand même. Trente-sept sources tournent à
chaque quart d'heure — un board déplacé, une API en panne ou un adaptateur
qui bute sur un cas inédit ne doit jamais coûter la collecte des trente-six
autres. Le run ne sort en erreur que si **toutes** les sources échouent :
là, ce n'est plus une source qui a un problème, c'est nous.

Les offres collectées sont ensuite **normalisées** (Feature 3.1) : les
`RawJob` de dix-huit adaptateurs deviennent des `Job`, le schéma que toute
la suite du pipeline manipule. Puis vient le **filtre de rétention**
(Feature 3.2), la première étape qui jette : sur trois mille cinq cents
offres collectées, quelques dizaines correspondent au poste cherché. Le
bilan du run affiche le décompte par motif de rejet — sans lui, un filtre
cassé qui rejette tout ressemblerait à un marché de l'emploi calme.

`filters.yaml` est lu **avant** la première requête. Une configuration
illisible arrête le run tout de suite plutôt qu'après trente-sept appels
réseau, et surtout : collecter sans savoir quoi retenir n'a aucun intérêt.

Vient ensuite la **déduplication** (Feature 3.3) : le run ne garde que les
offres retenues **jamais vues**, puis les mémorise dans `state/seen.json`,
que le workflow commite. Sans elle, une offre en ligne trois semaines
serait publiée à chaque quart d'heure.

La suite — scoring, publication — arrive avec les Features 3.4 à 4.3.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

import httpx

from src.adapters import RawJob, get_adapter
from src.adapters.base import new_client
from src.adapters.registry import CADENCES, FAST, load_all
from src.core.config import ConfigError, Source, load_filters
from src.core.dedup import SeenStore
from src.core.normalize import Job, normalize
from src.core.retention import RetentionFilter
from src.core.secrets import require_env

logger = logging.getLogger(__name__)

#: Emplacement par défaut des règles de rétention, surchargeable par
#: `--filters` (les tests s'en servent pour ne pas dépendre du fichier du
#: dépôt).
DEFAULT_FILTERS_PATH = "config/filters.yaml"


@dataclass(frozen=True)
class SourceResult:
    """Ce qu'a donné l'interrogation d'**une** source.

    Une source qui répond 404 (board déplacé) n'est pas en échec : elle a
    répondu, elle n'a simplement rien à offrir. Seule une erreur remontée
    par l'adaptateur remplit `error`.
    """

    source: Source
    jobs: list[RawJob] = field(default_factory=list)
    #: Message d'échec, ou `""` quand la source a répondu.
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def empty(self) -> bool:
        """A répondu, mais sans une seule offre.

        Ce n'est pas un échec — un board sans poste ouvert existe — mais
        c'est le trou par lequel une panne passe inaperçue : une API qui
        ferme en répondant « collection vide » compte aujourd'hui comme un
        succès. D'où un état nommé, visible dans le bilan du run.
        """
        return self.ok and not self.jobs


@dataclass(frozen=True)
class CollectReport:
    """Bilan d'un run : ce qui a répondu, ce qui a échoué, ce qu'on a récolté."""

    results: list[SourceResult] = field(default_factory=list)

    @property
    def succeeded(self) -> list[SourceResult]:
        return [result for result in self.results if result.ok]

    @property
    def failed(self) -> list[SourceResult]:
        return [result for result in self.results if not result.ok]

    @property
    def empty(self) -> list[SourceResult]:
        """Les sources qui ont répondu sans rapporter la moindre offre."""
        return [result for result in self.results if result.empty]

    @property
    def jobs(self) -> list[RawJob]:
        """Toutes les offres collectées, dans l'ordre des sources."""
        return [job for result in self.results for job in result.jobs]

    @property
    def all_failed(self) -> bool:
        """Aucune source n'a répondu — alors que le run en avait à interroger.

        C'est le seul cas qui fait sortir le run en erreur : une panne
        générale (réseau du runner, secret expiré, bug transverse) mérite
        d'être vue, là où une source isolée en échec est le quotidien.
        """
        return bool(self.results) and not self.succeeded


def collect_sources(
    sources: list[Source], *, client: httpx.Client | None = None
) -> CollectReport:
    """Interroge chaque source, en isolant les échecs (US-2.4.1).

    Le `client` est partagé par tous les adaptateurs du run : les
    connexions sont réutilisées, et les tests peuvent injecter un
    `httpx.MockTransport` pour rester hors réseau.
    """
    return CollectReport([_collect_one(source, client) for source in sources])


def _collect_one(source: Source, client: httpx.Client | None) -> SourceResult:
    """Interroge une source ; toute erreur devient un résultat en échec.

    On attrape `Exception` et pas seulement `AdapterError` : l'échec
    *attendu* est bien `AdapterError`, mais un adaptateur qui bute sur un
    cas inédit lèvera un `KeyError` ou un `AttributeError`, et ce bug ne
    doit pas emporter les autres sources. Les `BaseException`
    (`KeyboardInterrupt`, `SystemExit`) passent, elles, sans être avalées.
    """
    try:
        adapter = get_adapter(source.ats, client=client)
        jobs = adapter.fetch(source)
    except Exception as exc:  # noqa: BLE001 — isolation par source, cf. docstring
        logger.warning(
            "source « %s » (%s) en échec, le run continue — %s: %s",
            source.nom,
            source.ats,
            type(exc).__name__,
            exc,
        )
        return SourceResult(source=source, error=f"{type(exc).__name__}: {exc}")

    if jobs:
        logger.info(
            "source « %s » (%s) : %d offre(s)", source.nom, source.ats, len(jobs)
        )
    else:
        # Un avertissement, pas un échec : un board sans poste ouvert est
        # légitime. Mais une source qui se tarit — API fermée, endpoint
        # déplacé, filtre devenu vide côté plateforme — répond exactement
        # pareil, et resterait sinon invisible dans un bilan « tout va bien ».
        logger.warning(
            "source « %s » (%s) : 0 offre — board réellement vide, ou source "
            "tarie sans le dire ; à vérifier si ça dure",
            source.nom,
            source.ats,
        )
    return SourceResult(source=source, jobs=jobs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.collect",
        description="Collecte les offres des sources de la cadence demandée.",
    )
    parser.add_argument(
        "--cadence",
        choices=CADENCES,
        default=FAST,
        help=(
            "fast : sources sans plafond d'appels (toutes les 15 min) ; "
            "slow : sources qui limitent leurs appels quotidiens (toutes les 6 h)"
        ),
    )
    parser.add_argument(
        "--filters",
        default=DEFAULT_FILTERS_PATH,
        help="règles de rétention à appliquer (défaut : %(default)s)",
    )
    parser.add_argument(
        "--seen",
        # `None` plutôt que le chemin : il est résolu au chargement, ce qui
        # laisse la suite de tests rediriger `DEFAULT_SEEN_PATH`.
        default=None,
        help="mémoire des offres déjà vues (défaut : state/seen.json)",
    )
    return parser


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    """Collecte la cadence demandée. Retourne 0, ou 1 en cas d'échec.

    Deux échecs possibles, et un seul est une panne : des règles de
    rétention illisibles (avant toute requête), ou **toutes** les sources
    en erreur.

    `client` n'est là que pour les tests : en production, le run ouvre le
    sien et le referme à la fin.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    # httpx journalise chaque requête en INFO : une ligne par source, en
    # double de la nôtre. On garde ses avertissements, pas son bavardage.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    require_env("GITHUB_TOKEN")

    # Avant la première requête : collecter sans savoir quoi retenir n'a
    # aucun intérêt, et une erreur de config vaut mieux découverte tout de
    # suite qu'après trente-sept appels réseau. Le filtre neutre du
    # pré-filtre de slug (US-2.5.1) n'a pas d'équivalent ici : là-bas,
    # laisser passer coûtait quelques requêtes ; ici, cela publierait le
    # marché de l'emploi mondial sur le tableau de revue.
    try:
        filtre = RetentionFilter.from_config(load_filters(args.filters).retention)
    except ConfigError as exc:
        print(f"collect : règles de rétention illisibles — {exc}", file=sys.stderr)
        return 1

    sources = load_all(cadence=args.cadence)
    if not sources:
        # Ce n'est pas une erreur : la cadence lente peut légitimement être
        # vide si aucune plateforme ne plafonne ses appels.
        print(f"collect [{args.cadence}] : aucune source pour cette cadence")
        return 0

    pluriel = "s" if len(sources) > 1 else ""
    # `flush` partout : les journaux partent sur stderr sans tampon, les
    # `print` sur stdout avec — sans ça, le log d'Actions les entrelace
    # dans le désordre et devient illisible.
    print(
        f"collect [{args.cadence}] : {len(sources)} source{pluriel} à interroger",
        flush=True,
    )

    if client is not None:
        report = collect_sources(sources, client=client)
    else:
        with new_client() as owned:
            report = collect_sources(sources, client=owned)

    # Trois états, trois marqueurs de même largeur : le log d'Actions se lit
    # alors d'un coup d'œil, sans avoir à comparer des nombres.
    for result in report.results:
        if not result.ok:
            marqueur, detail = "KO  ", result.error
        elif result.empty:
            marqueur, detail = "vide", "0 offre"
        else:
            marqueur, detail = "ok  ", f"{len(result.jobs)} offre(s)"
        print(
            f"  {marqueur} {result.source.nom} ({result.source.ats}) : {detail}",
            flush=True,
        )

    print(
        f"collect [{args.cadence}] : {len(report.succeeded)} source(s) collectée(s), "
        f"{len(report.failed)} en échec, {len(report.jobs)} offre(s) brutes",
        flush=True,
    )

    if report.empty:
        # Sur une ligne à part, avec les noms : c'est ce qui rend visible une
        # source qui se tarit, là où « 37 collectées, 0 en échec » la cache.
        noms = ", ".join(result.source.nom for result in report.empty)
        print(
            f"collect [{args.cadence}] : {len(report.empty)} source(s) sans "
            f"aucune offre — {noms}",
            flush=True,
        )

    if report.all_failed:
        print(
            f"collect [{args.cadence}] : ÉCHEC — les {len(report.failed)} "
            "source(s) ont toutes échoué, aucune offre collectée",
            file=sys.stderr,
        )
        return 1

    # Le vocabulaire vient du filtre, pas du module de normalisation : c'est
    # ce qui garantit qu'une techno sur laquelle on filtre ne puisse jamais
    # être invisible au détecteur (US-3.2.4).
    jobs = normalize(report.jobs, vocabulaire=filtre.vocabulaire)
    print(
        f"collect [{args.cadence}] : {len(jobs)} offre(s) normalisée(s)"
        f"{_ecartees(report.jobs, jobs)}",
        flush=True,
    )

    retention = filtre.appliquer(jobs)
    print(f"collect [{args.cadence}] : {retention.resume}", flush=True)

    vues = SeenStore.load(args.seen)
    dedup = vues.nouvelles(retention)
    print(f"collect [{args.cadence}] : {dedup.resume}", flush=True)

    # Dernière étape du run, et pas par hasard : une offre n'est marquée vue
    # qu'une fois tout le reste fait. Quand la publication (Feature 4.3)
    # arrivera, elle s'intercalera juste au-dessus, et ne devront être
    # marquées que les offres **réellement publiées** — sinon un échec de
    # l'API GitHub les ferait disparaître pour toujours.
    ajouts = vues.marquer(dedup)
    if ajouts:
        vues.save()
    print(
        f"collect [{args.cadence}] : {ajouts} offre(s) mémorisée(s), "
        f"{len(vues)} au total dans {vues.path}",
        flush=True,
    )
    print("collect: offres nouvelles — scoring et publication à venir (Features 3.4/4.x)")
    return 0


def _ecartees(bruts: list[RawJob], jobs: list[Job]) -> str:
    """Suffixe nommant les offres perdues à la normalisation, ou `""`.

    Une offre écartée ici n'avait ni identifiant ni URL — le seul cas que
    la normalisation refuse. C'est rare et cela vient toujours d'un
    adaptateur : le signaler dans le bilan évite de chercher plus tard
    pourquoi le compte ne tombe pas juste.
    """
    perdues = len(bruts) - len(jobs)
    return f" ({perdues} écartée(s), sans identité)" if perdues else ""


if __name__ == "__main__":
    raise SystemExit(main())
