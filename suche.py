"""Sucht Kinder-Termine für Berlin mit einer kostenlosen KI (Gemini) und schreibt events.json.

Läuft täglich in GitHub Actions (siehe .github/workflows/suche.yml), geht aber auch von Hand:
    GEMINI_API_KEY=... python suche.py

Ablauf
  1. Die Seiten aus quellen.txt werden geladen und als Text an Gemini gegeben, das daraus Termine zieht.
  2. Zusätzlich fragt Gemini per Google-Suche (Grounding) nach weiteren Terminen – klappt das im
     kostenlosen Tarif nicht, wird dieser Schritt übersprungen.
  3. Orte werden über OpenStreetMap (Nominatim) in Koordinaten umgewandelt; daraus folgt, ob ein
     Termin im S-Bahn-Ring oder in Pankow liegt.
  4. Das Ergebnis wird mit der bisherigen events.json zusammengeführt (damit „NEU“ stimmt).
"""
import difflib, json, math, os, re, sys, time, unicodedata, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

HIER = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("GEMINI_API_KEY", "").strip()
MODELLE = [m for m in os.environ.get("GEMINI_MODEL", "").split(",") if m.strip()] or \
          ["gemini-2.5-flash", "gemini-3.1-flash-lite", "gemini-2.5-flash-lite"]
PAUSE = float(os.environ.get("GEMINI_PAUSE", "7"))   # Sekunden zwischen KI-Aufrufen (Free-Tier-Limits)
MIT_SUCHE = os.environ.get("GROUNDING", "1") != "0"
HORIZONT = 28
KATEGORIEN = ["indoor", "outdoor", "musik", "kunst", "theater", "natur", "bewegung", "fest", "museum", "workshop"]
BERLIN = ZoneInfo("Europe/Berlin")
UA = "Mozilla/5.0 (compatible; Kinderkalender/1.0; +https://github.com)"

# Grobe Umrisse – dieselben wie auf der Karte in index.html
RING = [[52.5487,13.3885],[52.5492,13.4147],[52.5446,13.4271],[52.5402,13.4389],[52.5292,13.4554],[52.5236,13.4646],[52.5136,13.4752],[52.5031,13.4693],[52.4935,13.4616],[52.4728,13.4555],[52.4693,13.4428],[52.4673,13.4316],[52.4709,13.3857],[52.4755,13.3655],[52.4797,13.3524],[52.4782,13.3428],[52.4776,13.3285],[52.4802,13.3116],[52.4888,13.2996],[52.4964,13.2905],[52.5009,13.2836],[52.5076,13.2834],[52.5185,13.2843],[52.5306,13.2997],[52.5344,13.3290],[52.5364,13.3436],[52.5428,13.3660]]
PANKOW = [[52.5300,13.4000],[52.5290,13.4300],[52.5390,13.4600],[52.5530,13.4800],[52.5700,13.5000],[52.6000,13.5150],[52.6350,13.5050],[52.6700,13.4800],[52.6650,13.4150],[52.6400,13.3700],[52.6050,13.3350],[52.5800,13.3600],[52.5600,13.3800],[52.5480,13.3900],[52.5380,13.3920]]
PANKOW_ORTSTEILE = {"pankow", "prenzlauer berg", "weißensee", "weissensee", "niederschönhausen", "heinersdorf", "buch",
                    "französisch buchholz", "blankenburg", "karow", "rosenthal", "wilhelmsruh", "blankenfelde", "malchow", "wartenberg"}
# Ortsteile, die sicher im Ring liegen (nur als Notlösung, wenn keine Koordinaten da sind)
RING_SICHER = {"mitte", "tiergarten", "wedding", "moabit", "gesundbrunnen", "prenzlauer berg", "friedrichshain", "kreuzberg", "charlottenburg"}
# ungefähre Mittelpunkte zur Plausibilitätsprüfung der Koordinaten (Ortsteil -> lat, lng)
ZENTREN = {"prenzlauer berg": (52.540, 13.425), "pankow": (52.570, 13.403), "weißensee": (52.553, 13.460), "weissensee": (52.553, 13.460),
           "mitte": (52.522, 13.390), "tiergarten": (52.514, 13.350), "wedding": (52.550, 13.350), "moabit": (52.530, 13.340),
           "friedrichshain": (52.515, 13.455), "kreuzberg": (52.495, 13.405), "charlottenburg": (52.515, 13.300),
           "neukölln": (52.480, 13.440), "schöneberg": (52.480, 13.350), "tempelhof": (52.470, 13.390), "wilmersdorf": (52.490, 13.315),
           "treptow-köpenick": (52.450, 13.570), "malchow": (52.580, 13.490), "niederschönhausen": (52.600, 13.400)}
RING_PUFFER_KM = 1.2



def log(*a):
    print(*a, flush=True)


def im_polygon(lat, lng, poly):
    inside = False
    j = len(poly) - 1
    for i in range(len(poly)):
        yi, xi = poly[i]
        yj, xj = poly[j]
        if (xi > lng) != (xj > lng) and lat < (yj - yi) * (lng - xi) / (xj - xi) + yi:
            inside = not inside
        j = i
    return inside


def km(lat1, lng1, lat2, lng2):
    dy = (lat2 - lat1) * 110.57
    dx = (lng2 - lng1) * 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot(dx, dy)


def abstand_polygon(lat, lng, poly):
    """Kürzester Abstand (km) von einem Punkt zum Rand des Polygons."""
    best = 1e9
    for i in range(len(poly)):
        (a1, o1), (a2, o2) = poly[i], poly[(i + 1) % len(poly)]
        ky, kx = 110.57, 111.32 * math.cos(math.radians(lat))
        px, py = (lng - o1) * kx, (lat - a1) * ky
        sx, sy = (o2 - o1) * kx, (a2 - a1) * ky
        l2 = sx * sx + sy * sy
        t = 0 if l2 == 0 else max(0, min(1, (px * sx + py * sy) / l2))
        best = min(best, math.hypot(px - t * sx, py - t * sy))
    return best


def nah(lat, lng, poly, puffer):
    return im_polygon(lat, lng, poly) or abstand_polygon(lat, lng, poly) <= puffer


def plausibel(pos, bezirk):
    z = ZENTREN.get((bezirk or "").lower())
    return z is None or km(pos[0], pos[1], z[0], z[1]) <= 6.5


# ---------------------------------------------------------------- Gemini
class KiFehler(Exception):
    pass


_modell_idx = 0


def gemini(prompt, schema=None, suche=False):
    """Ein Aufruf an die Gemini-API. Probiert bei Limit/Unbekannt automatisch das nächste Modell."""
    global _modell_idx
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0.2}}
    if schema:
        body["generationConfig"]["responseMimeType"] = "application/json"
        body["generationConfig"]["responseSchema"] = schema
    if suche:
        body["tools"] = [{"google_search": {}}]
    letzter = ""
    while _modell_idx < len(MODELLE):
        modell = MODELLE[_modell_idx].strip()
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{modell}:generateContent"
        for versuch in range(4):
            req = urllib.request.Request(url, json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": KEY})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    j = json.load(r)
                time.sleep(PAUSE)
                return j
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")[:300]
                letzter = f"{modell}: HTTP {e.code} {text}"
                if e.code in (429, 500, 503) and versuch < 3:
                    time.sleep(20 * (versuch + 1))
                    continue
                break
            except (urllib.error.URLError, TimeoutError) as e:
                letzter = f"{modell}: {e}"
                time.sleep(10)
        if suche:                       # Suche nicht verfügbar: nicht das Modell wechseln, einfach melden
            raise KiFehler(letzter)
        log(f"  Modell {modell} geht nicht ({letzter[:120]}) – nächstes Modell")
        _modell_idx += 1
    raise KiFehler(letzter or "kein Modell erreichbar")


def antwort_text(j):
    try:
        teile = j["candidates"][0]["content"]["parts"]
    except (KeyError, IndexError, TypeError):
        return ""
    return "".join(t.get("text", "") for t in teile)


def quellen_aus_suche(j):
    out = []
    try:
        for c in j["candidates"][0]["groundingMetadata"]["groundingChunks"]:
            w = c.get("web", {})
            if w.get("uri"):
                out.append((w.get("title", ""), w["uri"]))
    except (KeyError, IndexError, TypeError):
        pass
    return out


EVENT_SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "titel": {"type": "STRING"},
            "datum": {"type": "STRING", "description": "Beginn, YYYY-MM-DD"},
            "bis": {"type": "STRING", "description": "Ende bei mehrtägigen Märkten/Festivals, YYYY-MM-DD, sonst leer"},
            "zeit": {"type": "STRING", "description": "z. B. 10:00 oder 12:00–19:00, leer wenn unbekannt"},
            "ort": {"type": "STRING", "description": "Name des Ortes, wenn möglich mit Straße"},
            "bezirk": {"type": "STRING", "description": "Berliner Ortsteil, z. B. Prenzlauer Berg; 'stadtweit' wenn an vielen Orten"},
            "kategorien": {"type": "ARRAY", "items": {"type": "STRING", "enum": KATEGORIEN}},
            "alter": {"type": "STRING", "description": "Altersangabe wie im Material, z. B. ab 3 Jahren"},
            "alterVon": {"type": "INTEGER", "description": "jüngstes geeignetes Alter in Jahren (0 für Babys), leer wenn unbekannt"},
            "alterBis": {"type": "INTEGER", "description": "ältestes geeignetes Alter in Jahren, leer wenn offen oder unbekannt"},
            "preis": {"type": "STRING"},
            "beschreibung": {"type": "STRING", "description": "ein bis zwei Sätze"},
            "link": {"type": "STRING", "description": "genau eine URL aus der Quelle, sonst leer"},
        },
        "required": ["titel", "datum", "ort", "bezirk", "kategorien"],
    },
}


def extrahiere(text, quelle_name, heute, ende):
    prompt = f"""Du pflegst einen Veranstaltungskalender für Eltern von Kindern im Alter von 0 bis 6 Jahren in Berlin.
Heute ist {heute.isoformat()}. Gesucht sind Termine vom {heute.isoformat()} bis {ende.isoformat()}.

Ziehe aus dem folgenden Material alle passenden Termine heraus. Regeln:
- Nur Angebote, die Kinder von 0–6 Jahren (auch mit Eltern) wirklich nutzen können: Theater, Konzerte, Lesungen, Basteln, Spielgruppen, Feste, Weihnachtsmärkte mit Kinderprogramm, Museen, Kurse, Kino für Babys usw.
- Weglassen: Angebote ab 7 Jahren, Fachveranstaltungen, Flohmärkte und Feste ohne erkennbaren Kinderbezug, Veranstaltungen für Erwachsene.
- alterVon/alterBis als Zahlen füllen, wenn das Material ein Alter nennt. Steht dort "für alle" oder "Familien", beides leer lassen.
- Nur Berlin. Nichts erfinden: Wenn Datum oder Ort nicht im Material stehen, den Termin weglassen.
- Datum immer als YYYY-MM-DD. Jahreszahl ergänzen, falls sie fehlt (Termine liegen im Zeitraum oben).
- Mehrtägige Märkte/Festivals: ein Eintrag, datum = erster Tag (oder heute, falls schon laufend), bis = letzter Tag.
- Regelmäßige Termine (z. B. wöchentlich): je ein Eintrag pro Datum im Zeitraum, höchstens 5.
- kategorien: 1–3 passende Werte (indoor/outdoor plus Themenkategorie).
- link: bevorzugt die Seite genau dieses Termins; nur eine URL, die wörtlich im Material vorkommt, sonst leer lassen.
- Texte auf Deutsch, beschreibung kurz und sachlich. Unbekannte Felder leer lassen.

Quelle: {quelle_name}

Material:
{text}"""
    j = gemini(prompt, schema=EVENT_SCHEMA)
    try:
        return json.loads(antwort_text(j) or "[]")
    except json.JSONDecodeError:
        log("  Antwort war kein gültiges JSON – übersprungen")
        return []


# ---------------------------------------------------------------- Seiten laden
class TextSammler(HTMLParser):
    def __init__(self, basis):
        super().__init__(convert_charrefs=True)
        self.basis, self.teile, self.anker, self.skip, self.href, self.atext = basis, [], {}, 0, None, []

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "svg", "head"):
            self.skip += 1
        elif tag == "a":
            h = dict(attrs).get("href")
            if h and not h.startswith(("#", "mailto:", "javascript:")):
                self.href = urllib.parse.urljoin(self.basis, h)
                self.atext = []
                self.anker.setdefault(self.href, "")
                self.teile.append("[")
        elif tag in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "article", "section", "tr"):
            self.teile.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "svg", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag == "a" and self.href:
            self.teile.append(f"]({self.href})")
            t = " ".join(self.atext).strip()
            if len(t) > len(self.anker.get(self.href, "")):
                self.anker[self.href] = t
            self.href = None

    def handle_data(self, d):
        if not self.skip and d.strip():
            self.teile.append(d.strip() + " ")
            if self.href:
                self.atext.append(d.strip())


def lade_seite(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "de"})
    with urllib.request.urlopen(req, timeout=40) as r:
        html = r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
    p = TextSammler(url)
    p.feed(html)
    text = re.sub(r"\n\s*\n+", "\n", "".join(p.teile))
    return text[:60000], p.anker


def quellenliste():
    pfad = os.path.join(HIER, "quellen.txt")
    if not os.path.exists(pfad):
        return []
    with open(pfad, encoding="utf-8") as f:
        return [z.strip() for z in f if z.strip() and not z.startswith("#")]


# ---------------------------------------------------------------- Orte
_geo = {}
_geo_pfad = os.path.join(HIER, "geocache.json")
if os.path.exists(_geo_pfad):
    with open(_geo_pfad, encoding="utf-8") as f:
        _geo = json.load(f)


def geocode(ort, bezirk):
    if not ort or bezirk.lower() == "stadtweit" or "viele orte" in ort.lower():
        return None
    k = f"{ort}|{bezirk}".lower()
    if k in _geo:
        return _geo[k]
    ergebnis = None
    for q in (f"{ort}, {bezirk}, Berlin", f"{ort}, Berlin"):
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode({"q": q, "format": "json", "limit": 1, "countrycodes": "de"})
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=30) as r:
                treffer = json.load(r)
        except Exception as e:
            log(f"  Geocoding fehlgeschlagen für {ort}: {e}")
            treffer = []
        time.sleep(1.2)                 # Nominatim erlaubt höchstens 1 Anfrage pro Sekunde
        if treffer:
            la, ln = float(treffer[0]["lat"]), float(treffer[0]["lon"])
            if 52.3 < la < 52.7 and 13.0 < ln < 13.8:
                ergebnis = [round(la, 4), round(ln, 4)]
                break
    _geo[k] = ergebnis
    return ergebnis


def gebiet(e):
    """Setzt imRing/pankow; gibt False zurück, wenn der Termin in keinem der beiden Gebiete liegt."""
    bez = (e.get("bezirk") or "").lower()
    if bez == "stadtweit":
        e["imRing"], e["pankow"] = True, True
        return True
    if "lat" in e:
        e["imRing"] = nah(e["lat"], e["lng"], RING, RING_PUFFER_KM)
        e["pankow"] = nah(e["lat"], e["lng"], PANKOW, 0.5) or bez in PANKOW_ORTSTEILE
    else:                                # ohne Koordinaten nur sichere Ortsteile
        e["imRing"] = bez in RING_SICHER
        e["pankow"] = bez in PANKOW_ORTSTEILE
    return e["imRing"] or e["pankow"]


# ---------------------------------------------------------------- Aufräumen & Zusammenführen
def slug(s, n=40):
    s = unicodedata.normalize("NFKD", s.lower().replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue"))
    return re.sub(r"[^a-z0-9]+", "-", s.encode("ascii", "ignore").decode()).strip("-")[:n]


def zahl(x):
    return x if isinstance(x, int) and not isinstance(x, bool) else None


def alter_normieren(e):
    """Vereinheitlicht e['alter'] und setzt alterVon/alterBis (Zahlen, wenn erkennbar)."""
    t = (e.get("alter") or "").lower().replace("–", "-")
    von, bis = zahl(e.get("alterVon")), zahl(e.get("alterBis"))
    if von is None and bis is None:
        m = re.search(r"(\d+)\s*-\s*(\d+)", t)
        if m:
            von, bis = int(m.group(1)), int(m.group(2))
        elif re.search(r"ab\s*(\d+)", t):
            von = int(re.search(r"ab\s*(\d+)", t).group(1))
        elif "baby" in t or "babys" in t:
            von, bis = 0, 1
        elif "kleinkind" in t:
            von, bis = 1, 3
        elif "kita" in t:
            von, bis = 3, 6
    if bis is not None and bis >= 18 and (von or 0) <= 1:     # "0-99": gilt für alle
        e["alter"], von, bis = "Familien", None, None
    elif von is not None and bis is not None:
        e["alter"] = f"{von}–{bis} Jahre"
    elif von is not None:
        e["alter"] = f"ab {von} Jahren"
    elif re.fullmatch(r"[\d\s\-jJahre]*", e.get("alter") or ""):
        e["alter"] = ""
    for k, v in (("alterVon", von), ("alterBis", bis)):
        if v is None:
            e.pop(k, None)
        else:
            e[k] = v
    return von is None or von <= 6


def tokens(titel):
    return {w for w in slug(titel, 80).split("-") if len(w) >= 4}


def aehnlich(a, b):
    gleich_tag = a["datum"] == b["datum"]
    if not gleich_tag and not (a.get("bis") or b.get("bis")):
        return False
    sa, sb = slug(a["titel"], 80), slug(b["titel"], 80)
    if min(len(sa), len(sb)) >= 8 and (sa in sb or sb in sa):
        return True
    if difflib.SequenceMatcher(None, sa, sb).ratio() >= (0.72 if gleich_tag else 0.85):
        return True
    if gleich_tag and (not a.get("zeit") or not b.get("zeit") or a["zeit"] == b["zeit"]):
        ta, tb = tokens(a["titel"]), tokens(b["titel"])
        gem = len(ta & tb)
        return gem >= 2 and gem / max(1, min(len(ta), len(tb))) >= 0.6
    return False


def reicher(e):
    return (sum(1 for k in ("preis", "zeit", "alter", "beschreibung") if e.get(k)) + (2 if "lat" in e else 0)
            + (1 if (e.get("link") or "").count("/") >= 5 else 0))


def beste_verlinkung(titel, anker):
    """Sucht auf der Quellenseite den Link, dessen Text am besten zum Titel passt."""
    st, best, bl = slug(titel, 80), 0.0, ""
    for href, text in anker.items():
        if not text:
            continue
        r = difflib.SequenceMatcher(None, st, slug(text, 80)).ratio()
        if r > best:
            best, bl = r, href
    return bl if best >= 0.6 else ""


def gueltiges_datum(s):
    try:
        return datetime.strptime(s or "", "%Y-%m-%d").date()
    except ValueError:
        return None


def bereinige(roh, quelle_name, anker, fallback_link, heute, ende, jetzt):
    out = []
    for r in roh:
        if not isinstance(r, dict):
            continue
        titel = (r.get("titel") or "").strip()
        d = gueltiges_datum(r.get("datum"))
        if not titel or not d:
            continue
        b = gueltiges_datum(r.get("bis"))
        if b and b <= d:
            b = None
        if (b or d) < heute or d > ende:
            continue
        e = {
            "titel": titel,
            "datum": d.isoformat(),
            "zeit": (r.get("zeit") or "").strip(),
            "ort": (r.get("ort") or "").strip(),
            "bezirk": (r.get("bezirk") or "").strip() or "Berlin",
            "kategorien": [k for k in (r.get("kategorien") or []) if k in KATEGORIEN][:3] or ["indoor"],
            "alter": (r.get("alter") or "").strip(),
            "alterVon": r.get("alterVon"),
            "alterBis": r.get("alterBis"),
            "preis": (r.get("preis") or "").strip(),
            "beschreibung": (r.get("beschreibung") or "").strip(),
            "quelle": quelle_name,
            "gefunden": jetzt,
        }
        if b:
            e["bis"] = b.isoformat()
        if not alter_normieren(e):          # ab 7 Jahren: nicht für diese App
            continue
        link = (r.get("link") or "").strip()
        e["link"] = link if link in anker else (beste_verlinkung(titel, anker) or fallback_link)
        out.append(e)
    return out


def main():
    if not KEY:
        sys.exit("GEMINI_API_KEY fehlt (kostenlos auf https://aistudio.google.com/apikey erzeugen).")
    jetzt_dt = datetime.now(BERLIN)
    jetzt = jetzt_dt.isoformat(timespec="seconds")
    heute = jetzt_dt.date()
    ende = heute + timedelta(days=HORIZONT - 1)
    gefunden = []

    for url in quellenliste():
        name = urllib.parse.urlparse(url).netloc.replace("www.", "")
        log(f"Quelle: {url}")
        try:
            text, anker = lade_seite(url)
        except Exception as e:
            log(f"  nicht ladbar: {e}")
            continue
        if len(text) < 300:
            log("  kaum Text (Seite braucht evtl. JavaScript) – übersprungen")
            continue
        try:
            roh = extrahiere(text, url, heute, ende)
        except KiFehler as e:
            log(f"  KI-Fehler: {e}")
            continue
        neu = bereinige(roh, name, anker, url, heute, ende, jetzt)
        log(f"  {len(neu)} Termine")
        gefunden += neu

    if MIT_SUCHE:
        fragen = [
            "Kinder- und Familienveranstaltungen für Kleinkinder (0–6 Jahre) in Berlin Prenzlauer Berg, Pankow und Weißensee",
            "Kindertheater, Kinderkonzerte, Babykino und Mitmachangebote für Kleinkinder in Berlin Mitte, Friedrichshain, Kreuzberg und Charlottenburg",
            "Weihnachtsmärkte, Adventsfeste und Feste mit Kinderprogramm in Berlin innerhalb des S-Bahn-Rings und in Pankow",
        ]
        for i, f in enumerate(fragen, 1):
            log(f"Suche {i}/{len(fragen)}")
            try:
                j = gemini(f"Suche mit Google nach konkreten Terminen zwischen {heute.isoformat()} und {ende.isoformat()}: {f}. "
                           "Nenne für jeden Termin Titel, Datum, Uhrzeit, Ort mit Adresse, Altersangabe, Preis und die Webseite.", suche=True)
            except KiFehler as e:
                log(f"  Suche nicht verfügbar, wird übersprungen: {str(e)[:150]}")
                break
            quellen = quellen_aus_suche(j)
            text = antwort_text(j) + "\n\nQuellen:\n" + "\n".join(f"[{t}]({u})" for t, u in quellen)
            try:
                roh = extrahiere(text, "Google-Suche", heute, ende)
            except KiFehler as e:
                log(f"  KI-Fehler: {e}")
                continue
            neu = bereinige(roh, "Google-Suche", {u: t for t, u in quellen}, "", heute, ende, jetzt)
            log(f"  {len(neu)} Termine")
            gefunden += neu

    # Koordinaten & Gebiet für neue Termine
    neu_liste, verworfen = [], 0
    for e in gefunden:
        pos = geocode(e["ort"], e["bezirk"])
        if pos and not plausibel(pos, e["bezirk"]):
            log(f"  Koordinaten passen nicht zu {e['bezirk']}: {e['ort']} – ignoriert")
            pos = None
        if pos:
            e["lat"], e["lng"] = pos
        if gebiet(e):
            neu_liste.append(e)
        else:
            verworfen += 1
    log(f"{len(neu_liste)} neue Termine im Ring/Pankow ({verworfen} außerhalb verworfen)")
    if not neu_liste:
        sys.exit("Keine Termine gefunden – events.json bleibt unverändert.")

    # Bisherige Termine prüfen (mit den heutigen, strengeren Regeln)
    pfad = os.path.join(HIER, "events.json")
    alt = []
    if os.path.exists(pfad):
        with open(pfad, encoding="utf-8") as f:
            alt = json.load(f).get("events", [])
    alt_ok = []
    for e in alt:
        if not (e.get("titel") and e.get("datum")) or (e.get("bis") or e["datum"]) < heute.isoformat():
            continue
        if "lat" in e and not plausibel((e["lat"], e["lng"]), e.get("bezirk")):
            e.pop("lat"); e.pop("lng", None)
        if alter_normieren(e) and gebiet(e):
            alt_ok.append(e)

    # Zusammenführen, ähnliche Termine zu einem machen (der vollständigere gewinnt)
    ergebnis = []
    for e in alt_ok + neu_liste:
        for i, o in enumerate(ergebnis):
            if aehnlich(o, e):
                aelter = o if o.get("gefunden", "") <= e.get("gefunden", "") else e
                sieger, rest = (e, o) if reicher(e) > reicher(o) else (o, e)
                for k, v in rest.items():
                    if k not in ("gefunden", "id", "imRing", "pankow") and not sieger.get(k):
                        sieger[k] = v
                sieger["gefunden"] = aelter.get("gefunden", sieger.get("gefunden"))
                if aelter.get("id"):
                    sieger["id"] = aelter["id"]
                gebiet(sieger)
                ergebnis[i] = sieger
                break
        else:
            ergebnis.append(e)
    events = sorted(ergebnis, key=lambda e: (e["datum"], e.get("zeit") or "~", e["titel"]))
    for e in events:
        e["id"] = e.get("id") or f"{e['datum']}-{slug(e['titel'])}"
        for leer in ("bis", "lat", "lng", "link", "alterVon", "alterBis"):
            if e.get(leer) in ("", None):
                e.pop(leer, None)

    with open(pfad, "w", encoding="utf-8") as f:
        json.dump({"status": {"letzteSuche": jetzt, "horizontTage": HORIZONT}, "events": events}, f, ensure_ascii=False, indent=2)
    with open(_geo_pfad, "w", encoding="utf-8") as f:
        json.dump(_geo, f, ensure_ascii=False, indent=1, sort_keys=True)
    log(f"events.json geschrieben: {len(events)} Termine")


if __name__ == "__main__":
    main()
