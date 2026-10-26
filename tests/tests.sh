#!/bin/bash
# tests.sh (root) : garde-fous de nettoyer + archivage puis restauration identique + plan + page.
# Vrais backends (tar, zstd, rclone vers un dossier local), dossiers jetables sous /srv/test-nettoyage.
# Prerequis : nettoyer installe (install.sh), rclone, zstd, tar, curl.
set -u
ICI=$(cd "$(dirname "$0")/.." && pwd)
T=/srv/test-nettoyage; R=/etc/nettoyage-serveur/registre.json; ok=0; ko=0
verifie(){ if eval "$2"; then echo "OK  $1"; ok=$((ok+1)); else echo "KO  $1"; ko=$((ko+1)); fi; }
cp $R /tmp/registre.sauve
rm -rf $T /home/alice-test; mkdir -p $T/a /home/alice-test; head -c 2000000 /dev/urandom > $T/a/f.bin; echo x > $T/t.txt
AV=$(cd $T && tar -cf - . | md5sum)
python3 - <<PY
import json
r=json.load(open('$R')); e=r['elements']
e['$T']={'regle':'effacer'}; e['/etc']={'regle':'effacer'}; e['/home/alice-test']={'regle':'effacer'}
e['/srv/verrouille']={'regle':'effacer','verrou':True}
json.dump(r,open('$R','w'))
PY
verifie "sans regle refuse" "! nettoyer appliquer /srv/inconnu >/dev/null"
verifie "verrou refuse" "! nettoyer appliquer /srv/verrouille >/dev/null"
verifie "chemin protege (/etc) refuse" "! nettoyer appliquer /etc >/dev/null"
verifie "dossier personnel entier refuse" "! nettoyer appliquer /home/alice-test >/dev/null"
verifie "a blanc n'efface pas" "nettoyer appliquer $T --essai >/dev/null && [ -d $T ]"
python3 $ICI/tests/tenir.py $T/a/f.bin 20 & PID=$!
sleep 1
verifie "dossier utilise par un processus refuse" "! nettoyer appliquer $T >/dev/null && [ -d $T ]"
kill $PID; wait $PID 2>/dev/null
verifie "effacement reel (archive verifiee puis efface)" "nettoyer appliquer $T >/dev/null && [ ! -e $T ]"
A=$(tail -1 /var/lib/nettoyage-serveur/archives.jsonl | python3 -c "import json,sys;print(json.load(sys.stdin)['distant'])")
mkdir -p /tmp/restau; rclone cat "$A" | zstd -dc | tar -x -C /tmp/restau
verifie "restauration identique" "[ \"\$(cd /tmp/restau$T && tar -cf - . | md5sum)\" = \"$AV\" ]"
rm -rf /tmp/restau
# --- plan : une consigne qui efface sans citation est refusee, puis periodique
P=/srv/test-periodique; rm -rf $P; mkdir -p $P/vieux $P/recent; echo a > $P/vieux/x; echo b > $P/recent/y
touch -d '90 days ago' $P/vieux/x $P/vieux
export NETTOYAGE_PLANS=/tmp/plans-test
python3 $ICI/plan.py service --id essai --nom Essai --role "service d'essai" --etat vivant --citation "ok" >/dev/null
verifie "plan : consigne qui efface sans citation refusee" "! python3 $ICI/plan.py element --service essai --chemin $P --quoi x --consigne vider --jours 30 --citation ' ' >/dev/null 2>&1"
verifie "plan : chemin protege refuse" "! python3 $ICI/plan.py element --service essai --chemin /etc --quoi x --consigne archiver --citation 'ok' >/dev/null 2>&1"
python3 $ICI/plan.py element --service essai --chemin $P --quoi "dossier d'essai" --consigne vider --jours 30 --citation "videz ce dossier" >/dev/null
verifie "menage a blanc ne touche rien" "nettoyer menage --essai >/dev/null && [ -d $P/vieux ]"
NETTOYAGE_PLANS=/tmp/plans-test nettoyer menage >/dev/null
verifie "periodique : l'ancien est archive puis efface" "[ ! -e $P/vieux ]"
verifie "periodique : le recent est garde" "[ -f $P/recent/y ]"
# --- page en lecture seule
NETTOYAGE_PLANS=/tmp/plans-test NETTOYAGE_PORT=17965 python3 $ICI/serveur.py & SP=$!
sleep 1
verifie "page : /sante repond" "curl -s http://127.0.0.1:17965/sante | grep -q true"
verifie "page : le plan est affiche" "curl -s http://127.0.0.1:17965/api/etat | grep -q 'Essai'"
verifie "page : aucune ecriture acceptee" "[ \"\$(curl -s -o /dev/null -w '%{http_code}' -X POST -d '{}' http://127.0.0.1:17965/api/x)\" = 501 ]"
kill $SP; wait $SP 2>/dev/null
rm -rf $P /tmp/plans-test /home/alice-test
cp /tmp/registre.sauve $R; rm -f /tmp/registre.sauve
echo "$ok OK, $ko KO"; [ $ko = 0 ]
