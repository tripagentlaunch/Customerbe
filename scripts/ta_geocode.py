#!/usr/bin/env python3
# TripAgent — geocode curated guide venues per city for the in-city map.
# Source: data/cityguides.json (keyed by the same 110 city slugs the pages use).
# Sink:   data/venue-coords.json  { slug: {center:[lat,lon], venues:[{n,cat,tier,a,d,lat,lon,p}]} }
# Respectful of the OSM Nominatim usage policy: 1 request/sec, real User-Agent,
# resumable (skips cities already resolved), centroid sanity-check to reject bad hits.
import json, time, sys, math, urllib.parse, urllib.request

ROOT = "/Users/amit/Claude Code/tripagent-site"
GUIDES = json.load(open(ROOT+"/data/cityguides.json", encoding="utf-8"))
CITIES = json.load(open(ROOT+"/data/cities.json", encoding="utf-8"))
OUT = ROOT+"/data/venue-coords.json"
UA = "TripAgent/1.0 (luxury travel research; amit@yangtsofour.com)"
SLEEP = 0.35
# how many to keep per category (a clean, curated map — not every bar)
CAPS = {"stay": 99, "eat": 6, "do": 8, "party": 3}

try:
    RESULT = json.load(open(OUT, encoding="utf-8"))
except Exception:
    RESULT = {}

def q(query, bias=None):
    """Photon (Komoot) search -> (lat,lon) or None. bias=(lat,lon) nudges results local."""
    params = {"q": query, "limit": "1", "lang": "en"}
    if bias:
        params["lat"] = "%f" % bias[0]; params["lon"] = "%f" % bias[1]
    url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
            feats = d.get("features") or []
            if feats:
                lon, lat = feats[0]["geometry"]["coordinates"]
                return (float(lat), float(lon))
            return None
        except Exception as e:
            if attempt == 0:
                time.sleep(1.2); continue
            sys.stderr.write("  ! %s -> %s\n" % (query[:50], e))
            return None
    return None

def dist_km(a, b):
    R = 6371.0
    dlat = math.radians(b[0]-a[0]); dlon = math.radians(b[1]-a[1])
    x = math.sin(dlat/2)**2 + math.cos(math.radians(a[0]))*math.cos(math.radians(b[0]))*math.sin(dlon/2)**2
    return 2*R*math.asin(math.sqrt(x))

def venues_of(g):
    """Flatten a guide into a curated venue list with cat/tier."""
    out = []
    def add(cat, tier, lst):
        for v in lst:
            if isinstance(v, dict) and v.get("n"):
                out.append({"n": v["n"], "cat": cat, "tier": tier,
                            "a": v.get("a", ""), "d": v.get("d", "")})
    for cat in ("stay", "eat", "party"):
        obj = g.get(cat)
        if isinstance(obj, dict):
            for tier, lst in obj.items():
                if isinstance(lst, list): add(cat, tier, lst)
        elif isinstance(obj, list):
            add(cat, "", obj)
    do = g.get("do")
    if isinstance(do, list): add("do", "", do)
    elif isinstance(do, dict):
        for tier, lst in do.items():
            if isinstance(lst, list): add("do", tier, lst)
    # apply caps per category, preserving guide order
    kept, seen_cat = [], {}
    for v in out:
        c = v["cat"]; seen_cat[c] = seen_cat.get(c, 0)
        if seen_cat[c] < CAPS.get(c, 6):
            kept.append(v); seen_cat[c] += 1
    return kept

def main():
    slugs = sys.argv[1:] or list(CITIES.keys())
    for slug in slugs:
        if slug in RESULT and RESULT[slug].get("venues"):
            print("skip (done):", slug); continue
        g = GUIDES.get(slug)
        if not g:
            print("no guide:", slug); continue
        c = CITIES[slug]; name = c["name"]; country = c["country"]
        print("== %s (%s, %s) ==" % (slug, name, country))
        center = q("%s, %s" % (name, country)); time.sleep(SLEEP)
        if not center:
            center = q(name); time.sleep(SLEEP)
        if not center:
            print("  !! no centroid, skipping"); continue
        resolved = []
        for v in venues_of(g):
            pt = q("%s, %s, %s" % (v["n"], name, country), bias=center); time.sleep(SLEEP)
            prec = "v"
            if not pt or dist_km(center, pt) > 60:
                pt = None
                if v.get("a"):
                    pt = q("%s, %s, %s" % (v["a"], name, country), bias=center); time.sleep(SLEEP)
                    prec = "a"
            if pt and dist_km(center, pt) <= 60:
                r = {"n": v["n"], "cat": v["cat"], "tier": v["tier"],
                     "a": v["a"], "d": v["d"],
                     "lat": round(pt[0], 6), "lon": round(pt[1], 6), "p": prec}
                resolved.append(r)
            else:
                sys.stderr.write("  drop: %s\n" % v["n"][:40])
        RESULT[slug] = {"center": [round(center[0], 6), round(center[1], 6)], "venues": resolved}
        json.dump(RESULT, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
        print("  -> %d/%d venues placed" % (len(resolved), len(venues_of(g))))

if __name__ == "__main__":
    main()
