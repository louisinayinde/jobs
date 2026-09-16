# Mise en place du tableau GitHub Projects

Le client de la Feature 4.1 (`src/board/github.py`) publie chaque offre retenue
en **Issue** de ce dépôt, puis la pose sur un **GitHub Project** avec le statut
« Nouveau ». Trois choses ne se font pas dans le code, et se font une seule fois.

## 1. Créer le Project

1. github.com → ta photo de profil → **Your projects** → **New project**.
2. Modèle **Board**, nom **JobRadar** → **Create project**.
3. Relève le **numéro** dans l'URL : `https://github.com/users/louisinayinde/projects/<numéro>`.
4. Reporte-le dans `config/board.yaml`, clé `project.number`.

## 2. Régler les colonnes (champ « Status »)

Le modèle Board crée un champ **Status** avec `Todo`, `In Progress`, `Done`. Le
client exige l'option **Nouveau** ; les trois autres serviront aux Epics 5 à 7.

Dans le Project : **⋯** (en haut à droite) → **Settings** → **Status**, puis :

| Option actuelle | Renommer en | Sert à |
|---|---|---|
| Todo | **Nouveau** | fiche fraîchement publiée (Feature 4.1) |
| In Progress | **En génération** | CV en cours (US-5.2.2) |
| Done | **CV prêt** | PDF déposé (Feature 6.5) |
| — (ajouter) | **Ignoré** | offre écartée (US-5.1.1) |

La casse ne compte pas, l'orthographe si. Un autre nom de champ ou d'option
initiale se règle dans `board.yaml` (`status.field`, `status.initial`).

## 3. Créer le jeton

Le `GITHUB_TOKEN` qu'Actions fournit tout seul **ne suffit pas** : il est
limité au dépôt, et un Project appartient à ton compte. Il faut un jeton
personnel.

1. github.com → **Settings** → **Developer settings** → **Personal access tokens**
   → **Tokens (classic)** → **Generate new token (classic)**.
2. Note : `JobRadar`. Expiration : au choix — à l'expiration, le client lèvera
   `GitHubAuthError` (401) et il faudra régénérer le jeton et mettre à jour le secret.
3. Scopes : **`repo`** (ou `public_repo`, le dépôt étant public) **et `project`**.
4. **Generate token**, copie la valeur `ghp_…` (elle ne sera plus affichée).

Un jeton *fine-grained* n'est pas recommandé ici : l'accès aux Projects d'un
compte personnel n'y est pas garanti, là où la combinaison classique
`repo` + `project` est celle que le client a vérifiée.

## 4. Ranger le jeton en secret

Dépôt → **Settings** → **Secrets and variables** → **Actions** →
**New repository secret** : nom **`JOBRADAR_TOKEN`**, valeur `ghp_…`.

Le nom ne peut pas commencer par `GITHUB_` (réservé). Le code, lui, lit
`GITHUB_TOKEN` : ce sont les workflows `collect.yml` et `collect-slow.yml`
qui font le lien, par `GITHUB_TOKEN: ${{ secrets.JOBRADAR_TOKEN }}`.
**Sans ce secret, la collecte échoue à chaque run** (« variable
d'environnement requise absente : GITHUB_TOKEN »).

## 5. Vérifier

Depuis la racine du dépôt, sans rien écrire sur GitHub :

```sh
read -rs GITHUB_TOKEN && export GITHUB_TOKEN   # colle le jeton, rien ne s'affiche
.venv/bin/python -m src.board check
unset GITHUB_TOKEN
```

`read -s` évite que le jeton finisse dans l'historique du shell. Résultat attendu :

```
  ok   jeton accepté, dépôt louisinayinde/jobs, Issues actives (scopes : project, repo)
  ok   Project « JobRadar » — https://github.com/users/louisinayinde/projects/<numéro>
  ok   champ « Status » : Nouveau, En génération, CV prêt, Ignoré
board check : prêt à publier
```

| Message | Cause | Correctif |
|---|---|---|
| `clé requise non renseignée : « number »` | étape 1.4 | numéro dans `board.yaml` |
| `GitHubAuthError … 401` | jeton faux, expiré ou révoqué | étape 3 |
| `Project n°… introuvable … scope « project »` | jeton sans le scope `project` | étape 3.3 |
| `Project n°… introuvable` | mauvais numéro ou mauvais `owner` | `board.yaml` |
| `sans option « Nouveau »` | colonnes non renommées | étape 2 |
| `les Issues sont désactivées` | Issues coupées sur le dépôt | Settings → General → Features → Issues |

## 6. Si le registre des fiches est cassé

Chaque offre publiée est notée dans `state/fiches.json` (Feature 4.2), et
c'est ce qui empêche de la publier deux fois. S'il devient illisible — le plus
souvent un conflit de fusion —, la publication s'arrête avec
`registre des fiches illisible … python -m src.board resync`. Pour le
reconstruire depuis les Issues (lecture seule sur GitHub) :

```sh
git pull
read -rs GITHUB_TOKEN && export GITHUB_TOKEN
.venv/bin/python -m src.board resync
unset GITHUB_TOKEN
git add state/fiches.json && git commit -m "chore(state): registre des fiches reconstruit" && git push
```

Résultat attendu : `ok   N fiche(s) trouvée(s) sur GitHub — N sur le tableau, 0 hors du tableau`.
Une Issue « hors du tableau » (carte retirée à la main) ne sera pas reposée.

## 7. Mettre en service la publication (Feature 4.3)

À chaque run, `python -m src.collect` publie les offres nouvelles sur le
tableau, **meilleur score d'abord**, au plus 50 par run (`--max-publications`) ;
le reste part aux runs suivants. Une seule fois, à la mise en service :

```sh
# 1. Le jeton et le tableau sont prêts (sections 3 à 5) :
read -rs GITHUB_TOKEN && export GITHUB_TOKEN
.venv/bin/python -m src.board check
unset GITHUB_TOKEN

# 2. Facultatif — un essai à blanc : collecte, filtre et classe, sans rien
#    publier ni écrire dans state/ (pas besoin de jeton) :
.venv/bin/python -m src.collect --sans-publication

# 3. Récupérer les derniers commits d'état des workflows, puis vider
#    `seen.json`, qui mémorise des offres jamais publiées (state/README.md) :
git pull --rebase
git rm state/seen.json
git commit -m "chore(state): seen.json vidé à la mise en service de la publication"
git push
```

Si `git push` est refusé parce qu'un run vient de pousser l'état entre-temps :
`git pull --rebase`, et en cas de conflit sur `state/seen.json` (« deleted by
us ») : `git rm state/seen.json && GIT_EDITOR=true git rebase --continue`,
puis `git push`.

Le run suivant (au plus 15 min, ou **Actions → Collect → Run workflow**)
publie les 50 premières offres ; les ~300 autres suivent en six ou sept runs.

### Lire le bilan d'un run

Dans le log de l'étape `python -m src.collect` :

```
collect [fast] : 50 fiche(s) publiée(s) sur 335 offre(s), 285 reportée(s) au run suivant
  #12 lever:malt:5549f929-…
collect [fast] : 50 offre(s) mémorisée(s), 50 au total dans state/seen.json
```

| Ligne | Sens | À faire |
|---|---|---|
| `N déjà sur le tableau` | le registre les connaissait, aucun appel | rien |
| `N reportée(s) au run suivant` | plafond atteint | rien, elles suivent |
| `INTERROMPUE : GitHubAuthError …` (run en échec) | jeton expiré ou révoqué | section 3, puis mettre à jour le secret |
| `INTERROMPUE : … n'a pas d'option « Nouveau »` | colonnes renommées | section 2 |
| `INTERROMPUE : GitHubRateLimitError` | limite de débit GitHub | rien : les offres reportées repartent au run suivant |
| `N refusée(s) par GitHub` (run en échec) | GitHub rejette le contenu d'une offre (422) | lire la ligne `ERROR` du log ; l'offre n'est pas retentée |
| `registre des fiches illisible` | `state/fiches.json` en conflit | section 6 |

Un run en échec **commite quand même** `state/` : les fiches créées avant la
panne sont notées, et ne seront pas recréées.
