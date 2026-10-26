#!/usr/bin/env python3
"""nettoyer — le seul outil qui efface sur le serveur.

Il ne décide rien : il applique les règles du registre /etc/nettoyage-serveur/registre.json et
du plan de ménage (plans/plan-vps.json, écrit avec plan.py pendant un état des lieux avec le propriétaire).
Un agent ou un utilisateur l'appelle par sudo ; il n'efface pas un chemin qui n'a pas de règle.

Règles (par élément) :
  garder                       on n'y touche jamais
  effacer                      dossier : « Archiver » — archivé (rclone), vérifié, puis effacé (une fois)
  supprimer                    dossier : effacé définitivement, sans archive (une fois)
                               docker  : images inutilisées supprimées (re-téléchargeables, pas d'archive)
  periodique  (jours N)        dossier : tous les N jours, les entrées de 1er niveau intouchées
                               depuis N jours sont archivées puis effacées

Sous-commandes :
  nettoyer inventaire              relève tailles et croissance
  nettoyer appliquer ID [--essai]  applique la règle de ID maintenant
  nettoyer entretien [--essai]     règles régulières échues + archives expirées
  nettoyer menage [--essai]        inventaire + entretien + éléments hors plan + journal (passe de nuit)
  nettoyer etat                    registre + inventaire (JSON)

Réglages : /etc/nettoyage-serveur/config.json (voir config/config.example.json).
Archives : <archives>/AAAA-MM-JJ/<nom>.tar.zst, gardées 183 jours puis supprimées définitivement.
Restaurer : voir <état>/archives.jsonl (ligne « restaurer »).
"""
import argparse, datetime, fnmatch, glob, hashlib, json, os, re, shutil, subprocess, sys, time

CONFIG = os.environ.get("NETTOYAGE_CONFIG", "/etc/nettoyage-serveur/config.json")


def _lire_json(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as h:
            return json.load(h)
    except (OSError, ValueError):
        return defaut


CFG = _lire_json(CONFIG, {})
REGISTRE = os.environ.get("NETTOYAGE_REGISTRE", CFG.get("registre", "/etc/nettoyage-serveur/registre.json"))
ETAT = os.environ.get("NETTOYAGE_ETAT", CFG.get("etat", "/var/lib/nettoyage-serveur"))
JOURNAL = os.environ.get("NETTOYAGE_JOURNAL", CFG.get("journal", "/var/log/nettoyage-serveur.log"))
# Destination des archives : un chemin local ou une destination rclone (« monremote:dossier »).
DRIVE = os.environ.get("NETTOYAGE_ARCHIVES", CFG.get("archives", "/var/lib/nettoyage-serveur/archives"))
PLANS = os.environ.get("NETTOYAGE_PLANS", CFG.get("plans", "/etc/nettoyage-serveur/plans"))
SIGNALER = CFG.get("commande_signalement", [])  # ex. ["/usr/local/bin/mon-alerte"] ; le texte est ajouté en dernier argument
RETENTION_JOURS = int(CFG.get("retention_jours", 183))

# Où l'inventaire regarde : (racine, profondeur). Un dossier de plus de 1 Go est aussi détaillé un niveau plus bas.
RACINES = [tuple(r) for r in CFG.get("racines", [["/home", 1], ["/root", 1], ["/srv", 1], ["/opt", 1],
                                                   ["/var/lib", 1], ["/usr/local/lib", 1], ["/tmp", 0], ["/var/log", 0]])]
SEUIL_LISTE = int(CFG.get("seuil_liste_mo", 100)) * 2**20
SEUIL_DETAIL = 2**30
SEUIL_SOUS = 200 * 2**20

# Jamais effaçables, quelle que soit la règle : le cœur du système et ce qui le fait tourner.
INTERDITS = ["/", "/home", "/root", "/etc", "/etc/*", "/usr", "/usr/bin*", "/usr/sbin*", "/usr/lib*", "/boot*",
             "/bin*", "/sbin*", "/lib*", "/proc*", "/sys*", "/dev*", "/run*", "/mnt", "/media",
             "/var", "/var/lib", "/var/lib/containerd*", "/var/lib/docker*", "/var/lib/dpkg*", "/var/lib/apt*",
             "/var/lib/systemd*", "/var/lib/nettoyage-serveur*", "/usr/local/sbin*", "/usr/local/bin*",
             "/home/*/.ssh*", "/home/*/.config*", "/home/*/.gnupg*", "/home/*/.local/bin*",
             "/root/.ssh*", "/root/.config*", "/root/.gnupg*", "/srv", "/opt", "/usr/local/lib", "/tmp", "/var/log"]
INTERDITS += CFG.get("interdits_en_plus", [])
# Un dossier personnel entier ne s'efface pas ; ses sous-dossiers, si (s'ils ne sont pas protégés ci-dessus).
INTERDITS_EXACTS_MOTIFS = [r"^/home/[^/]+$"]


def maintenant():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def journal(texte):
    ligne = f"{maintenant().isoformat()} {texte}"
    print(ligne)
    try:
        with open(JOURNAL, "a", encoding="utf-8") as h:
            h.write(ligne + "\n")
    except OSError:
        pass


def lire(chemin, defaut):
    return _lire_json(chemin, defaut)


def ecrire(chemin, donnees):
    os.makedirs(os.path.dirname(chemin), exist_ok=True)
    tmp = chemin + ".tmp"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(donnees, h, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin)


def plan(machine="vps"):
    return lire(os.path.join(PLANS, f"plan-{machine}.json"), {"services": []})


def regle_du_plan(el):
    """Consigne du plan -> règle de l'outil. Une consigne qui efface sans les mots du propriétaire est ignorée."""
    c = el.get("consigne")
    if c == "garder":
        return {"regle": "garder", "source": "plan", "maj": el.get("maj", "")}
    if not (el.get("citation") or "").strip():
        return None
    base = {"source": "plan", "maj": el.get("maj", ""), "citation": el["citation"]}
    if c == "archiver":
        return {"regle": "effacer", **base}
    if c == "supprimer":
        return {"regle": "supprimer", **base}
    if c == "vider":
        return {"regle": "periodique", "jours": int(el.get("jours") or 30), "archive": el.get("archive", True), **base}
    return None


def registre():
    """Registre root (verrous, anciennes décisions) + plan de ménage. Le plan l'emporte, sauf sur un élément verrouillé."""
    r = lire(REGISTRE, {})
    r.setdefault("elements", {})
    for svc in plan().get("services", []):
        for el in svc.get("elements", []):
            regle = regle_du_plan(el)
            if regle and not r["elements"].get(el["id"], {}).get("verrou"):
                r["elements"][el["id"]] = {**regle, "service": svc.get("id")}
    return r


# ---------------------------------------------------------------- inventaire

def tailles_du(racine, profondeur):
    p = subprocess.run(["du", "-x", "-B1", f"--max-depth={profondeur}", racine],
                       capture_output=True, text=True)
    res = {}
    for ligne in p.stdout.splitlines():
        t, _, chemin = ligne.partition("\t")
        if t.isdigit():
            res[chemin] = int(t)
    return res


def derniere_modif(chemin, limite=20000):
    """mtime la plus récente sous chemin (échantillon borné pour rester rapide)."""
    plus = 0
    n = 0
    for base, dirs, fichiers in os.walk(chemin):
        for f in fichiers:
            try:
                plus = max(plus, os.lstat(os.path.join(base, f)).st_mtime)
            except OSError:
                pass
            n += 1
            if n > limite:
                return plus
    return plus


def touche_depuis(chemin, limite):
    """Vrai si un fichier sous chemin (ou chemin lui-même) a été modifié après limite (sortie au premier trouvé)."""
    try:
        if os.lstat(chemin).st_mtime >= limite:
            return True
    except OSError:
        return False
    if not os.path.isdir(chemin) or os.path.islink(chemin):
        return False
    for base, dirs, fichiers in os.walk(chemin):
        for f in dirs + fichiers:
            try:
                if os.lstat(os.path.join(base, f)).st_mtime >= limite:
                    return True
            except OSError:
                pass
    return False


def docker_inutilise():
    try:
        p = subprocess.run(["docker", "system", "df", "--format", "{{json .}}"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    for ligne in p.stdout.splitlines():
        d = json.loads(ligne)
        if d.get("Type") == "Images":
            m = re.match(r"([\d.]+)\s*([kMG]?B)", d.get("Reclaimable", "0B"))
            mult = {"B": 1, "kB": 1e3, "MB": 1e6, "GB": 1e9}
            return {"taille": int(float(m.group(1)) * mult[m.group(2)]) if m else 0, "total": d.get("Size")}
    return {"taille": 0}


def inventaire(_a=None):
    precedent = lire(os.path.join(ETAT, "inventaire.json"), {})
    avant = {e["id"]: e for e in precedent.get("elements", [])}
    epoch_avant = precedent.get("epoch", time.time())
    elements = []
    for racine, prof in RACINES:
        if not os.path.isdir(racine):
            continue
        t = tailles_du(racine, prof)
        enfants = [c for c in t if c != racine] if prof else [racine]
        for c in enfants:
            if t.get(c, 0) < SEUIL_LISTE or c.startswith(("/var/lib/containerd", "/var/lib/docker")):
                continue  # Docker : couvert par l'élément « images Docker non utilisées »
            liste = [c]
            if t[c] >= SEUIL_DETAIL and os.path.isdir(c):
                sous = tailles_du(c, 1)
                liste += [s for s in sous if s != c and sous[s] >= SEUIL_SOUS]
                t.update(sous)
            for chemin in liste:
                elements.append({"id": chemin, "type": "dossier", "chemin": chemin, "parent": c if chemin != c else "",
                                 "taille": t[chemin], "modif": int(derniere_modif(chemin)) if os.path.isdir(chemin) else int(os.path.getmtime(chemin))})
    d = docker_inutilise()
    if d is not None:
        elements.append({"id": "docker:images-inutilisees", "type": "docker", "chemin": "images Docker non utilisées",
                         "taille": d["taille"], "modif": 0})
    now = time.time()
    jours = (now - epoch_avant) / 86400
    for e in elements:
        if e["type"] == "dossier":
            e["protege"] = interdit(e["chemin"])
        a = avant.get(e["id"])
        if a:
            e["taille_avant"] = a.get("taille", 0)
            e["croissance_jour"] = int((e["taille"] - a.get("taille", 0)) / jours) if jours >= 0.5 else a.get("croissance_jour", 0)
            e["vu_depuis"] = a.get("vu_depuis", a.get("date"))
        else:
            e["vu_depuis"] = maintenant().date().isoformat()
    disque = shutil.disk_usage("/")
    inv = {"date": maintenant().isoformat(), "epoch": now, "disque": {"total": disque.total, "utilise": disque.used, "libre": disque.free},
           "elements": elements}
    ecrire(os.path.join(ETAT, "inventaire.json"), inv)
    with open(os.path.join(ETAT, "historique.jsonl"), "a") as h:
        h.write(json.dumps({"date": inv["date"], "utilise": disque.used, "tailles": {e["id"]: e["taille"] for e in elements}}) + "\n")
    journal(f"inventaire : {len(elements)} éléments, disque {disque.used / disque.total * 100:.1f} %")
    return inv


# ---------------------------------------------------------------- garde-fous

def interdit(chemin):
    chemin = os.path.normpath(chemin)
    for m in INTERDITS:
        if fnmatch.fnmatch(chemin, m) or (m.endswith("*") and chemin.startswith(m[:-1].rstrip("/") + "/")):
            return f"chemin protégé ({m})"
    for m in INTERDITS_EXACTS_MOTIFS:
        if re.match(m, chemin):
            return "dossier personnel entier"
    return ""


def utilise_par(chemin):
    """Processus qui utilisent chemin (dossier courant, fichiers ouverts ou mappés, ligne de commande)."""
    chemin = os.path.normpath(chemin)
    def sous(p):
        return p == chemin or p.startswith(chemin + "/")
    trouves = []
    moi = {os.getpid(), os.getppid()}
    for pdir in glob.glob("/proc/[0-9]*"):
        pid = int(pdir.split("/")[2])
        if pid in moi:
            continue
        try:
            cmd = open(pdir + "/cmdline", "rb").read().decode("utf-8", "replace").replace("\0", " ")
        except OSError:
            continue
        motif = ""
        try:
            if sous(os.readlink(pdir + "/cwd")):
                motif = "dossier courant"
        except OSError:
            pass
        if not motif and (chemin + "/" in cmd or cmd.rstrip().endswith(chemin) or (chemin + " ") in cmd):
            motif = "ligne de commande"
        if not motif:
            try:
                for fd in os.listdir(pdir + "/fd"):
                    try:
                        if sous(os.readlink(f"{pdir}/fd/{fd}")):
                            motif = "fichier ouvert"
                            break
                    except OSError:
                        pass
            except OSError:
                pass
        if motif:
            trouves.append(f"{pid} ({motif}) {cmd[:80]}")
    return trouves


def cite_par(chemin):
    """Unités systemd et crontabs qui citent chemin (ils casseraient si on l'efface)."""
    trouves = []
    for f in glob.glob("/etc/systemd/system/*.service") + glob.glob("/etc/systemd/system/*.d/*.conf") + \
             glob.glob("/home/*/.config/systemd/user/*.service") + glob.glob("/var/spool/cron/crontabs/*") + \
             glob.glob("/etc/cron.d/*"):
        try:
            texte = open(f, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for ligne in texte.splitlines():
            if ligne.lstrip().startswith("#"):
                continue
            if re.search(re.escape(chemin) + r"(/|\s|$|['\"])", ligne):
                if f.endswith(".service"):
                    try:
                        etat = subprocess.run(["systemctl", "is-enabled", os.path.basename(f)],
                                              capture_output=True, text=True).stdout.strip()
                    except OSError:
                        etat = ""
                    if etat in ("disabled", "masked"):
                        continue
                trouves.append(f"{f} : {ligne.strip()[:100]}")
                break
    return trouves


# ---------------------------------------------------------------- archivage

def nom_archive(chemin):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", chemin.strip("/")) or "racine"


def archiver(chemins, nom, essai):
    """tar | zstd -> rclone, md5 du flux envoyé comparé au md5 de la destination. Rend (ok, distant, octets)."""
    jour = maintenant().date().isoformat()
    distant = f"{DRIVE}/{jour}/{nom}-{maintenant().strftime('%H%M%S')}.tar.zst"
    if essai:
        return True, distant, 0
    tar = subprocess.Popen(["nice", "-n", "19", "ionice", "-c3", "tar", "--ignore-failed-read", "-C", "/", "-cf", "-",
                            *[c.lstrip("/") for c in chemins]], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    zst = subprocess.Popen(["nice", "-n", "19", "zstd", "-T1", "-6", "-q", "-c"], stdin=tar.stdout, stdout=subprocess.PIPE)
    tar.stdout.close()
    rc = subprocess.Popen(["rclone", "rcat", distant], stdin=subprocess.PIPE)
    md5 = hashlib.md5()
    n = 0
    while True:
        bloc = zst.stdout.read(4 << 20)
        if not bloc:
            break
        md5.update(bloc)
        n += len(bloc)
        rc.stdin.write(bloc)
    rc.stdin.close()
    err_tar = tar.stderr.read().decode("utf-8", "replace")
    codes = (tar.wait(), zst.wait(), rc.wait())
    if codes[0] not in (0, 1) or codes[1] != 0 or codes[2] != 0:
        journal(f"ÉCHEC archive {distant} codes tar/zstd/rclone={codes} {err_tar[:200]}")
        return False, distant, n
    p = subprocess.run(["rclone", "md5sum", distant], capture_output=True, text=True)
    md5_distant = p.stdout.split()[0] if p.stdout.split() else ""
    if md5_distant != md5.hexdigest():
        journal(f"ÉCHEC vérification {distant} : md5 envoyé {md5.hexdigest()} ≠ destination {md5_distant or '?'}")
        return False, distant, n
    with open(os.path.join(ETAT, "archives.jsonl"), "a") as h:
        h.write(json.dumps({"date": maintenant().isoformat(), "distant": distant, "octets": n, "md5": md5_distant,
                            "chemins": chemins, "expire": (maintenant().date() + datetime.timedelta(days=RETENTION_JOURS)).isoformat(),
                            "restaurer": f"rclone cat {distant} | zstd -dc | tar -x -C /"}, ensure_ascii=False) + "\n")
    return True, distant, n


def effacer_chemins(chemins):
    restes = []
    for c in chemins:
        if os.path.isdir(c) and not os.path.islink(c):
            subprocess.run(["rm", "-rf", "--one-file-system", c], capture_output=True)
        else:
            try:
                os.remove(c)
            except OSError:
                pass
        if os.path.lexists(c):
            restes.append(c)
    return restes


def controler(chemin):
    """Raisons de refuser d'effacer chemin ('' = rien ne s'y oppose)."""
    r = interdit(chemin)
    if r:
        return r
    u = utilise_par(chemin)
    if u:
        return "utilisé par " + " ; ".join(u[:3])
    c = cite_par(chemin)
    if c:
        return "cité par " + " ; ".join(c[:3])
    return ""


def garde_dessous(eid, reg):
    """Premier élément marqué « garder » situé sous eid : effacer eid l'emporterait avec lui."""
    for k, v in reg["elements"].items():
        if v.get("regle") != "garder" and not v.get("verrou"):
            continue
        if k.startswith(eid.rstrip("/") + "/"):
            return k
    return ""


# ---------------------------------------------------------------- application des règles

def taille(chemins):
    p = subprocess.run(["du", "-xsc", "-B1", *chemins], capture_output=True, text=True)
    lignes = p.stdout.strip().splitlines()
    return int(lignes[-1].split()[0]) if lignes else 0


def appliquer_dossier(eid, regle, essai):
    chemin = eid
    if not os.path.lexists(chemin):
        return {"ok": True, "message": "déjà absent"}
    refus = controler(chemin)
    if regle["regle"] in ("effacer", "supprimer"):
        g = garde_dessous(eid, registre())
        if g:
            refus = refus or f"contient un élément marqué « garder » ({g})"
        if refus:
            return {"ok": False, "message": "refusé : " + refus}
        cibles = [chemin]
    else:  # periodique
        if interdit(chemin):
            return {"ok": False, "message": "refusé : " + interdit(chemin)}
        limite = time.time() - regle.get("jours", 30) * 86400
        reg = registre()
        cibles = []
        for nom in sorted(os.listdir(chemin)):
            c = os.path.join(chemin, nom)
            if not touche_depuis(c, limite) and not controler(c) and not garde_dessous(c, reg) \
                    and reg["elements"].get(c, {}).get("regle") != "garder":
                cibles.append(c)
        if not cibles:
            return {"ok": True, "message": f"rien d'intouché depuis {regle.get('jours', 30)} j"}
    octets = taille(cibles)
    if regle["regle"] == "supprimer" or (regle["regle"] == "periodique" and regle.get("archive") is False):
        if essai:
            return {"ok": True, "essai": True, "message": f"supprimerait définitivement {octets / 2**20:.0f} Mo, sans archive", "cibles": cibles[:20]}
        restes = effacer_chemins(cibles)
        msg = f"{octets / 2**20:.0f} Mo supprimés définitivement, sans archive"
        if restes:
            msg += f" ; restent (verrouillés ?) : {', '.join(restes[:5])}"
        return {"ok": True, "message": msg, "octets": octets}
    if essai:
        return {"ok": True, "essai": True, "message": f"effacerait {len(cibles)} entrée(s), {octets / 2**20:.0f} Mo, après archive",
                "cibles": cibles[:20]}
    ok, distant, n = archiver(cibles, nom_archive(chemin), essai)
    if not ok:
        return {"ok": False, "message": f"archive non vérifiée, rien effacé ({distant})"}
    restes = effacer_chemins(cibles)
    msg = f"{len(cibles)} entrée(s), {octets / 2**20:.0f} Mo archivés ({distant}, {n / 2**20:.0f} Mo) puis effacés"
    if restes:
        msg += f" ; restent (verrouillés ?) : {', '.join(restes[:5])}"
    return {"ok": True, "message": msg, "archive": distant, "octets": octets}


def appliquer(eid, essai, reg=None, source="décision"):
    reg = reg or registre()
    regle = reg["elements"].get(eid)
    if not regle:
        return {"ok": False, "message": "refusé : aucune règle pour cet élément dans le registre"}
    if regle.get("regle") == "garder" or regle.get("verrou"):
        return {"ok": False, "message": "refusé : élément marqué « garder »"}
    r = regle["regle"]
    if eid == "docker:images-inutilisees":
        if r not in ("effacer", "periodique", "supprimer"):
            return {"ok": False, "message": f"règle {r} invalide pour Docker"}
        if essai:
            res = {"ok": True, "essai": True, "message": "supprimerait les images Docker non utilisées"}
        else:
            p = subprocess.run(["docker", "image", "prune", "-a", "-f"], capture_output=True, text=True)
            res = {"ok": p.returncode == 0, "message": (p.stdout.strip().splitlines() or ["?"])[-1]}
    else:
        if r not in ("effacer", "supprimer", "periodique"):
            return {"ok": False, "message": f"règle {r} invalide pour un dossier"}
        res = appliquer_dossier(eid, regle, essai)
    journal(f"{'[à blanc] ' if res.get('essai') else ''}{source} {eid} ({r}) : {res['message']}")
    if not res.get("essai") and res.get("ok") and r in ("effacer", "supprimer"):
        retirer_de_inventaire(eid)
    if not res.get("essai") and res.get("ok"):
        etat = lire(os.path.join(ETAT, "derniers.json"), {})
        etat[eid] = {"date": maintenant().isoformat(), "message": res["message"]}
        ecrire(os.path.join(ETAT, "derniers.json"), etat)
    return res


def retirer_de_inventaire(eid):
    """Ce qui vient d'être effacé disparaît de la page tout de suite, sans attendre la nuit."""
    p = os.path.join(ETAT, "inventaire.json")
    inv = lire(p, {})
    if not eid.startswith("docker:") and os.path.lexists(eid):
        return
    inv["elements"] = [e for e in inv.get("elements", []) if e["id"] != eid and not e["id"].startswith(eid + "/")
                       or e["id"] == "docker:images-inutilisees"]
    d = shutil.disk_usage("/")
    inv["disque"] = {"total": d.total, "utilise": d.used, "libre": d.free}
    ecrire(p, inv)


def purger_archives(essai):
    limite = (maintenant().date() - datetime.timedelta(days=RETENTION_JOURS)).isoformat()
    p = subprocess.run(["rclone", "lsf", "--dirs-only", DRIVE], capture_output=True, text=True)
    for d in p.stdout.split():
        jour = d.rstrip("/")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", jour) and jour < limite:
            if essai:
                journal(f"[à blanc] supprimerait l'archive {DRIVE}/{jour} (plus de {RETENTION_JOURS} jours)")
            else:
                q = subprocess.run(["rclone", "purge", f"{DRIVE}/{jour}"], capture_output=True, text=True)
                journal(f"archive {DRIVE}/{jour} supprimée définitivement (plus de {RETENTION_JOURS} jours) : code {q.returncode}")


def entretien(essai):
    """Applique les règles échues. Les consignes du plan sont une décision explicite du propriétaire : elles s'appliquent pour de vrai."""
    reg = registre()
    derniers = lire(os.path.join(ETAT, "derniers.json"), {})
    resultats = []
    for eid, regle in reg["elements"].items():
        r = regle.get("regle")
        if regle.get("source") == "plan" and r in ("effacer", "supprimer"):
            fait = derniers.get(eid, {}).get("date", "")
            if fait and fait[:10] >= regle.get("maj", ""):
                continue  # consigne « une fois » déjà appliquée
            resultats.append((eid, appliquer(eid, essai, reg, "ménage")))
        elif r == "periodique":
            dernier = derniers.get(eid, {}).get("date", "")
            if dernier and (maintenant() - datetime.datetime.fromisoformat(dernier)).days < regle.get("jours", 30):
                continue
            resultats.append((eid, appliquer(eid, essai, reg, "ménage")))
    purger_archives(essai)
    journal(f"{'[à blanc] ' if essai else ''}entretien : {len(resultats)} action(s)")
    return resultats


def a_voir():
    """Plus hauts éléments de l'inventaire que le plan ne couvre pas : notés pour le prochain état des lieux."""
    ids = [e["id"] for s in plan().get("services", []) for e in s.get("elements", [])]
    inv = lire(os.path.join(ETAT, "inventaire.json"), {}).get("elements", [])
    par_id = {e["id"]: e for e in inv}

    def sous_id(a, b):
        return a == b or a.startswith(b.rstrip("/") + "/")
    res = []
    for e in inv:
        if e.get("protege"):
            continue
        if any(sous_id(e["id"], p) for p in ids):
            continue  # couvert (lui ou un dossier au-dessus)
        if any(sous_id(p, e["id"]) for p in ids):
            continue  # en partie couvert : ses enfants non couverts sont listés à sa place
        parent = par_id.get(e.get("parent"))
        if parent and not parent.get("protege") and not any(sous_id(p, parent["id"]) for p in ids):
            continue  # son parent est listé à sa place
        res.append({"id": e["id"], "taille": e.get("taille", 0), "type": e["type"], "chemin": e.get("chemin", "")})
    return sorted(res, key=lambda x: -x["taille"])


def signaler(texte):
    if not SIGNALER:
        journal("à signaler : " + texte.replace("\n", " / "))
        return
    subprocess.run([*SIGNALER, texte], capture_output=True, timeout=60)


def menage(essai=False):
    """Le ménage quotidien : inventaire, consignes du plan, éléments hors plan, journal."""
    inventaire()
    resultats = entretien(essai)
    actions = [{"id": eid, "ok": r.get("ok"), "message": r.get("message", "")} for eid, r in resultats
               if not r.get("message", "").startswith(("rien d'intouché", "déjà absent"))]
    bloques = [a for a in actions if not a["ok"]]
    voir = a_voir()
    if essai:
        return {"actions": actions, "a_voir": voir}
    ecrire(os.path.join(ETAT, "a-voir-vps.json"), {"date": maintenant().isoformat(), "elements": voir})
    etat = {"jour": time.strftime("%Y-%m-%d"), "ok": True, "date": maintenant().isoformat(), "actions": actions,
            "bloques": len(bloques), "a_voir": len(voir), "a_voir_octets": sum(v["taille"] for v in voir)}
    ecrire(os.path.join(ETAT, "menage-vps.json"), etat)
    with open(os.path.join(ETAT, "menages-vps.jsonl"), "a") as h:
        h.write(json.dumps(etat, ensure_ascii=False) + "\n")
    if bloques:
        signaler(f"Ménage : {len(bloques)} consigne(s) bloquée(s).\n" +
                 "\n".join(f"- {b['id']} : {b['message'][:200]}" for b in bloques[:8]))
    journal(f"ménage : {len(actions)} action(s), {len(bloques)} bloquée(s), {len(voir)} élément(s) hors plan")
    return etat


def main():
    ap = argparse.ArgumentParser(description="Le seul outil qui efface sur le serveur.")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("inventaire")
    a = sp.add_parser("appliquer")
    a.add_argument("id")
    a.add_argument("--essai", action="store_true")
    e = sp.add_parser("entretien")
    e.add_argument("--essai", action="store_true")
    mn = sp.add_parser("menage")
    mn.add_argument("--essai", action="store_true")
    sp.add_parser("etat")
    args = ap.parse_args()
    if os.geteuid() != 0:
        sys.exit("nettoyer : à lancer en root (sudo nettoyer …)")
    os.makedirs(ETAT, exist_ok=True)
    if args.cmd == "inventaire":
        inventaire()
    elif args.cmd == "appliquer":
        res = appliquer(args.id, args.essai)
        print(json.dumps(res, ensure_ascii=False))
        sys.exit(0 if res["ok"] else 1)
    elif args.cmd == "entretien":
        entretien(args.essai)
    elif args.cmd == "menage":
        print(json.dumps(menage(args.essai), ensure_ascii=False, indent=1))
    elif args.cmd == "etat":
        print(json.dumps({"registre": registre(), "inventaire": lire(os.path.join(ETAT, "inventaire.json"), {})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
