#!/bin/bash
# Menage quotidien (root, 02h) : inventaire, consignes du plan, hors-plan, journal.
# Pour limiter l'impact sur un petit serveur : lancez-le avec `lourd` (depot garde-fou-cpu-vps).
set -uo pipefail
/usr/local/sbin/nettoyer menage > /dev/null
