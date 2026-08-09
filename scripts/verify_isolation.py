"""Verify project isolation between Phoenix instances A and B."""
import json
import urllib.request


def gql(endpoint: str, query: str, key: str | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        endpoint + "/graphql",
        data=json.dumps({"query": query}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"_raw": body[:200]}


QUERY_PROJECTS = """query { projects { edges { node { name } } } }"""

key_a = open(".local-keys/instance_a.key", encoding="utf-8").read().strip()
key_b = open(".local-keys/instance_b.key", encoding="utf-8").read().strip()

print("=== 1. A 的 key 查 A instance ===")
st, d = gql("http://localhost:6006", QUERY_PROJECTS, key_a)
print(f"   http={st} projects={[e['node']['name'] for e in d['data']['projects']['edges']]}")

print("=== 2. B 的 key 查 B instance ===")
st, d = gql("http://localhost:6007", QUERY_PROJECTS, key_b)
print(f"   http={st} projects={[e['node']['name'] for e in d['data']['projects']['edges']]}")

print("=== 3. 交叉：B 的 key 打 A 的端點（應該被拒）===")
st, d = gql("http://localhost:6006", QUERY_PROJECTS, key_b)
print(f"   http={st} body={json.dumps(d, ensure_ascii=False)[:120]}")

print("=== 4. 交叉：A 的 key 打 B 的端點（應該被拒）===")
st, d = gql("http://localhost:6007", QUERY_PROJECTS, key_a)
print(f"   http={st} body={json.dumps(d, ensure_ascii=False)[:120]}")

print("=== 5. 無 key 直接查（應該被拒）===")
st, d = gql("http://localhost:6006", QUERY_PROJECTS)
print(f"   http={st} body={json.dumps(d, ensure_ascii=False)[:120]}")