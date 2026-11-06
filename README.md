**English** · [Français](README.fr.md)

**Anthony Hunt** · [ingevision.cloud](https://ingevision.cloud/?lang=en) · [LinkedIn](https://www.linkedin.com/in/anthony-hunt-3948a9196) · [GitHub profile](https://github.com/anthohunt)

# nettoyage-serveur

A cleanup agent for Linux servers that **only deletes what you have authorised**. You (or your AI assistant, with you present) take stock: the disk is organised by service and each item gets an instruction (keep, archive then delete, delete, empty every N days). A nightly pass applies this plan and nothing else, after archiving and verifying.

The commands, options and instructions are in French; the English meaning is given below.

## What is in the repository
- `nettoyer.py`: the deletion tool, the only one that deletes. Inventory, safeguards, verified archiving, cleanup pass.
- `plan.py`: writes the cleanup plan (services, items, instructions). It deletes nothing.
- `serveur.py` and `page/`: a read-only web page (plan, latest cleanups, what remains outside the plan).
- `systemd/`: nightly timer (2 am), page service, sample `sudoers` rule.
- `tests/tests.sh`: the safeguards and the restore, on real tools.
- `config/config.example.json`, `install.sh`.

## Safeguards
- No deletion without a rule in the registry or the plan; `garder` (keep) and `verrou` (lock) are refused.
- Hard-coded protected paths (system, `/etc`, whole home folders, SSH keys, raw Docker…), extensible through `interdits_en_plus`.
- Refusal if a process uses the path (current folder, open file, command line) or if an active systemd unit or cron job mentions it.
- An item that contains a "keep" sub-item is refused: the most specific rule wins.
- `tar | zstd` archive sent by `rclone` (local folder or remote storage), md5 checksum compared on arrival, and **only then** deletion. Archives are kept for 183 days, then deleted. Restore: `rclone cat <archive> | zstd -dc | tar -x -C /` (the command is also written in `archives.jsonl`).
- An instruction that deletes requires the owner's words (`--citation`): this is a behavioural safeguard for an AI assistant, not authentication. The real protection is the root registry and the forbidden paths.

## Install
Requirements: Linux, Python 3.8+, `tar`, `zstd`, `rclone`, `du`; Docker and systemd optional.
```
git clone <this repository> && cd nettoyage-serveur
sudo ./install.sh YOUR_ACCOUNT
sudoedit /etc/nettoyage-serveur/config.json     # archive folder (or "myremote:archives")
sudo nettoyer inventaire
sudo bash tests/tests.sh                          # checks the safeguards on disposable folders
```
`install.sh` copies what root runs to `/usr/local/lib/nettoyage-serveur` and enables the timer and the page (`http://127.0.0.1:7965`, no authentication: access through an SSH tunnel or a VPN).

## Usage
```
nettoyage-plan service --id blog --nom "Blog" --role "Site and its caches" --etat vivant --citation "this is my blog"
nettoyage-plan element --service blog --chemin /srv/blog/cache --quoi "Page cache" --consigne vider --jours 30 --citation "empty it every month"
nettoyage-plan element --service blog --chemin /srv/blog/contenu --quoi "Articles" --consigne garder --citation "never"
nettoyage-plan a-voir          # what the inventory shows and the plan does not cover
nettoyage-plan essai           # what the next pass would do (dry run)
sudo nettoyer appliquer /srv/blog/cache --essai
```
Instructions: `garder` (keep), `archiver` (once, with an archive), `supprimer` (delete once, no archive), `vider` (empty every N days, with or without an archive; for `docker:images-inutilisees`, removes unused images).
Stop: `systemctl disable --now nettoyage-serveur-nuit.timer nettoyage-serveur-page.service`.

## Licenses
Code: Apache 2.0 (`LICENSE`, `NOTICE`). Text and documentation: CC BY 4.0 (`LICENSE-docs`). No third-party Python dependency; external programs: tar, zstd (BSD/GPL), rclone (MIT), used as separate commands.
