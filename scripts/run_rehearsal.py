"""Full-scale rehearsal runner (manual, local MySQL only).

Reads connection + paths from the environment/flags — never hard-codes
credentials or production references:

    set FIDE_REHEARSAL_URL=mysql+pymysql://user:pw@127.0.0.1:3307/db?charset=utf8mb4
    python scripts/run_rehearsal.py --xml-dir <generator-out> \
        --workdir <temp> --periods 2026-08 2026-09 --evidence <out.json>

For each period: stage the three XMLs as ZIPs, ensure jobs, run three
worker ticks (standard -> rapid -> blitz), then verify. Measures wall
time, Python peak allocation, peak RSS, DB size, tmp disk, row counts,
and known-player correctness from manifest.json. Official September ZIPs
are used when available; otherwise the deterministic generator output.
"""
import argparse
import ctypes
import json
import os
import shutil
import sys
import tempfile
import time
import tracemalloc
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))


def peak_rss_mb():
    try:  # Windows (argtypes required on 64-bit)
        import ctypes.wintypes as wt
        psapi = ctypes.windll.psapi
        psapi.GetProcessMemoryInfo.argtypes = [
            wt.HANDLE, ctypes.c_void_p, wt.DWORD]
        psapi.GetProcessMemoryInfo.restype = wt.BOOL

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wt.DWORD),
                        ("PageFaultCount", wt.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]

        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        if psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(pmc), pmc.cb):
            return round(pmc.PeakWorkingSetSize / 1048576, 1)
    except Exception:
        pass
    try:  # POSIX fallback
        import resource
        return round(resource.getrusage(
            resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        return None


def dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml-dir", required=True)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--periods", nargs="+", required=True)
    ap.add_argument("--evidence", default=None)
    args = ap.parse_args()

    url = os.environ.get("FIDE_REHEARSAL_URL", "")
    if not url:
        raise SystemExit("FIDE_REHEARSAL_URL is required")
    workdir = args.workdir or tempfile.mkdtemp(prefix="fide-rehearsal-")
    data_dir = os.path.join(workdir, "data")
    os.makedirs(data_dir, exist_ok=True)

    with open(os.path.join(args.xml_dir, "manifest.json")) as fh:
        manifest = json.load(fh)

    from config import Settings
    from interfaces.factory import create_app
    from infrastructure import migrations
    from infrastructure.database import db
    from application import checks, import_service
    from application.import_service import JobRunnerConfig
    from infrastructure import storage
    from sqlalchemy import text

    settings = Settings(database_url=url, api_key="x", admin_key="y",
                        secret_key="s", data_directory=data_dir,
                        rate_limit_per_minute=0,
                        history_federations=["IRI"])
    app = create_app(settings)
    evidence = {"periods": {}, "manifest_counts": manifest["counts"],
                "unique_expected": manifest["unique"]}
    t0 = time.monotonic()
    tracemalloc.start()
    with app.app_context():
        migrations.upgrade(db.engine)
        db.session.execute(text("DELETE FROM fide_ratings"))
        db.session.execute(text("DELETE FROM fide_import_jobs"))
        db.session.execute(text("DELETE FROM fide_players"))
        db.session.commit()

        cfg = JobRunnerConfig(
            data_dir=data_dir, base_url="http://localhost/",
            batch_size=500, history_federations=["IRI"])
        grand_processed = grand_stored = grand_pruned = 0
        for period in args.periods:
            for rtype in ("standard", "rapid", "blitz"):
                src = os.path.join(args.xml_dir, rtype + ".xml")
                dest = storage.tmp_zip_path(data_dir, period, rtype)
                with zipfile.ZipFile(dest, "w",
                                     zipfile.ZIP_DEFLATED) as zf:
                    zf.write(src, "players_list.xml")
            import_service.ensure_period_jobs(period)
            ticks = []
            for _ in range(3):
                t1 = time.monotonic()
                res = import_service.tick(period, cfg)
                res["wall_seconds"] = round(time.monotonic() - t1, 1)
                ticks.append(res)
                assert res["status"] == "success", res
            evidence["periods"][period] = ticks
            for res in ticks:
                grand_processed += res["processed"]
                grand_stored += res["stored"]
                grand_pruned += res.get("pruned", 0)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        report = checks.verify_data()
        # Known-player assertions from the manifest.
        from infrastructure.repositories import (
            FidePlayerRepository, FideRatingRepository)
        known_ok = True
        known_detail = {}
        for pid, exp in manifest["known"].items():
            row = FidePlayerRepository.get_by_fide_id(pid)
            detail = {"found": row is not None}
            if row is None:
                known_ok = False
            else:
                detail["federation"] = row.federation
                detail["title"] = row.title
                if row.federation != exp["federation"]:
                    known_ok = False
                latest = FideRatingRepository.latest_for_player(pid)
                for rtype, vals in exp["ratings"].items():
                    got = latest.get(rtype)
                    match = (got is not None and got.rating == vals["rating"]
                             and got.games == vals["games"]
                             and got.k_factor == vals["k_factor"])
                    detail[rtype] = match
                    if not match:
                        known_ok = False
            known_detail[pid] = detail
        # Cross-type consistency: players holding all three current types.
        three = db.session.execute(text(
            "SELECT COUNT(*) FROM (SELECT fide_id FROM fide_ratings "
            "WHERE period = :p GROUP BY fide_id "
            "HAVING COUNT(DISTINCT rating_type) = 3) t"),
            {"p": args.periods[-1]}).scalar()
        size = db.session.execute(text(
            "SELECT SUM(data_length + index_length) "
            "FROM information_schema.tables "
            "WHERE table_schema = DATABASE()")).scalar()

    evidence.update({
        "verify": report,
        "known_players_ok": known_ok,
        "known_players": known_detail,
        "three_type_players_last_period": three,
        "totals": {"processed": grand_processed, "stored": grand_stored,
                   "pruned": grand_pruned},
        "wall_seconds": round(time.monotonic() - t0, 1),
        "python_peak_mb": round(peak / 1048576, 1),
        "process_peak_rss_mb": peak_rss_mb(),
        "db_bytes": int(size or 0),
        "tmp_bytes_after": dir_size(data_dir),
    })
    out = json.dumps(evidence, indent=2)
    if args.evidence:
        with open(args.evidence, "w") as fh:
            fh.write(out)
    print(out)


if __name__ == "__main__":
    main()
