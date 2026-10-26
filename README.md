# nettoyage-serveur

Un agent de ménage pour serveur Linux qui **n'efface que ce que vous avez autorisé**. Vous (ou votre assistant IA, en votre présence) faites un état des lieux : le disque est rangé par service, chaque élément reçoit une consigne (garder, archiver puis effacer, supprimer, vider tous les N jours). Une passe de nuit applique seulement ce plan, après avoir archivé et vérifié.

## Ce que contient le dépôt
• `nettoyer.py` : l'outil d'effacement, le seul qui supprime. Inventaire, garde-fous, archivage vérifié, passe de ménage.
• `plan.py` : écrit le plan de ménage (services, éléments, consignes). Il n'efface rien.
• `serveur.py` et `page/` : une page web en lecture seule (plan, derniers ménages, ce qui reste hors plan).
• `systemd/` : minuterie de nuit (02 h), service de la page, exemple de règle `sudoers`.
• `tests/tests.sh` : les garde-fous et la restauration, sur de vrais outils.
• `config/config.example.json`, `install.sh`.

## Garde-fous
• Aucun effacement sans règle dans le registre ou le plan ; `garder` et `verrou` sont refusés.
• Chemins protégés codés en dur (système, `/etc`, dossiers personnels entiers, clés SSH, Docker brut…), extensibles par `interdits_en_plus`.
• Refus si un processus utilise le chemin (dossier courant, fichier ouvert, ligne de commande) ou si une unité systemd ou une tâche cron active le cite.
• Un élément qui contient un sous-élément « garder » est refusé : le plus précis l'emporte.
• Archive `tar | zstd` envoyée par `rclone` (dossier local ou stockage distant), empreinte md5 comparée à l'arrivée, **puis seulement** effacement. Les archives sont gardées 183 jours puis supprimées. Restauration : `rclone cat <archive> | zstd -dc | tar -x -C /` (commande aussi écrite dans `archives.jsonl`).
• Une consigne qui efface demande les mots du propriétaire (`--citation`) : c'est un garde-fou de comportement pour un assistant IA, pas une authentification. La vraie protection est le registre root et les chemins interdits.

## Installer
Prérequis : Linux, Python 3.8+, `tar`, `zstd`, `rclone`, `du` ; Docker et systemd facultatifs.
```
git clone <ce dépôt> && cd nettoyage-serveur
sudo ./install.sh VOTRE_COMPTE
sudoedit /etc/nettoyage-serveur/config.json     # dossier d'archives (ou « monremote:archives »)
sudo nettoyer inventaire
sudo bash tests/tests.sh                          # vérifie les garde-fous sur des dossiers jetables
```
`install.sh` copie ce que root exécute dans `/usr/local/lib/nettoyage-serveur` et active la minuterie et la page (`http://127.0.0.1:7965`, sans authentification : accès par tunnel SSH ou VPN).

## Utiliser
```
nettoyage-plan service --id blog --nom "Blog" --role "Site et ses caches" --etat vivant --citation "c'est mon blog"
nettoyage-plan element --service blog --chemin /srv/blog/cache --quoi "Cache de pages" --consigne vider --jours 30 --citation "vide-le chaque mois"
nettoyage-plan element --service blog --chemin /srv/blog/contenu --quoi "Articles" --consigne garder --citation "jamais"
nettoyage-plan a-voir          # ce que l'inventaire montre et que le plan ne couvre pas
nettoyage-plan essai           # ce que la prochaine passe ferait (à blanc)
sudo nettoyer appliquer /srv/blog/cache --essai
```
Consignes : `garder`, `archiver` (une fois, avec archive), `supprimer` (une fois, sans archive), `vider` (tous les N jours, avec ou sans archive ; pour `docker:images-inutilisees`, vide les images inutilisées).
Arrêt : `systemctl disable --now nettoyage-serveur-nuit.timer nettoyage-serveur-page.service`.

## Licences
Code : Apache 2.0 (`LICENSE`, `NOTICE`). Texte et documentation : CC BY 4.0 (`LICENSE-docs`). Aucune dépendance Python tierce ; programmes externes : tar, zstd (BSD/GPL), rclone (MIT), utilisés comme commandes séparées.
