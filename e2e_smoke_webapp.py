import os
import threading

ROOT = r"C:\Users\user\music-intelligence-engine"
DB = os.path.join(ROOT, "data", "music-intelligence.db")
os.environ.setdefault("PYTHONPATH", ROOT)


def main() -> int:
    import portalocker_off  # noqa - not a real dep; just ensure import baseline

    from backend.webapp import create_server

    # The module keeps its own server event; we just need the httpd handle.
    host = "127.0.0.1"
    port = 8789
    server = create_server(host, port, db_path=DB)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    import time
    time.sleep(1.2)

    import json
    from urllib.request import urlopen

    def get(path: str) -> dict:
        url = f"http://{host}:{port}{path}"
        with urlopen(url, timeout=8) as resp:
            body = resp.read().decode("utf-8")
            return {"status": resp.status, "body": json.loads(body)}

    base = "/api/v1/djs"
    results = {}
    for label, suffix in (
        ("all", ""),
        ("dj_type=club_dj", "?dj_type=club_dj"),
        ("dj_type=independent", "?dj_type=independent"),
        ("platform=Mixcloud", "?platform=Mixcloud"),
        ("has_contact=true", "?has_contact=true"),
        ("station=KEXP", "?station=KEXP"),
    ):
        try:
            data = get(base + suffix)
            djs = data["body"].get("data", {}).get("djs", [])
            names = [d.get("name") for d in djs]
            results[label] = (len(djs), names)
        except Exception as exc:  # noqa: BLE001
            results[label] = ("ERR", str(exc))

    for label, (count, names) in results.items():
        print(f"{label:24} -> {count}  {names}")

    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
