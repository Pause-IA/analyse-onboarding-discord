# Analyse onboarding Discord (Pause IA)

Script **en lecture seule** qui exporte l'activité du serveur Discord de Pause IA, **sans le texte des messages et pseudonymisée**, pour analyser l'implication des membres (trajectoires d'activité, rétention, diagrammes de Sankey).
Python standard, aucune dépendance. Lancé à la main par l'équipe tech, pas de bot allumé en permanence.

## Deux niveaux de fichiers

| Dossier | Contenu | Qui y a accès |
| --- | --- | --- |
| `cache/` | Ids réels des messages (pour ne récupérer que les nouveaux) et heures exactes. Les auteurs sont déjà pseudonymisés. | Équipe tech uniquement, ne sort jamais |
| `out/` | Le livrable pour l'analyse, régénéré à chaque `build` | Envoyé à la personne qui fait l'analyse |

## Ce que contient le livrable (et ce qu'il ne contient pas)

- **Jamais le texte des messages.** Le bot n'a pas l'option *Message Content Intent* : Discord ne lui transmet pas le texte, ni les pièces jointes.
- **Membres** : chaque membre devient un code stable `u_xxxxxxxxxxxx` = HMAC-SHA256(`PSEUDO_SALT`, id Discord). Le même membre garde le même code d'un export à l'autre. Sans le sel, impossible de savoir qui est derrière. Aucun pseudo ni nom n'est écrit.
- **Messages** : leur id Discord est remplacé par un code `m_…`, pour qu'on ne puisse pas ouvrir le message sur Discord et voir son auteur. Même chose pour les fils (`t_…`), dont le nom est retiré, car il contient souvent le nom d'un membre.
- **Heures arrondies à l'heure**. Le rang du message dans l'historique du membre (`rank`) et le délai de réponse (`reply_delay`) sont calculés avant l'arrondi.
- **Rôles rares regroupés** en `autre` : un rôle porté par moins de `ROLE_MIN_MEMBERS` personnes (« Président »…) désignerait quelqu'un. Les rôles de `ROLES_ALWAYS_KEEP` sont toujours gardés. Les rôles de bots sont retirés.
- **Mentions** : seulement leur nombre, pas qui est mentionné.
- **Droit d'opposition** : les membres listés dans `EXCLUDED_USER_IDS` sont retirés du cache et du livrable, y compris rétroactivement.
- **Salons exclus** : `EXCLUDED_CHANNELS` (un id de catégorie exclut tous ses salons et fils), en plus des permissions du bot.
- Le bot ne peut ni poster, ni modérer, ni lire les MP.

Limite : un membre actif dans un salon calme reste reconnaissable par quelqu'un qui connaît bien le serveur. Le but est de rendre la ré-identification difficile et volontaire, pas impossible.

### Fichiers de `out/`

`messages.csv`, une ligne par message :

| Colonne | Contenu |
| --- | --- |
| `message` | code du message `m_…` |
| `hour_utc` | heure UTC arrondie, ex. `2026-03-14T15:00Z` |
| `channel` | id du salon, ou code `t_…` pour un fil (voir `channels.csv`) |
| `parent_channel` | pour un fil : le salon qui le contient |
| `type` | type Discord : 0 = message, 19 = réponse, 7 = « X a rejoint le serveur », etc. |
| `author` | code du membre `u_…` |
| `bot` | 1 si l'auteur est un bot |
| `rank` | n-ième message (types 0 et 19) de ce membre, tous salons confondus |
| `reply_to` | code du message auquel il répond |
| `reply_delay` | délai depuis ce message : `<15min`, `15min-1h`, `1h-6h`, `6h-24h`, `>24h` |
| `mentions` | nombre de membres mentionnés |
| `reactions` | nombre de réactions reçues |
| `thread_started` | code du fil ouvert à partir de ce message |

`channels.csv` : salon (id ou code de fil), nom (vide pour les fils), type, catégorie, salon parent.

`members_AAAA-MM-JJ.csv` : une photo des membres présents à chaque lancement (`author`, `joined_day` = date d'arrivée au jour près, `roles` séparés par `;`). Comparer deux photos successives montre **les départs** (un code qui disparaît) et **les changements de rôle**, à la précision de l'intervalle entre deux lancements. Cet historique commence au premier lancement.

## 1. Créer le bot (~5 min)

1. https://discord.com/developers/applications → **New Application** (ex. `Pause IA – analyse`).
2. Onglet **Bot** → **Reset Token** → le copier dans `.env` (`cp .env.example .env`) en `DISCORD_BOT_TOKEN`.
3. Même onglet, **Privileged Gateway Intents** : activer **Server Members Intent** uniquement. **Laisser *Message Content Intent* désactivé.** Décocher **Public Bot**.
4. Onglet **OAuth2** → copier le **Client ID**, puis inviter le bot avec :
   `https://discord.com/oauth2/authorize?client_id=CLIENT_ID&permissions=66560&scope=bot`
   (66560 = Voir les salons + Lire l'historique, rien d'autre). On peut retirer l'accès du rôle du bot aux salons modo/privés.
5. `GUILD_ID` = id du serveur (Paramètres → Avancés → Mode développeur, puis clic droit sur le serveur → Copier l'identifiant).
6. `PSEUDO_SALT` : `python3 -c "import secrets; print(secrets.token_hex(32))"`. **À garder secret et à ne jamais changer.**
7. Remplir `EXCLUDED_CHANNELS`, `EXCLUDED_USER_IDS` et `ROLES_ALWAYS_KEEP` si besoin (voir `.env.example`).

## 2. Lancer

    python3 export.py check   # vérifie le token et les intents, liste les salons lisibles
    python3 export.py run     # met à jour cache/ puis génère out/

`run` = `fetch` (met à jour le cache, seulement les nouveaux messages) puis `build` (régénère `out/` depuis le cache). `build` seul suffit après un changement de `EXCLUDED_*` ou de `ROLE_*`.

Avec Docker, pour un lancement isolé qui s'arrête à la fin :

    docker build -t pauseia-export .
    docker run --rm --user "$(id -u)" --env-file .env \
      -v "$PWD/cache:/cache" -v "$PWD/out:/out" pauseia-export

Tests (sans réseau, sur un faux serveur) : `python3 -m unittest test_export.py`

## 3. Transmettre et conserver

- Envoyer **uniquement** le dossier `out/` zippé. Jamais `cache/` ni `.env` : le sel ne sort pas de l'équipe tech.
- Conservation : supprimer le cache, les livrables et les photos de membres de plus de 12 mois (les chiffres agrégés peuvent être gardés).
- Un membre demande à être exclu : ajouter son id Discord à `EXCLUDED_USER_IDS`, relancer, et renvoyer le nouveau `out/` en demandant de supprimer l'ancien.
