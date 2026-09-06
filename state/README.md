# État du crawl

Ce dossier porte l'**état** que le dépôt héberge au même titre que le code et
la configuration (US-1.1.1). Il existe pour une raison précise : le runner
GitHub Actions est **éphémère**. Rien de ce qu'un run calcule ne lui survit,
alors que la collecte a besoin de se souvenir d'où elle en était.

| Fichier | Écrit par | Contient |
|---|---|---|
| `crawl.json` | `src/core/state.py` (Feature 2.5) | où en est le crawl de chaque source lue par sitemap |

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

Ce n'est pas `seen.json`, que la Feature 3.3 ajoutera ici pour mémoriser les
offres déjà publiées sur le tableau. Deux fichiers, deux responsabilités.
