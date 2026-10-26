#!/usr/bin/env python3
"""Page « Nettoyage » — lecture seule (par défaut http://127.0.0.1:7965).

Affiche le plan de ménage du serveur, le journal des derniers ménages et ce que l'inventaire montre
hors plan. On n'y décide rien : le plan se modifie avec plan.py pendant un état des lieux.
Pour l'ouvrir à distance, passez par un tunnel SSH ou un VPN ; elle n'a pas d'authentification.
"""
import json, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ICI = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.environ.get("NETTOYAGE_CONFIG", "/etc/nettoyage-serveur/config.json")
_cfg = {}
try:
    _cfg = json.load(open(CONFIG, encoding="utf-8"))
except (OSError, ValueError):
    pass
ETAT = os.environ.get("NETTOYAGE_ETAT", _cfg.get("etat", "/var/lib/nettoyage-serveur"))
PLANS = os.environ.get("NETTOYAGE_PLANS", _cfg.get("plans", "/etc/nettoyage-serveur/plans"))
INVENTAIRES = {"vps": os.path.join(ETAT, "inventaire.json")}
HOTE = os.environ.get("NETTOYAGE_HOTE", "127.0.0.1")
PORT = int(os.environ.get("NETTOYAGE_PORT", "7965"))


def lire(chemin, defaut):
    try:
        with open(chemin, encoding="utf-8") as h:
            return json.load(h)
    except (OSError, ValueError):
        return defaut


def derniers_menages(machine, n=10):
    try:
        lignes = open(os.path.join(ETAT, f"menages-{machine}.jsonl"), encoding="utf-8").read().splitlines()
    except OSError:
        return []
    res = []
    for l in reversed(lignes[-n:]):
        try:
            res.append(json.loads(l))
        except ValueError:
            pass
    return res


def machine(m):
    inv = lire(INVENTAIRES[m], {"elements": []})
    tailles = {e["id"]: e.get("taille", 0) for e in inv.get("elements", [])}
    plan = lire(os.path.join(PLANS, f"plan-{m}.json"), {"services": []})
    derniers = lire(os.path.join(ETAT, "derniers.json"), {})
    for s in plan.get("services", []):
        for el in s.get("elements", []):
            el["taille"] = tailles.get(el["id"])
            el["dernier"] = derniers.get(el["id"])
        s["taille"] = sum(el["taille"] or 0 for el in s.get("elements", []))
    return {"disque": inv.get("disque"), "date": inv.get("date"), "joignable": inv.get("joignable", True),
            "plan": plan, "menage": lire(os.path.join(ETAT, f"menage-{m}.json"), {}),
            "a_voir": lire(os.path.join(ETAT, f"a-voir-{m}.json"), {}), "journal": derniers_menages(m)}


class Page(BaseHTTPRequestHandler):
    def envoyer(self, code, corps, type_="application/json; charset=utf-8"):
        if not isinstance(corps, bytes):
            corps = json.dumps(corps, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", type_)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self.envoyer(200, open(os.path.join(ICI, "page", "index.html"), "rb").read(), "text/html; charset=utf-8")
        if self.path == "/style.css":
            return self.envoyer(200, open(os.path.join(ICI, "page", "style.css"), "rb").read(), "text/css")
        if self.path == "/api/etat":
            return self.envoyer(200, {"vps": machine("vps")})
        if self.path == "/sante":
            return self.envoyer(200, {"ok": True})
        self.envoyer(404, {"erreur": "introuvable"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((HOTE, PORT), Page).serve_forever()
