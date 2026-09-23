#!/usr/bin/env python3
"""
Scraper für FC Lionem Zürich, Junioren C 1. Staerkeklasse (FVRZ-Matchcenter).

Holt:
  - Tabelle der Gruppe
  - Spielplan (kommende + aktuelle Spiele)
  - Torschuetzen (aggregiert aus den Spielberichten / "Telegramm"-Seiten)

Schreibt das Ergebnis nach data.json im selben Ordner. Wird als "played"
markierte Spiele (mit Resultat) einmalig ausgewertet und die Torschuetzen
werden in processed_telegrams.json gemerkt, damit nicht jede Stunde
dieselben Spielberichte nochmal geparst werden.

WICHTIG: FVRZ kann die HTML-Struktur jederzeit aendern. Dieser Scraper
arbeitet mit Regex auf dem sichtbaren Text der Seite (robuster gegen
CSS-Aenderungen, aber nicht unfehlbar). Wenn nach einem Layout-Wechsel
nichts mehr gefunden wird: DEBUG=1 setzen, das schreibt den rohen Text
jeder abgerufenen Seite nach debug_*.txt, damit man die Muster anpassen
kann.
"""

import os
import re
import json
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# ---- Konfiguration: bei Bedarf anpassen -----------------------------------
BASE = "https://matchcenter.fvrz.ch/default.aspx"
CLUB_PARAMS = {"v": "822598", "oid": "11", "lng": "1"}
TEAM_ID = "74908"                       # Junioren C 1. Staerkeklasse a
GROUP_HINT = "Junioren C 1"             # Teilstring zur Gruppen-Erkennung
OUR_TEAM_FULL = "FC Lionem ZH a"
OUR_TEAM_SHORT = "Lionem"               # taucht in den Torschuetzen-Zeilen so auf

DATA_FILE = os.path.join(os.path.dirname(__file__), "data.json")
PROCESSED_FILE = os.path.join(os.path.dirname(__file__), "processed_telegrams.json")
DEBUG = os.environ.get("DEBUG") == "1"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
    "Referer": "https://matchcenter.fvrz.ch/",
}

DAY_RE = r"(?:Mo|Di|Mi|Do|Fr|Sa|So)"
DATE_RE = re.compile(rf"^{DAY_RE}\s+(\d{{2}}\.\d{{2}}\.\d{{4}})$")
TIME_RE = re.compile(r"^(\d{2}:\d{2})$")


def fetch(url, params=None, tag=""):
    r = requests.get(url, params=params, headers=HEADERS, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    if DEBUG and tag:
        with open(f"debug_{tag}.txt", "w", encoding="utf-8") as f:
            f.write(soup.get_text("\n"))
    return soup


def club_url(**extra):
    p = dict(CLUB_PARAMS)
    p.update(extra)
    return BASE, p


# ---- Tabelle ----------------------------------------------------------------
def parse_standings(soup):
    """Findet die Tabelle, deren vorausgehende Ueberschrift GROUP_HINT enthaelt."""
    standings = []
    for table in soup.find_all("table"):
        heading = table.find_previous(string=re.compile(GROUP_HINT))
        if not heading:
            continue
        rows = table.find_all("tr")
        parsed = []
        for tr in rows:
            cells = [c.get_text(strip=True) for c in tr.find_all(["td", "th"])]
            cells = [c for c in cells if c != ""]
            if len(cells) < 8:
                continue
            # erste Zelle sollte ein Rang wie "1." oder "1" sein
            if not re.match(r"^\d+\.?$", cells[0]):
                continue
            try:
                rank = int(cells[0].rstrip("."))
                team = cells[1]
                played, won, draw, lost = (int(x) for x in cells[2:6])
                # Tore-Spalte kann "54:3" oder "54", ":", "3" sein
                goals_txt = "".join(cells[6:9])
                m = re.search(r"(\d+)\D+(\d+)", goals_txt)
                gf, ga = (int(m.group(1)), int(m.group(2))) if m else (None, None)
                points = int(re.sub(r"\D", "", cells[-1]))
                parsed.append({
                    "rank": rank, "team": team, "played": played,
                    "won": won, "draw": draw, "lost": lost,
                    "goals_for": gf, "goals_against": ga,
                    "points": points,
                    "is_us": OUR_TEAM_SHORT.lower() in team.lower(),
                })
            except (ValueError, IndexError):
                continue
        if parsed:
            standings = parsed
            break  # erste passende Tabelle nehmen
    return standings


# ---- Spielplan (Text-basiert) ------------------------------------------------
def parse_fixtures_from_text(text):
    """
    Durchsucht den linearisierten Seitentext nach Bloecken der Form:
        Sa 19.09.2026
        15:30
        FC Wallisellen
        -
        FC Lionem ZH a
        Meisterschaft Junioren C 1. Staerkeklasse - ...
        Spielnummer 153549
        Sportzentrum - Platz 1, Wallisellen
    Ist toleranter gegenueber Leerzeilen / Reihenfolge-Abweichungen.
    """
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    fixtures = []
    current_date = None
    i = 0
    while i < len(lines):
        line = lines[i]
        d = DATE_RE.match(line)
        if d:
            current_date = d.group(1)
            i += 1
            continue
        t = TIME_RE.match(line)
        if t and current_date:
            # Sammle die naechsten ~8 Zeilen als Kontext fuer dieses Spiel
            block = lines[i:i + 10]
            block_text = " | ".join(block)
            if GROUP_HINT in block_text and OUR_TEAM_SHORT in block_text:
                home = block[1] if len(block) > 1 else ""
                away = block[3] if len(block) > 3 else ""
                score = None
                sm = re.search(r"\b(\d{1,2})\s*:\s*(\d{1,2})\b", block_text)
                if sm:
                    score = f"{sm.group(1)}:{sm.group(2)}"
                venue = None
                for cand in block[4:]:
                    if "," in cand and "Spielnummer" not in cand and "Meisterschaft" not in cand:
                        venue = cand
                        break
                fixtures.append({
                    "date": current_date, "time": t.group(1),
                    "home": home, "away": away,
                    "score": score, "played": score is not None,
                    "venue": venue,
                })
        i += 1
    # Duplikate (gleiche Zeit+Teams) entfernen
    seen = set()
    unique = []
    for f in fixtures:
        key = (f["date"], f["time"], f["home"], f["away"])
        if key not in seen:
            seen.add(key)
            unique.append(f)
    return unique


def find_telegram_links(soup):
    """Alle Links mit tg=... , die im Kontext von OUR_TEAM_SHORT stehen."""
    links = []
    for a in soup.find_all("a", href=True):
        if "tg=" in a["href"]:
            context = a.find_parent().get_text(" ", strip=True) if a.find_parent() else ""
            if OUR_TEAM_SHORT in context or OUR_TEAM_SHORT in a.get_text():
                full = a["href"]
                if full.startswith("/"):
                    full = "https://matchcenter.fvrz.ch" + full
                elif not full.startswith("http"):
                    full = "https://matchcenter.fvrz.ch/" + full
                links.append(full)
    return list(dict.fromkeys(links))


# ---- Torschuetzen aus einem Spielbericht ------------------------------------
def parse_scorers_from_telegram(soup):
    text = soup.get_text("\n")
    scorers = []
    # Muster wie in den Beispielen: "Tor Lionem ZH" gefolgt von "Torschuetze NAME"
    for m in re.finditer(
        rf"Tor\s+[^\n]*{OUR_TEAM_SHORT}[^\n]*\n+\s*Torsch[uü]tze\s+([^\n(]+)",
        text,
    ):
        name = m.group(1).strip()
        name = re.sub(r"\s*\(Penalty\)\s*$", "", name).strip()
        if name:
            scorers.append(name)
    return scorers


# ---- Hauptablauf --------------------------------------------------------------
def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def main():
    data = load_json(DATA_FILE, {
        "generated_at": None, "team": OUR_TEAM_FULL, "group": None,
        "standings": [], "fixtures": [], "topscorers": {},
    })
    processed = load_json(PROCESSED_FILE, [])

    url, params = club_url(t=TEAM_ID, a="rr")
    rr_soup = fetch(url, params, tag="rr")
    standings = parse_standings(rr_soup)
    if standings:
        data["standings"] = standings

    fixtures_all = {}
    for action in ("vs", "as", "rr"):
        u, p = club_url(a=action)
        s = fetch(u, p, tag=action)
        for fx in parse_fixtures_from_text(s.get_text("\n")):
            key = (fx["date"], fx["time"], fx["home"], fx["away"])
            fixtures_all[key] = fx
        for link in find_telegram_links(s):
            if link not in processed:
                try:
                    tg_soup = fetch(link, tag=None)
                    scorers = parse_scorers_from_telegram(tg_soup)
                    for name in scorers:
                        data["topscorers"][name] = data["topscorers"].get(name, 0) + 1
                    processed.append(link)
                    time.sleep(1)  # kein Dauerfeuer auf den Server
                except requests.RequestException:
                    pass

    data["fixtures"] = sorted(fixtures_all.values(), key=lambda f: (f["date"], f["time"]))
    data["generated_at"] = datetime.now(timezone.utc).isoformat()

    save_json(DATA_FILE, data)
    save_json(PROCESSED_FILE, processed)
    print(f"OK: {len(data['standings'])} Tabellenzeilen, "
          f"{len(data['fixtures'])} Spiele, "
          f"{len(data['topscorers'])} Torschuetzen erfasst.")


if __name__ == "__main__":
    main()
