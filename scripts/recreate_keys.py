"""Recreate system API keys for both instances after DB backend migration (SQLite -> PG)."""
import json
import subprocess
import sys

INSTANCES = [
    {"inst": "A", "port": 6006, "jar": "jar_a.txt", "password": "ProjectA-Admin!2026",
     "out": ".local-keys/instance_a.key"},
    {"inst": "B", "port": 6007, "jar": "jar_b.txt", "password": "ProjectB-Admin!2026",
     "out": ".local-keys/instance_b.key"},
]

MUTATION = """mutation { createSystemApiKey(input: {name: "local-demo"}) { jwt apiKey { name } } }"""


def sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True)


for c in INSTANCES:
    base = f"http://localhost:{c['port']}"
    # login (204 = ok)
    login = sh("curl", "-s", "-o", "nul", "-w", "%{http_code}", "-c", c["jar"],
               "-X", "POST", f"{base}/auth/login",
               "-H", "Content-Type: application/json",
               "-d", json.dumps({"email": "admin@localhost", "password": c["password"]}))
    if login.stdout.strip() != "204":
        print(f"[{c['inst']}] login FAILED: {login.stdout}"); sys.exit(1)
    # create key
    gql = sh("curl", "-s", "-b", c["jar"], "-X", "POST", f"{base}/graphql",
             "-H", "Content-Type: application/json", "-d", json.dumps({"query": MUTATION}))
    data = json.loads(gql.stdout)
    jwt = data["data"]["createSystemApiKey"]["jwt"]
    with open(c["out"], "w", encoding="utf-8") as f:
        f.write(jwt)
    print(f"[{c['inst']}] login 204, key recreated -> {c['out']} (len={len(jwt)})")
print("DONE")