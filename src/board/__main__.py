"""`python -m src.board check` — vérifier la mise en place du tableau, sans rien écrire.

Trois contrôles, dans l'ordre où une mise en place échoue d'habitude :

1. le jeton `GITHUB_TOKEN` est accepté et ouvre le dépôt, Issues actives ;
2. le Project de `board.yaml` est visible pour ce jeton ;
3. son champ de statut a bien l'option initiale (« Nouveau »).

Rien n'est créé : c'est la commande à lancer après avoir créé le jeton et le
Project, avant de laisser la collecte publier. Voir `docs/github-projects.md`.
"""

from __future__ import annotations

import argparse
import sys
from typing import Mapping

import httpx

from src.board.github import GitHubClient, GitHubError
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
    check.add_argument(
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
        print(f"board check : KO — {exc}", file=sys.stderr, flush=True)
        return 1

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


if __name__ == "__main__":
    raise SystemExit(main())
