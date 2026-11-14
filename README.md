# Kinderkalender Berlin (Handy-App)

- `index.html`, `sw.js`, `manifest.json`, Icons: die App (installierbar, läuft offline)
- `suche.py` + `quellen.txt`: tägliche Terminsuche mit Gemini
- `.github/workflows/suche.yml`: startet die Suche jeden Morgen auf GitHub (kostenlos)
- `events.json`: das Ergebnis, das die App anzeigt

## Einrichten
1. Neues GitHub-Repo anlegen, alle Dateien hochladen (auch den Ordner `.github`).
2. Repo → Settings → Pages → Branch `main`, Ordner `/ (root)`.
3. Schlüssel kostenlos auf https://aistudio.google.com/apikey erzeugen.
4. Repo → Settings → Secrets and variables → Actions → New repository secret: Name `GEMINI_API_KEY`, Wert = der Schlüssel.
5. Reiter Actions → „Termine suchen“ → Run workflow (Testlauf).
6. Auf dem Handy die Pages-Adresse in Chrome öffnen → Menü → „App installieren“ / „Zum Startbildschirm“.
