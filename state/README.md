# État du crawl

Ce dossier porte l'**état** que le dépôt héberge au même titre que le code et
la configuration (US-1.1.1). Il existe pour une raison précise : le runner
GitHub Actions est **éphémère**. Rien de ce qu'un run calcule ne lui survit,
alors que la collecte a besoin de se souvenir d'où elle en était.

| Fichier | Écrit par | Contient |
|---|---|---|
| `crawl.json` | `src/core/state.py` (Feature 2.5) | où en est le crawl de chaque source lue par sitemap |
| `seen.json` | `src/core/dedup.py` (Feature 3.3) | les offres retenues déjà vues, avec leur date de première vue |

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

**Le supprimer ne casse rien non plus** : toutes les offres retenues
encore en ligne redeviennent neuves pour un run, puis sont mémorisées à
nouveau. Un fichier corrompu (conflit de fusion compris) vaut un état vide.

⚠️ Tant que la publication (Feature 4.3) n'existe pas, `seen.json` mémorise
des offres **qui n'ont été publiées nulle part**. Au branchement de la
publication, videz-le (`git rm state/seen.json`) pour que le premier run
publie les offres déjà en ligne au lieu de les croire traitées.

Deux fichiers, deux responsabilités : `crawl.json` retient *où en est le
crawl d'une source*, `seen.json` *quelles offres ont déjà été vues*.
