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
`GITHUB_TOKEN` : c'est le workflow qui fait le lien, à partir de la
Feature 4.3, par `GITHUB_TOKEN: ${{ secrets.JOBRADAR_TOKEN }}`.

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
