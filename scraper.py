#!/usr/bin/env python3
"""
UFC stats scraper -> ufc_data/fighters.json, events.json, fights.json
Output matches what ufc_api.py expects.

Usage:
    pip install requests beautifulsoup4
    python scraper.py --limit 3     # quick test (3 events, 3 fighters)
    python scraper.py               # full run (slow: ~13k pages, a couple of hours)

Pages are cached in .cache/, so an interrupted run resumes where it stopped.
Delete .cache/ to refresh data. Be polite: keep the delay, personal use only.
"""
import argparse
import hashlib
import json
import os
import re
import string
import time
from collections import defaultdict

import requests
from bs4 import BeautifulSoup

BASE = "http://ufcstats.com"
OUT_DIR = "ufc_data"
CACHE_DIR = ".cache"
DELAY = 0.6  # seconds between real requests

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------- helpers ----------
def get(url):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, hashlib.md5(url.encode()).hexdigest() + ".html")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return BeautifulSoup(f.read(), "html.parser")
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            break
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(2 * (attempt + 1))
    with open(path, "w", encoding="utf-8") as f:
        f.write(r.text)
    time.sleep(DELAY)
    return BeautifulSoup(r.text, "html.parser")


def txt(el):
    return " ".join(el.get_text(" ", strip=True).split()) if el else None


def clean(s):
    return None if s in (None, "", "--", "---") else s


def num(s):
    s = clean((s or "").strip().replace("%", ""))
    try:
        return float(s) if s is not None else None
    except ValueError:
        return None


def pct(s):
    v = num(s)
    return round(v / 100, 2) if v is not None else None


def id_from(url):
    return url.rstrip("/").split("/")[-1]


def key(s):
    s = s.lower().replace("%", "pct")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


# ---------- fighters ----------
def fighter_index(limit=None):
    """Returns {fighter_id: is_champion} from the A-Z list pages."""
    found = {}
    for c in string.ascii_lowercase:
        soup = get(f"{BASE}/statistics/fighters?char={c}&page=all")
        for row in soup.select("tr.b-statistics__table-row"):
            a = row.select_one("a[href*='fighter-details']")
            if not a:
                continue
            found[id_from(a["href"])] = bool(row.select_one("img[src*='belt']"))
        if limit and len(found) >= limit:
            break
    ids = list(found)[:limit] if limit else list(found)
    return {i: found[i] for i in ids}


STAT_MAP = {
    "slpm": ("slpm", num),
    "str. acc": ("str_acc", pct),
    "sapm": ("sapm", num),
    "str. def": ("str_def", pct),
    "td avg": ("td_avg", num),
    "td acc": ("td_acc", pct),
    "td def": ("td_def", pct),
    "sub. avg": ("sub_avg", num),
}
TEXT_MAP = {"height": "height", "weight": "weight", "reach": "reach",
            "stance": "stance", "dob": "dob"}


def parse_fighter(fid, is_champ):
    soup = get(f"{BASE}/fighter-details/{fid}")
    rec = txt(soup.select_one(".b-content__title-record")) or ""
    m = re.search(r"(\d+)-(\d+)-(\d+)", rec)
    w, l, d = (int(x) for x in m.groups()) if m else (0, 0, 0)
    f = {
        "id": fid,
        "name": txt(soup.select_one(".b-content__title-highlight")),
        "nickname": clean((txt(soup.select_one(".b-content__Nickname")) or "").strip()),
        "record": f"{w}-{l}-{d}",
        "wins": w, "losses": l, "draws": d,
        "is_champion": is_champ,
    }
    for li in soup.select("li.b-list__box-list-item"):
        t = txt(li)
        if not t or ":" not in t:
            continue
        k, v = t.split(":", 1)
        k = k.strip().lower()
        v = v.strip()
        if k in TEXT_MAP:
            f[TEXT_MAP[k]] = clean(v)
        elif k in STAT_MAP:
            name, fn = STAT_MAP[k]
            f[name] = fn(v)
    return f


# ---------- events ----------
def event_list(limit=None):
    soup = get(f"{BASE}/statistics/events/completed?page=all")
    events = []
    for row in soup.select("tr.b-statistics__table-row"):
        a = row.select_one("a[href*='event-details']")
        if not a:
            continue
        cols = row.select("td")
        events.append({
            "id": id_from(a["href"]),
            "name": txt(a),
            "date": txt(row.select_one(".b-statistics__date")),
            "location": txt(cols[1]) if len(cols) > 1 else None,
        })
    if not events:
        print("DIAGNOSTIC: no events parsed from the events page")
        print("page title:", txt(soup.title))
        print("html length:", len(str(soup)))
        print("first 600 chars:", str(soup)[:600])
        raise SystemExit("Stopping: events page had no recognizable rows (blocked or layout changed).")
    return events[:limit] if limit else events


def parse_event(ev):
    soup = get(f"{BASE}/event-details/{ev['id']}")
    card = []
    for row in soup.select("tr.b-fight-details__table-row[data-link]"):
        cols = row.select("td")
        if len(cols) < 10:
            continue
        card.append({
            "fight_id": id_from(row["data-link"]),
            "fighters": [txt(p) for p in cols[1].select("p")],
            "weight_class": txt(cols[6]),
            "method": " ".join(txt(p) for p in cols[7].select("p")),
            "round": txt(cols[8]),
            "time": txt(cols[9]),
        })
    ev["fights"] = card
    return ev


# ---------- fights ----------
def parse_table(table):
    """Parses a two-fighters-per-row stats table into a list of round dicts."""
    header, cur_round, out = [], None, []
    for row in table.select("tr"):
        ths = [txt(th) for th in row.select("th")]
        tds = row.select("td")
        if ths and not tds:
            if len(ths) == 1 and ths[0].lower().startswith("round"):
                cur_round = int(re.sub(r"\D", "", ths[0]) or 0)
            elif any(t and t.lower() == "fighter" for t in ths):
                header = [key(t) for t in ths]
            continue
        if not tds or not header:
            continue
        cols = []
        for td in tds:
            ps = [txt(p) for p in td.select("p")]
            cols.append(ps if ps else [txt(td)])
        entry = {"round": cur_round, "fighter1": {}, "fighter2": {}}
        for i, h in enumerate(header[:len(cols)]):
            vals = cols[i]
            entry["fighter1"][h] = vals[0] if len(vals) > 0 else None
            entry["fighter2"][h] = vals[1] if len(vals) > 1 else None
        out.append(entry)
    return out


def parse_fight(fid, event):
    soup = get(f"{BASE}/fight-details/{fid}")
    fighters = []
    for p in soup.select(".b-fight-details__person")[:2]:
        a = p.select_one("a[href*='fighter-details']")
        fighters.append({
            "id": id_from(a["href"]) if a else None,
            "name": txt(a),
            "result": txt(p.select_one(".b-fight-details__person-status")),
        })
    fight = {
        "id": fid,
        "event_id": event["id"],
        "event_name": event["name"],
        "date": event["date"],
        "title": txt(soup.select_one(".b-fight-details__fight-title")),
        "fighters": fighters,
        "method": None, "round": None, "time": None, "referee": None,
    }
    for item in soup.select(".b-fight-details__text-item, .b-fight-details__text-item_first"):
        t = txt(item) or ""
        for label, k in (("Method:", "method"), ("Round:", "round"),
                         ("Time:", "time"), ("Referee:", "referee")):
            if t.startswith(label):
                fight[k] = t[len(label):].strip()

    tables = [parse_table(t) for t in soup.select("table")]
    names = [f["name"] for f in fighters] + [None, None]

    def label(entries):
        for e in entries:
            e["fighter1"]["fighter"] = e["fighter1"].get("fighter") or names[0]
            e["fighter2"]["fighter"] = e["fighter2"].get("fighter") or names[1]
        return entries

    if len(tables) > 0 and tables[0]:
        t = label(tables[0])[0]
        fight["totals"] = {"fighter1": t["fighter1"], "fighter2": t["fighter2"]}
    else:
        fight["totals"] = {"fighter1": {"fighter": names[0]}, "fighter2": {"fighter": names[1]}}
    fight["rounds"] = label(tables[1]) if len(tables) > 1 else []
    fight["sig_strikes_totals"] = label(tables[2]) if len(tables) > 2 else []
    fight["sig_strikes_rounds"] = label(tables[3]) if len(tables) > 3 else []
    return fight


# ---------- main ----------
def save(name, data):
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, name), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"saved {name}: {len(data)} records")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only scrape N events and N fighters (for testing)")
    args = ap.parse_args()

    # 1) events + fights
    events, fights = [], []
    for i, ev in enumerate(event_list(args.limit), 1):
        try:
            ev = parse_event(ev)
        except Exception as e:
            print(f"event {ev['id']} failed: {e}")
            continue
        events.append(ev)
        for c in ev["fights"]:
            try:
                fights.append(parse_fight(c["fight_id"], ev))
            except Exception as e:
                print(f"fight {c['fight_id']} failed: {e}")
        print(f"[events {i}] {ev['name']} ({len(ev['fights'])} fights)")
    save("events.json", events)
    save("fights.json", fights)

    # 2) fight history per fighter, built from fights
    history = defaultdict(list)
    for ft in fights:
        fs = ft["fighters"]
        if len(fs) < 2:
            continue
        for a, b in ((fs[0], fs[1]), (fs[1], fs[0])):
            if a["id"]:
                history[a["id"]].append({
                    "fight_id": ft["id"], "event_id": ft["event_id"],
                    "event_name": ft["event_name"], "date": ft["date"],
                    "opponent": b["name"], "opponent_id": b["id"],
                    "result": a["result"], "method": ft["method"],
                    "round": ft["round"], "time": ft["time"],
                })

    # 3) fighters
    fighters = []
    index = fighter_index(args.limit)
    for i, (fid, champ) in enumerate(index.items(), 1):
        try:
            f = parse_fighter(fid, champ)
            f["fights"] = history.get(fid, [])
            fighters.append(f)
        except Exception as e:
            print(f"fighter {fid} failed: {e}")
        if i % 100 == 0:
            print(f"[fighters {i}/{len(index)}]")
    save("fighters.json", fighters)


if __name__ == "__main__":
    main()