"""Generate deterministic rehearsal XML in the official FIDE format.

Scale mirrors the September 2026 baseline (NOT hard-coded anywhere in
the service — this script is test tooling only):

  standard: 16,244 players (IDs 16682..32925)
  rapid:    26,830 players (IDs 1..26830)
  blitz:    11,959 players (IDs 1..11959, subset of rapid)
  unique:   32,925 players / 55,033 snapshots per period

20% of IDs (every 5th) carry federation IRI; the rest spread over ten
federations. Titles/birthdays/inactive flags are deterministic by ID so
a manifest of known players can be verified after import.

Usage:
    python scripts/generate_rehearsal_xml.py --out <dir>

Outputs standard.xml / rapid.xml / blitz.xml + manifest.json into <dir>.
"""
import argparse
import json
import os
import xml.sax.saxutils as sax

STANDARD_N = 16244
RAPID_N = 26830
BLITZ_N = 11959
UNIQUE_N = 32925
STANDARD_START = UNIQUE_N - STANDARD_N + 1  # 16682

FEDS = ["GER", "FRA", "IND", "USA", "ESP", "AZE", "ARM", "UZB", "CHN",
        "NED"]
TITLES = ["GM", "IM", "FM", "CM"]


def fed_for(pid: int) -> str:
    if pid % 5 == 0:
        return "IRI"
    return FEDS[pid % len(FEDS)]


def profile_for(pid: int):
    name = "Family%05d, Test%05d" % (pid, pid)
    sex = "F" if pid % 7 == 0 else "M"
    title, wtitle = "", ""
    if pid % 37 == 0:
        title = TITLES[(pid // 37) % len(TITLES)]
    elif sex == "F" and pid % 41 == 0:
        wtitle = ["WGM", "WIM", "WFM"][(pid // 41) % 3]
    rating = 1400 + (pid * 7919) % 1301
    games = pid % 31
    k = 40 if games < 30 else (10 if rating >= 2400 else 20)
    birth = str(1950 + (pid * 13) % 66)
    inactive = "1" if pid % 53 == 0 else ""
    return {"name": name, "sex": sex, "title": title, "wtitle": wtitle,
            "rating": rating, "games": games, "k": k, "birth": birth,
            "inactive": inactive}


def player_xml(pid: int) -> str:
    p = profile_for(pid)
    fed = fed_for(pid)
    parts = ["<player>"]
    parts.append("<fideid>%d</fideid>" % pid)
    parts.append("<name>%s</name>" % sax.escape(p["name"]))
    parts.append("<country>%s</country>" % fed)
    parts.append("<sex>%s</sex>" % p["sex"])
    if p["title"]:
        parts.append("<title>%s</title>" % p["title"])
    if p["wtitle"]:
        parts.append("<w_title>%s</w_title>" % p["wtitle"])
    parts.append("<rating>%d</rating>" % p["rating"])
    parts.append("<games>%d</games>" % p["games"])
    parts.append("<k>%d</k>" % p["k"])
    parts.append("<birthday>%s</birthday>" % p["birth"])
    if p["inactive"]:
        parts.append("<flag>inactive</flag>")
    parts.append("</player>")
    return "".join(parts)


def write_file(path: str, ids) -> int:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("<playerslist>")
        n = 0
        for pid in ids:
            fh.write(player_xml(pid))
            n += 1
        fh.write("</playerslist>")
    return n


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    std_ids = list(range(STANDARD_START, UNIQUE_N + 1))
    rapid_ids = list(range(1, RAPID_N + 1))
    # Blitz overlaps standard substantially (as in reality) so three-type
    # players exist; it stays a subset of rapid.
    blitz_ids = list(range(RAPID_N - BLITZ_N + 1, RAPID_N + 1))
    assert len(std_ids) == STANDARD_N
    assert len(rapid_ids) == RAPID_N
    assert len(blitz_ids) == BLITZ_N
    assert set(blitz_ids) <= set(rapid_ids)
    assert len(set(std_ids) | set(rapid_ids) | set(blitz_ids)) == UNIQUE_N

    counts = {
        "standard": write_file(os.path.join(args.out, "standard.xml"),
                               std_ids),
        "rapid": write_file(os.path.join(args.out, "rapid.xml"), rapid_ids),
        "blitz": write_file(os.path.join(args.out, "blitz.xml"), blitz_ids),
    }
    iri = sum(1 for pid in range(1, UNIQUE_N + 1) if fed_for(pid) == "IRI")

    # Known players: rapid-only non-IRI, all-three-types IRI,
    # standard-only IRI, rapid+blitz IRI.
    known_ids = [1, 20000, 30000, 15000]
    manifest = {"counts": counts, "unique": UNIQUE_N, "iri_players": iri,
                "known": {}}
    for pid in known_ids:
        p = profile_for(pid)
        entry = {"federation": fed_for(pid), "name": p["name"],
                 "title": p["title"], "wtitle": p["wtitle"],
                 "birth_year": p["birth"], "sex": p["sex"],
                 "inactive": bool(p["inactive"]), "ratings": {}}
        if pid in std_ids:
            entry["ratings"]["standard"] = {
                "rating": p["rating"], "games": p["games"],
                "k_factor": p["k"]}
        if pid in rapid_ids:
            entry["ratings"]["rapid"] = {
                "rating": p["rating"], "games": p["games"],
                "k_factor": p["k"]}
        if pid in blitz_ids:
            entry["ratings"]["blitz"] = {
                "rating": p["rating"], "games": p["games"],
                "k_factor": p["k"]}
        manifest["known"][str(pid)] = entry

    with open(os.path.join(args.out, "manifest.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
    print("wrote %s unique=%d iri=%d" % (counts, UNIQUE_N, iri))


if __name__ == "__main__":
    main()
