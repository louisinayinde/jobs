# État du crawl

Ce dossier porte l'**état** que le dépôt héberge au même titre que le code et
la configuration (US-1.1.1). Il existe pour une raison précise : le runner
GitHub Actions est **éphémère**. Rien de ce qu'un run calcule ne lui survit,
alors que la collecte a besoin de se souvenir d'où elle en était.

| Fichier | Écrit par | Contient |
|---|---|---|
| `crawl.json` | `src/core/state.py` (Feature 2.5) | où en est le crawl de chaque source lue par sitemap |
| `seen.json` | `src/core/dedup.py` (Feature 3.3) | les offres retenues déjà vues, avec leur date de première vue |
| `fiches.json` | `src/board/registre.py` (Feature 4.2) | les offres publiées sur le tableau, avec leur numéro d'Issue |

`crawl.json` porte, par source :

- `cursor` — le `<lastmod>` le plus récent réellement traité. Le run suivant
  ne reprend que ce qui lui est postérieur ;
- `urls` — les URLs déjà chargées, **uniquement** pour les sitemaps qui ne
  publient pas de `<lastmod>` (Japan Dev). L'incrémental se repère alors sur
  l'identité de l'URL au lieu d'une date.

Le fichier est écrit trié et il est versionné : un `git log` doit laisser voir
quelles offres sont apparues d'un run à l'autre. Il est commité par
`collect-slow.yml`, sous un groupe `concurrency` — deux runs qui l'écriraient
en même temps se marcheraient dessus.

**Le supprimer ne casse rien** : un fichier absent ou corrompu vaut un état
vide, et le crawl repart d'une fenêtre bornée (une semaine). Ça coûte un
recrawl, jamais un run en échec.

## `seen.json`

Une ligne par offre **retenue** déjà vue, triée :

```json
{
  "greenhouse:gitlab:8620720002": "2026-09-16T08:15:00Z"
}
```

- la clé est l'identifiant stable `source:entreprise:id` (US-3.3.1) —
  l'entreprise réduite à un slug, l'identifiant natif laissé tel quel ;
- la valeur est la date de **première** vue, jamais réécrite ensuite.

Seules les offres qui passent le filtre de rétention y entrent : ~335 sur
3 530 lors d'un vrai run (2026-09-16), soit ~21 Ko. Élargir `filters.yaml`
fait donc apparaître d'anciennes offres comme neuves, ce qui est voulu.

Il est commité par **les deux** workflows de collecte, qui partagent le
groupe `concurrency` `collect-state` pour ne jamais écrire en même temps.

Depuis la Feature 4.3, une offre n'y entre qu'une fois **traitée** par la
publication : sa fiche est sur le tableau (créée maintenant ou avant), ou
GitHub l'a refusée pour son contenu. Une offre reportée — plafond de
`--max-publications` atteint, publication interrompue par une panne — n'y
entre pas, et revient au run suivant.

**Le supprimer ne casse rien non plus** : toutes les offres retenues
encore en ligne redeviennent neuves pour un run, puis sont mémorisées à
nouveau. Un fichier corrompu (conflit de fusion compris) vaut un état vide.

⚠️ **Avant la Feature 4.3**, `seen.json` mémorisait des offres **qui
n'avaient été publiées nulle part**. Pour que ces offres arrivent sur le
tableau, le fichier est vidé une fois, à la mise en service de la publication
(`git rm state/seen.json`, voir `docs/github-projects.md`, section 7). Sans
risque de doublon : c'est `fiches.json` qui décide de créer une fiche.

## `fiches.json`

Une ligne par offre **publiée** : identifiant stable → Issue.

```json
{
  "greenhouse:gitlab:4012": {"issue": 42, "node_id": "I_kwDO…", "item": "PVTI_…"},
  "lever:malt:9f1c":        {"issue": 43, "node_id": "I_kwDO…", "hors_tableau": true},
  "ashby:ramp:77":          {"en_cours": "2026-09-16T08:15:00Z"}
}
```

- `issue`, `node_id` — l'Issue existe ; `item` — sa carte est sur le tableau ;
- `hors_tableau` — l'Issue a été retirée du tableau : elle n'y sera pas reposée ;
- `en_cours` — une création est partie sans réponse nette (timeout, 502).
  La publication suivante cherche l'Issue sur GitHub avant d'en créer une.

C'est ce fichier, et non `seen.json`, qui garantit qu'une offre n'a jamais
deux fiches : une offre qu'il connaît ne déclenche aucune création.

Il est écrit par la publication (`python -m src.collect`, les deux cadences)
**après chaque geste** sur GitHub, et commité par les deux workflows de
collecte **même quand le run échoue** (`if: always()`) : un run qui publie
puis tombe en panne a créé des Issues, et doit laisser leur trace.

⚠️ **Contrairement aux deux autres, un `fiches.json` corrompu arrête la
publication.** Repartir d'un registre vide recréerait une fiche pour chaque
offre déjà publiée. Pour le réparer (conflit de fusion, fichier perdu),
le reconstruire depuis les Issues elles-mêmes, qui portent leur identifiant :

```sh
read -rs GITHUB_TOKEN && export GITHUB_TOKEN
.venv/bin/python -m src.board resync
unset GITHUB_TOKEN
git add state/fiches.json && git commit -m "chore(state): registre des fiches reconstruit" && git push
```

Trois fichiers, trois responsabilités : `crawl.json` retient *où en est le
crawl d'une source*, `seen.json` *quelles offres ont déjà été vues*,
`fiches.json` *lesquelles ont été publiées, et où*.
