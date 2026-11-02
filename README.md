# Analyse onboarding Discord (Pause IA)

Bot **en lecture seule** qui exporte l'historique du serveur Discord de Pause IA, **pseudonymisé**, pour analyser l'implication des membres (trajectoires d'activité, diagrammes de Sankey).
Python standard, aucune dépendance.

## Ce qui est exporté (et ce qui ne l'est pas)
- Chaque utilisateur est remplacé par un pseudonyme stable `u_xxxxxxxxxxxx` = HMAC-SHA256(`PSEUDO_SALT`, id Discord). Sans le sel, on ne peut pas remonter à la personne. Avec le sel, les pseudonymes restent les mêmes d'un export à l'autre, ce qui permet de suivre les trajectoires.
- **Aucun pseudo ni nom d'utilisateur** n'est écrit. Les mentions `@quelqu'un` dans le texte sont aussi pseudonymisées.
- Par message : date, salon/fil, auteur pseudonymisé, réponse à quel message, nb de réactions et de pièces jointes, texte (désactivable avec `INCLUDE_CONTENT=0`).
- Par membre : pseudonyme, date d'arrivée, rôles (`_roles.json` donne le nom des rôles).
- Limite : un prénom écrit en clair dans un message reste dans le texte. Si c'est un problème, mettre `INCLUDE_CONTENT=0` (on garde quand même toutes les métadonnées d'activité).
- Le bot ne peut ni poster, ni modérer, ni lire les MP.

## 1. Créer le bot (~5 min)
1. https://discord.com/developers/applications → **New Application** (ex. `Pause IA – analyse`).
2. Onglet **Bot** → **Reset Token** → le copier dans `.env` (`cp .env.example .env`) en `DISCORD_BOT_TOKEN`.
3. Même onglet, **Privileged Gateway Intents** : activer **Message Content Intent** et **Server Members Intent**. Décocher **Public Bot**.
4. Onglet **OAuth2** → copier le **Client ID**, puis inviter le bot avec :
   `https://discord.com/oauth2/authorize?client_id=CLIENT_ID&permissions=66560&scope=bot`
   (66560 = Voir les salons + Lire l'historique, rien d'autre). On peut retirer l'accès du rôle du bot aux salons modo/privés.
5. `GUILD_ID` = id du serveur (Paramètres → Avancés → Mode développeur, puis clic droit sur le serveur → Copier l'identifiant).
6. `PSEUDO_SALT` : `python3 -c "import secrets; print(secrets.token_hex(32))"`. **À garder secret et à ne jamais changer.**

## 2. Lancer
Une fois (analyse ponctuelle) :

    python3 export.py check    # liste les salons lisibles, prévient si le contenu est vide
    python3 export.py export   # export complet → data/

Relancer `export` ne récupère que les nouveaux messages.

En continu (Docker, un export toutes les `INTERVAL_HOURS`, 24 h par défaut) :

    docker compose up -d --build

## Sortie (`data/`)
- `<id_salon>.jsonl` : un message par ligne, du plus ancien au plus récent
- `_channels.json` : id → nom/type/salon parent (les fils pointent vers leur salon)
- `_members.json` : membres actuels (pseudonyme, arrivée, rôles)
- `_roles.json` : id de rôle → nom

Pour l'analyse, envoyer le dossier `data/` zippé, **sans** le `.env` (le sel ne sort pas du serveur).
