# 上 K8s 部署指南（從本地驗證 → 公司叢集）

> 對應 `docs/production-deployment-plan.md` 的藍圖；本地已驗證的架構原封不動搬上 Helm。
> Chart：`oci://registry-1.docker.io/arizephoenix/phoenix-helm`（本文撰寫時 v11.0.17 / appVersion 19.19.1）

## 1. 本地 ↔ K8s 對照

| 本地（已驗證） | K8s |
|---|---|
| `phoenix serve`（venv） | Helm chart Deployment（官方 image） |
| port 6006 / 6007 | 各 release 自己的 Service（ClusterIP/NodePort/LoadBalancer） |
| `data/a`、`data/b`（SQLite→已移轉 PG） | 共用 managed PostgreSQL |
| `PHOENIX_SQL_DATABASE_SCHEMA=team_a/team_b` | values 的 `database.postgres.schema` |
| `PHOENIX_ENABLE_AUTH` + `PHOENIX_SECRET` | values 的 `auth.enableAuth` + K8s Secret（chart 自動產生） |
| `.local-keys/*.key`（本機 API key） | 部署後到 UI 重建 system API key 給各應用程式 |

## 2. 前置條件

```bash
helm version        # Helm 3
kubectl config current-context   # 已連到公司 cluster
kubectl get ns      # 有權限建 namespace
```

## 3. 先跟公司要這四樣（deploy/ 的 values 有 `<...>` 佔位）

1. **PG endpoint + 帳號密碼**（RDS/Cloud SQL 等，版本 ≥ 14）
2. **namespace**（建議 `observability`；本文範例用它）
3. **ingress host**（例如 `phoenix-a.observability.internal.example.com`）+ TLS 方式
4. 確認 PG 帳號對 `team_a` / `team_b` 兩個 schema 有權限
   （用同一帳號最簡單；嚴格一點就每 team 一帳號 + 只 grant 自己 schema）

## 4. 部署（兩個 release）

```bash
NAMESPACE=observability
CHART=oci://registry-1.docker.io/arizephoenix/phoenix-helm
VERSION=11.0.17          # 正式環境務必 pin 版本

kubectl create ns $NAMESPACE

# 先填好 deploy/values-team-a.yaml 的 <PG_HOST>
helm install phoenix-team-a $CHART --version $VERSION \
  -n $NAMESPACE -f deploy/values-team-a.yaml

helm install phoenix-team-b $CHART --version $VERSION \
  -n $NAMESPACE -f deploy/values-team-b.yaml
```

Helm chart 會自動：
- 建立 `phoenix-secret`（內含自動產生的 `PHOENIX_SECRET`、admin 初始密碼、PG 密碼）
- 首啟時自動建立 `team_a` / `team_b` schema 並跑 migration
- 掛上 ingress（依 values 的 host）

## 5. 驗證

```bash
kubectl -n $NAMESPACE get pods                 # 4 個 pod 都 Running/Ready (2+2)
kubectl -n $NAMESPACE get svc                  # phoenix-team-a/b service
kubectl -n $NAMESPACE get ingress

# 開 UI（不想曝光可以先 port-forward 測）
kubectl -n $NAMESPACE port-forward svc/phoenix-team-a 6006:6006
# → http://localhost:6006 登入 admin@localhost（密碼在 secret 裡）：
kubectl -n $NAMESPACE get secret phoenix-secret -o jsonpath='{.data.PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD}' | base64 -d

# 建 system API key（同本機流程：login → GraphQL createSystemApiKey）
# 之後把 key 給專案 A 的應用程式（PHOENIX_API_KEY）

# DB 層確認 schema 分開（用公司 PG 工具或 psql）
# SELECT table_schema, count(*) FROM information_schema.tables
#   WHERE table_schema IN ('team_a','team_b') GROUP BY 1;
```

## 6. 升級 / 回滾（重點：DB migration）

```bash
# 升級前：備份 PG（PITR 快照）
# 改 values → 升級（滾動，replica 一個一個換）
helm upgrade phoenix-team-a $CHART --version $VERSION \
  -n $NAMESPACE -f deploy/values-team-a.yaml

# 驗證：pods 全部 Ready → UI 登入 → 新 trace 進得來 → 舊資料查得到
# 出問題：回滾（image 回上一版；DB 若 migration 不可逆則用備份還原）
helm rollback phoenix-team-a <REVISION>
```

原則（詳見 production-deployment-plan §5）：**備份先行、滾動升級、驗證新舊資料、保留 rollback 路徑**。

## 7. 網路鎖定（Cilium NetworkPolicy 簡版）

依官方 production guide：Phoenix pod 是「跳板」風險點，egress 轉 allow-list：

```yaml
apiVersion: cilium.io/v2
kind: CiliumNetworkPolicy
metadata:
  name: phoenix-team-a-egress
  namespace: observability
spec:
  endpointSelector:
    matchLabels:
      app.kubernetes.io/instance: phoenix-team-a   # 依實際 pod label 調整
  egress:
    - toEntities: [kube-apiserver, cluster]        # cluster DNS 等
    - toServices:
        - k8sService:
            namespace: observability
            serviceName: <pg-service-or-external-name>   # DB
    # 明確的 LLM provider 網域白名單（evals/playground 需要）
    # 擋 metadata endpoint: 169.254.169.254（預設 Cilium 會擋 cloud metadata）
```

> 完整原則見 `docs/production-deployment-plan.md` §4.4。公司若用其他 CNI（Calico 等）對應調整。

## 8. 常見錯誤

| 症狀 | 原因 / 解法 |
|---|---|
| pod CrashLoop：`Exception: package not installed` | 缺 PG driver——先 `uv pip install "arize-phoenix[pg]"`（`pip install 'arize-phoenix[pg]'`） |
| 兩台都寫進 `public` | values `database.postgres.schema` 沒設或拼錯——schema 字串會原樣傳給 `PHOENIX_SQL_DATABASE_SCHEMA` |
| admin 密碼不知道 | `kubectl get secret phoenix-secret -o jsonpath='{.data.PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD}' \| base64 -d` |
| 升級後 trace 進不來 | 先看 pod log 有沒有 migration 失敗 → 檢查 PG 備份是否要先還原 |
| 兩個 release 的 secret 名稱衝突 | 各自 release 會有各自 secret（預設 `phoenix-secret` 以 release 為作用域）；確認沒用 `--set auth.name` 覆蓋成同名 |

## 9. 拆除

```bash
helm uninstall phoenix-team-a -n $NAMESPACE   # 不刪 PG 資料（schema 留在 DB）
```

---

### 與本機實驗的關係

本機（SQLite → 共用 PG + schema）驗證的每一個環節，在 K8s 上都有對應：
`deploy/values-team-a.yaml` / `values-team-b.yaml` 就是「本機啟動指令的 Helm 版」。
隔離邏輯完全一樣：**同一台 PG、不同 schema、不同 instance、不同 key**。