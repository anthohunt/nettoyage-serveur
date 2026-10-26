#!/bin/bash
# install.sh (root) : installe ou met a jour le nettoyage depuis ce depot.
# Usage : sudo ./install.sh [COMPTE_ADMIN]   (le compte recoit le droit sudo d'appeler nettoyer et ecrit le plan)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "a lancer en root (sudo)"; exit 1; }
D=$(cd "$(dirname "$0")" && pwd); ADMIN=${1:-}
install -m 755 -o root -g root $D/nettoyer.py /usr/local/sbin/nettoyer
# Ce que root execute vit dans une copie root : le depot peut appartenir a un autre compte.
L=/usr/local/lib/nettoyage-serveur; mkdir -p $L/page
install -m 755 -o root -g root $D/serveur.py $D/nuit.sh $L/
install -m 644 -o root -g root $D/page/* $L/page/
install -m 755 -o root -g root $D/plan.py $L/plan.py
ln -sf $L/plan.py /usr/local/bin/nettoyage-plan
install -m 644 $D/systemd/nettoyage-serveur-*.service $D/systemd/nettoyage-serveur-*.timer /etc/systemd/system/
mkdir -p /etc/nettoyage-serveur/plans /var/lib/nettoyage-serveur; chmod 755 /etc/nettoyage-serveur /var/lib/nettoyage-serveur
[ -f /etc/nettoyage-serveur/config.json ] || install -m 644 $D/config/config.example.json /etc/nettoyage-serveur/config.json
[ -f /etc/nettoyage-serveur/registre.json ] || echo '{"version": 1, "elements": {}}' > /etc/nettoyage-serveur/registre.json
if [ -n "$ADMIN" ]; then
  sed "s/^ADMIN /$ADMIN /" $D/systemd/sudoers-nettoyage-serveur.example > /etc/sudoers.d/nettoyage-serveur
  chmod 440 /etc/sudoers.d/nettoyage-serveur; visudo -cf /etc/sudoers.d/nettoyage-serveur
  chown "$ADMIN" /etc/nettoyage-serveur/plans
fi
command -v systemctl >/dev/null && [ -d /run/systemd/system ] && {
  systemctl daemon-reload
  systemctl enable --now nettoyage-serveur-nuit.timer
  systemctl enable nettoyage-serveur-page.service; systemctl restart nettoyage-serveur-page.service; } \
  || echo "systemd absent : unites non activees (outil nettoyer installe)"
echo "installe. Essai : sudo nettoyer inventaire && sudo nettoyer menage --essai"
