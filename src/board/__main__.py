"""`python -m src.board` — les commandes du tableau de revue.

`check` — vérifier la mise en place du tableau, sans rien écrire sur GitHub.
Trois contrôles, dans l'ordre où une mise en place échoue d'habitude :

1. le jeton `GITHUB_TOKEN` est accepté et ouvre le dépôt, Issues actives ;
2. le Project de `board.yaml` est visible pour ce jeton ;
3. son champ de statut a bien l'option initiale (« Nouveau »).

Rien n'est créé : c'est la commande à lancer après avoir créé le jeton et le
Project, avant de laisser la collecte publier. Voir `docs/github-projects.md`.

`resync` — reconstruire `state/fiches.json` (US-4.2.2) depuis GitHub, quand
il est illisible ou perdu. Lecture seule côté GitHub : les Issues du dépôt
qui portent un identifiant d'offre, et les cartes du tableau. Seul le
fichier local est réécrit ; il reste à le commiter.
"""

from __future__ import annotations

import argparse
import sys
from typing import Mapping

import httpx

from src.board.github import GitHubClient, GitHubError
from src.board.publication import reconstruire
from src.board.registre import RegistreFiches, RegistreIllisible
from src.core.config import ConfigError, load_board
from src.core.secrets import SecretsError

DEFAULT_BOARD_PATH = "config/board.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.board",
        description="Tableau de revue GitHub Projects.",
    )
    commandes = parser.add_subparsers(dest="commande", required=True)
    check = commandes.add_parser(
        "check", help="vérifie jeton, dépôt, Project et statut initial, sans rien écrire"
    )
    resync = commandes.add_parser(
        "resync", help="reconstruit le registre des fiches depuis GitHub, sans rien y écrire"
    )
    resync.add_argument(
        "--fiches",
        default=None,
        help="registre des fiches à réécrire (défaut : state/fiches.json)",
    )
    for commande in (check, resync):
        commande.add_argument(
            "--board",
            default=DEFAULT_BOARD_PATH,
            help="configuration du tableau (défaut : %(default)s)",
        )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    client: httpx.Client | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_board(args.board)
        github = GitHubClient.from_env(config, env=env, client=client)
    except (ConfigError, SecretsError) as exc:
        print(f"board {args.commande} : KO — {exc}", file=sys.stderr, flush=True)
        return 1

    if args.commande == "resync":
        with github:
            return _resync(github, args.fiches)

    with github:
        try:
            acces = github.verifier_acces()
            scopes = ", ".join(acces.scopes) if acces.scopes is not None else "non annoncés"
            print(
                f"  ok   jeton accepté, dépôt {acces.repository}, Issues actives "
                f"(scopes : {scopes})",
                flush=True,
            )

            projet = github.projet()
            print(f"  ok   Project « {projet.titre} » — {projet.url}", flush=True)

            options = ", ".join(projet.options_statut) or "aucune"
            if projet.option(config.status_initial) is None:
                print(
                    f"  KO   champ « {config.status_field} » sans option "
                    f"« {config.status_initial} » (options : {options})",
                    file=sys.stderr,
                    flush=True,
                )
                return 1
            print(f"  ok   champ « {config.status_field} » : {options}", flush=True)
        except GitHubError as exc:
            print(f"  KO   {type(exc).__name__} — {exc}", file=sys.stderr, flush=True)
            return 1

    print("board check : prêt à publier")
    return 0


def _resync(github: GitHubClient, fiches: str | None) -> int:
    chemin = RegistreFiches.chemin(fiches)
    try:
        avant = f"{len(RegistreFiches.load(chemin))} entrée(s)"
    except RegistreIllisible:
        avant = "illisible"

    try:
        registre = reconstruire(github, chemin)
    except GitHubError as exc:
        print(f"  KO   {type(exc).__name__} — {exc}", file=sys.stderr, flush=True)
        return 1

    hors = sum(1 for fiche in registre.fiches() if fiche.hors_tableau)
    print(
        f"  ok   {len(registre)} fiche(s) trouvée(s) sur GitHub — "
        f"{len(registre) - hors} sur le tableau, {hors} hors du tableau",
        flush=True,
    )
    registre.save()
    print(f"board resync : {chemin} réécrit (avant : {avant}) — à commiter")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
