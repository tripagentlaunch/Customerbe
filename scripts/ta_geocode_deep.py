#!/usr/bin/env python3
# TripAgent — geocode the FULL deep guides (data/deep/<slug>.json) for the map.
# Unlike ta_geocode.py (old capped cityguides subset), this places EVERY venue in
# the credentialed guide the member is actually reading — hotels/eat/do/nightlife.
# Free (Photon/Komoot), resumable, centroid sanity-checked. Sink: data/venue-coords.json
# (the same file js/days.js reads), so the map reflects the real guide instantly.
#
# Testbed note: coords are approximate (Photon name search). When we swap the base
# map to Google, Google Places returns exact coords per venue — this is the interim.
import json, time, sys, math, glob, os, urllib.parse, urllib.request

ROOT = "/Users/amit/Claude Code/tripagent-site"
CITIES = json.load(open(ROOT+"/data/cities.json", encoding="utf-8"))
OUT = ROOT+"/data/venue-coords.json"
UA = "TripAgent/1.0 (luxury travel research; amit@yangtsofour.com)"
SLEEP = 0.35
# deep category -> map category the front-end expects (stay/eat/do/party)
CATMAP = {"hotels": "stay", "eat": "eat", "do": "do", "nightlife": "party"}

try:
    RESULT = json.load(open(OUT, encoding="utf-8"))
except Exception:
    RESULT = {}

def q(query, bias=None):
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

def deep_venues(slug):
    """Flatten a deep guide into map venues: {n,cat,tier,a,d}."""
    d = json.load(open(ROOT+"/data/deep/%s.json" % slug, encoding="utf-8"))
    out = []
    for cat_key, mapcat in CATMAP.items():
        for e in d.get(cat_key, []) or []:
            nm = e.get("name")
            if not nm:
                continue
            out.append({"n": nm, "cat": mapcat, "tier": e.get("band", ""),
                        "a": e.get("area", ""), "d": e.get("why", "")})
    return out

def main():
    deep_slugs = [os.path.basename(p)[:-5] for p in glob.glob(ROOT+"/data/deep/*.json")]
    slugs = sys.argv[1:] or sorted(deep_slugs)
    for slug in slugs:
        if slug not in deep_slugs:
            print("no deep guide:", slug); continue
        # rebuild if the stored record is the OLD capped one (few venues) or missing
        vs = deep_venues(slug)
        existing = RESULT.get(slug, {}).get("venues")
        if existing and len(existing) >= len(vs) - 2:
            print("skip (done):", slug, len(existing)); continue
        c = CITIES.get(slug, {}); name = c.get("name", slug); country = c.get("country", "")
        print("== %s (%s, %s)  %d venues ==" % (slug, name, country, len(vs)))
        center = q("%s, %s" % (name, country)); time.sleep(SLEEP)
        if not center:
            center = q(name); time.sleep(SLEEP)
        if not center:
            print("  !! no centroid, skipping"); continue
        resolved = []
        for v in vs:
            pt = q("%s, %s, %s" % (v["n"], name, country), bias=center); time.sleep(SLEEP)
            prec = "v"
            if not pt or dist_km(center, pt) > 60:
                pt = None
                if v.get("a"):
                    pt = q("%s, %s, %s" % (v["a"], name, country), bias=center); time.sleep(SLEEP)
                    prec = "a"
            if pt and dist_km(center, pt) <= 60:
                resolved.append({"n": v["n"], "cat": v["cat"], "tier": v["tier"],
                                 "a": v["a"], "d": v["d"],
                                 "lat": round(pt[0], 6), "lon": round(pt[1], 6), "p": prec})
            else:
                sys.stderr.write("  drop: %s\n" % v["n"][:40])
        RESULT[slug] = {"center": [round(center[0], 6), round(center[1], 6)], "venues": resolved}
        # atomic write: a concurrent deploy reading OUT always sees a complete file
        tmp = OUT + ".tmp"
        json.dump(RESULT, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
        os.replace(tmp, OUT)
        print("  -> %d/%d placed" % (len(resolved), len(vs)))

if __name__ == "__main__":
    main()
