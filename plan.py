#!/usr/bin/env python3
"""plan.py — écrit le plan de ménage du serveur, pendant un état des lieux avec son propriétaire.

Le plan (plan-vps.json, dans le dossier `plans` des réglages) range le serveur par SERVICE : à quoi il
sert, s'il est vivant, en pause ou fini, et pour chaque élément qu'il occupe une consigne. Le ménage
quotidien (`nettoyer menage`, root) n'applique que ce plan. Ce script n'efface rien.

  plan.py voir
  plan.py service   --id ID --nom NOM --role TEXTE --etat vivant|en-pause|fini --citation "mots du propriétaire"
  plan.py element   --service ID --chemin CHEMIN --quoi TEXTE --consigne garder|archiver|supprimer|vider
                    [--jours N] [--archive oui|non] --citation "mots du propriétaire"
  plan.py retirer   --chemin CHEMIN
  plan.py a-voir    ce que l'inventaire montre et que le plan ne couvre pas encore
  plan.py essai     ce que le prochain ménage ferait (à blanc, sudo)

Consignes :
  garder      on n'y touche jamais, ni lui ni ce qu'il contient, sauf un sous-élément qui a sa propre
              consigne (le plus précis l'emporte : on garde le projet, on vide son cache)
  archiver    une fois : archive vérifiée (rclone), puis effacé
  supprimer   une fois : effacé sans archive (à réserver à ce qui se recrée tout seul)
  vider       tous les N jours : ce qui n'a pas été touché depuis N jours est effacé (avec archive si --archive oui) ;
              pour les images Docker inutilisées : vidées tous les N jours
"""
import argparse, datetime, importlib.machinery, importlib.util, json, os, subprocess, sys

ICI = os.path.dirname(os.path.abspath(__file__))
CONSIGNES = {"garder", "archiver", "supprimer", "vider"}
ETATS = {"vivant", "en-pause", "fini"}

_src = os.environ.get("NETTOYER_PY") or (os.path.join(ICI, "nettoyer.py") if os.path.exists(os.path.join(ICI, "nettoyer.py"))
                                           else "/usr/local/sbin/nettoyer")
_spec = importlib.util.spec_from_loader("nettoyer", importlib.machinery.SourceFileLoader("nettoyer", _src))
N = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(N)  # pour les mêmes chemins protégés que l'outil d'effacement


PLANS = N.PLANS
NETTOYER = os.environ.get("NETTOYER_CMD", "/usr/local/sbin/nettoyer")


def chemin_plan(m):
    return os.path.join(PLANS, f"plan-{m}.json")


def lire(m):
    try:
        return json.load(open(chemin_plan(m), encoding="utf-8"))
    except (OSError, ValueError):
        return {"machine": m, "maj": "", "services": []}


def ecrire(m, plan):
    os.makedirs(PLANS, exist_ok=True)
    plan["maj"] = datetime.datetime.now().isoformat(timespec="minutes")
    tmp = chemin_plan(m) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as h:
        json.dump(plan, h, ensure_ascii=False, indent=1)
    os.replace(tmp, chemin_plan(m))


def ident(m, chemin):
    """Identifiant de l'élément pour l'outil d'effacement."""
    if chemin.startswith("docker:"):
        return chemin
    return os.path.normpath(chemin)


def protege(m, eid):
    if eid == "docker:images-inutilisees":
        return ""
    return N.interdit(eid)


def a_voir(m):
    return N.a_voir()  # même calcul que le ménage quotidien


def main():
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    for c in ("voir", "a-voir", "essai"):
        sp.add_parser(c)
    s = sp.add_parser("service")
    for k in ("--id", "--nom", "--role", "--citation"):
        s.add_argument(k, required=True)
    s.add_argument("--etat", required=True, choices=sorted(ETATS))
    e = sp.add_parser("element")
    for k in ("--service", "--chemin", "--quoi", "--citation"):
        e.add_argument(k, required=True)
    e.add_argument("--consigne", required=True, choices=sorted(CONSIGNES))
    e.add_argument("--jours", type=int)
    e.add_argument("--archive", choices=["oui", "non"], default="oui")
    r = sp.add_parser("retirer")
    r.add_argument("--chemin", required=True)
    a = ap.parse_args()
    m = "vps"
    plan = lire(m)
    jour = datetime.date.today().isoformat()

    if a.cmd == "voir":
        print(json.dumps(plan, ensure_ascii=False, indent=1))
    elif a.cmd == "a-voir":
        for x in a_voir(m):
            print(f"{x['taille'] / 1e9:7.1f} Go  {x['id']}")
    elif a.cmd == "essai":
        cmd = [NETTOYER, "menage", "--essai"]
        sys.exit(subprocess.run(cmd if os.geteuid() == 0 else ["sudo"] + cmd).returncode)
    elif a.cmd == "service":
        if not a.citation.strip():
            sys.exit("refusé : il faut les mots du propriétaire (--citation)")
        svc = next((x for x in plan["services"] if x["id"] == a.id), None)
        if not svc:
            svc = {"id": a.id, "elements": []}
            plan["services"].append(svc)
        svc.update({"nom": a.nom, "role": a.role, "etat": a.etat, "citation": a.citation, "maj": jour})
        ecrire(m, plan)
        print(f"service {a.id} : {a.etat}")
    elif a.cmd == "element":
        svc = next((x for x in plan["services"] if x["id"] == a.service), None)
        if not svc:
            sys.exit(f"refusé : service {a.service} inconnu (le créer d'abord)")
        eid = ident(m, a.chemin)
        if a.consigne != "garder":
            if not a.citation.strip():
                sys.exit("refusé : une consigne qui efface demande les mots du propriétaire (--citation)")
            if protege(m, eid):
                sys.exit(f"refusé : {protege(m, eid)}")
        if a.consigne == "vider" and not a.jours:
            sys.exit("refusé : « vider » demande --jours")
        for t in plan["services"]:  # un élément n'appartient qu'à un service
            t["elements"] = [x for x in t["elements"] if x["id"] != eid]
        el = {"id": eid, "quoi": a.quoi, "consigne": a.consigne, "citation": a.citation, "maj": jour}
        if a.consigne == "vider":
            el["jours"] = a.jours
            el["archive"] = a.archive == "oui"
        svc["elements"].append(el)
        ecrire(m, plan)
        print(f"{eid} : {a.consigne}" + (f" tous les {a.jours} j" if a.jours else ""))
    elif a.cmd == "retirer":
        eid = ident(m, a.chemin)
        for t in plan["services"]:
            t["elements"] = [x for x in t["elements"] if x["id"] != eid]
        ecrire(m, plan)
        print(f"{eid} retiré du plan")


if __name__ == "__main__":
    main()
