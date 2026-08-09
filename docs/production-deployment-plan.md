# Phoenix 生產環境部署規劃（大型企業）

> 版本：v1.0（2026-08-09）
> 依據：Arize Phoenix 官方文件 v19.x（self-hosting / architecture / production-guide / authentication / RBAC）
> 關鍵事實來源：`docs/phoenix/self-hosting/architecture.mdx`、`self-hosting/configuration.mdx`、`production-guide.mdx`、`features/authentication.mdx`、`settings/access-control-rbac.mdx`

---

## 0. 執行摘要

大型企業部署 Arize Phoenix 的核心結論：

1. **Phoenix 是無狀態應用 + PostgreSQL 後端**，水平擴展 = 多個 replicate 掛在同一個 DB 後面，官方架構頁明載此模式。
2. **OSS Phoenix 是單租戶架構**（single tenant）：一個 instance = 一個租戶，RBAC 只有 admin/member/viewer 三種角色，**沒有 SAML SSO、沒有多租戶**。大公司要靠「多 instance（環境/團隊隔離）+ schema 隔離」組合來達成隔離需求。
3. **資料層是真正的重點**：managed PostgreSQL（RDS/Cloud SQL/Azure Database，PG ≥ 14）＋ Multi-AZ + PITR 備份 + read replica（`PHOENIX_SQL_DATABASE_READ_REPLICA_URL`，v14.0.0+）。
4. **安全模型分三層**：應用層（auth/API key/RBAC）＋ 網路層（Cilium egress allowlist、擋 metadata endpoint）＋ 資料層（TLS、加密、備份）。
5. 升級有 DB migration 風險 → 需要 rollback 能力與 runbook（備份先行、逐 replica 滾動）。

---

## 1. 先回答的三個關鍵決策

### 決策 1：OSS 自架 vs Arize AX（企業版）

| 需求 | OSS Phoenix | Arize AX |
|---|---|---|
| 定價 | 免費（自架） | 授權費 |
| SSO（SAML/OIDC） | ❌ 僅本地帳號 + OAuth2 內建授權伺服器 | ✅ SAML、SSO |
| 多租戶（org/space） | ❌ 單租戶 | ✅ org/space 多層 |
| JIT 使用者佈建 | ❌ | ✅ |
| 高吞吐 OLAP 分析 | ❌（PG 是其極限） | ✅ 專用 OLAP DB（adb） |
| 資料主權 | ✅ 完全自控 | 可自架（on-prem 選項） |

**建議**：若公司需要 SAML SSO 或跨部門租戶隔離，直接評估 AX。本文件以下內容以 **OSS 自架**為前提（也是多數大型企業先用自架驗證、後續再談 AX 的常見路徑）。

### 決策 2：部署平台

建議 **Kubernetes（EKS / GKE / AKS）**，理由：
- 官方提供 Helm chart（`arizephoenix/phoenix-helm`）
- 水平擴展（多 replica）是原生操作
- NetworkPolicy（Cilium）是官方 production guide 推薦的網路鎖定方式
- 大公司的標準平台，維運交接成本最低

### 決策 3：規模數字（會決定資源給多少）

需要收集的數字（第 7 節有估算方法）：
- 每日 trace 數（或 span 數）
- 每日 LLM 呼叫次數 → 平均 token 數
- 保留期限（天）
- 同時線上使用者數（UI 查詢量）

---

## 2. 官方架構事實（設計依據）

| 事實 | 出處 |
|---|---|
| Phoenix = Web UI + trace collector(OTLP HTTP/gRPC) + SQL DB；PostgreSQL 為 production 建議後端，最低版本 **PG 14** | architecture.mdx |
| **水平擴展**：多 Phoenix instance 共享同一 DB，前端掛 load balancer | architecture.mdx |
| **單租戶**：一個 instance = 一個租戶；多團隊隔離靠多 instance，或共享 DB + `PHOENIX_SQL_DATABASE_SCHEMA` 做 schema 隔離 | architecture.mdx |
| read replica 支援：`PHOENIX_SQL_DATABASE_READ_REPLICA_URL`（v14.0.0+） | configuration.mdx |
| 認證：`PHOENIX_ENABLE_AUTH=True` + `PHOENIX_SECRET`（JWT 簽章，多 replica 必須共享同一 secret） | authentication.mdx |
| 角色：admin / member / viewer；system API key（寫入 trace 用）vs user API key | authentication.mdx / RBAC |
| 資料保留：`PHOENIX_DEFAULT_RETENTION_POLICY_DAYS`，0 = 永久保留 | configuration.mdx |
| Phoenix 自身可觀測性：`PHOENIX_ENABLE_PROMETHEUS`（9090）+ 自身 OTLP self-instrumentation | configuration.mdx / production-guide.mdx |
| 安全重點：Phoenix pod 是「跳板」風險點 → 網路層 egress allowlist + 擋 `169.254.169.254`（metadata endpoint） | production-guide.mdx |

---

## 3. 目標架構

```
                    應用程式層（各團隊 service / agent / MCP server）
   ┌───────────┐ ┌───────────┐ ┌───────────┐        ┌───────────┐
   │  App A    │ │  App B    │ │  App C    │  ...   │  OTel     │
   │(OpenInfe- │ │(LangChain)│ │(MCP svr)  │        │ Collector │
   │ rince SDK)│ │           │ │           │        │ (optional)│
   └─────┬─────┘ └─────┬─────┘ └─────┬─────┘        └─────┬─────┘
         │             │             │                    │
         ▼             ▼             ▼                    ▼
   ┌──────────────────────────────────────────────────────────────┐
   │  VPC 內網（私有 subnet）                                       │
   │                                                              │
   │  ┌────────────────────────────────────────────────────────┐  │
   │  │ K8s Ingress / Internal LB (TLS 終止, mTLS 可選)         │  │
   │  └───────────────┬────────────────────────────────────────┘  │
   │                  ▼                                           │
   │  ┌───────────────────────────────────────────┐               │
   │  │ Phoenix Deployment（2~3 replicas，共享     │               │
   │  │ PHOENIX_SECRET，滾動升級）                 │               │
   │  │  - 6006 UI + OTLP HTTP                    │               │
   │  │  - 4317 OTLP gRPC                         │               │
   │  │  - 9090 Prometheus metrics                │               │
   │  └───────────────────┬───────────────────────┘               │
   │                      ▼                                       │
   │  ┌───────────────────────────────────────────┐               │
   │  │ Managed PostgreSQL (PG ≥ 14)              │               │
   │  │  - Primary: Multi-AZ / HA 自動故障轉移     │               │
   │  │  - Read replica → PHOENIX_SQL_DATABASE_   │               │
   │  │    READ_REPLICA_URL（大查詢量時啟用）       │               │
   │  │  - PITR 備份、加密、獨立 instance          │               │
   │  └───────────────────────────────────────────┘               │
   └──────────────────────────────────────────────────────────────┘
```

**流量說明**：
- 應用程式用 OpenInference 儀器化（官方 SDK / instrumentor），OTLP 送出 traces。
- 選配：中游放 **OTel Collector**（batch、retry、頻寬控制、多團隊 routing）——高吞吐時強烈建議，production guide 明確要求 batch processing + gRPC transport。
- Phoenix replicas 無狀態，共用同一個 PG；UI/API 查詢走 Ingress，traces 走 collector port。

---

## 4. 元件設計細節

### 4.1 追蹤管線（在應用程式側設定）

- **Batch processor**：span/metric/log 都要啟用（production guide 明載，高吞吐穩定性的關鍵）。
- **gRPC transport** 優先（壓縮率最高）。
- 應用程式內用 `PHOENIX_API_KEY`（system API key）打 bearer auth。
- 各團隊統一走組織標準的 OTLP endpoint（`https://phoenix.internal.example.com/v1/traces` 或 gRPC `phoenix.internal.example.com:4317`）。

### 4.2 Phoenix 應用層（K8s）

| 項目 | 建議 |
|---|---|
| 部署方式 | Helm chart `arizephoenix/phoenix-helm`，**pin 版本**（challenge：官方 Docker tag 用 `version-X.X.X`） |
| Replicas | 至少 2（滾動升級不中斷）；依吞吐調 3~N |
| 資源 | 起點 2 vCPU / 4 GiB per replica；以第 7 節方法壓測調整 |
| 環境變數（必設） | `PHOENIX_ENABLE_AUTH=True`、`PHOENIX_SECRET`（secret store 注入）、`PHOENIX_SQL_DATABASE_URL`、`PHOENIX_DEFAULT_RETENTION_POLICY_DAYS` |
| 環境變數（建議） | `PHOENIX_USE_SECURE_COOKIES=True`、`PHOENIX_CSRF_TRUSTED_ORIGINS`、`PHOENIX_ALLOW_EXTERNAL_RESOURCES=False`（內網）、`PHOENIX_ENABLE_STRONG_PASSWORD_POLICY=True`、`PHOENIX_ENABLE_PROMETHEUS=True` |
| 環境變數（安全性審查） | `PHOENIX_ENABLE_MCP_CODE_MODE=False`（生產環境關閉 sandbox code execution，除非明確需要）；`PHOENIX_ALLOWED_SANDBOX_PROVIDERS` 白名單 |
| Read replica | 高查詢量時設 `PHOENIX_SQL_DATABASE_READ_REPLICA_URL` |
| 就緒探針 | 6006 `/healthz`（如有）；沒有就做 TCP probe + UI 登入 smoke test |

### 4.3 資料層（Managed PostgreSQL）

| 項目 | 建議 |
|---|---|
| 服務 | RDS / Cloud SQL / Azure Database for PostgreSQL，**獨立 instance**（不與其他系統共用） |
| 版本 | ≥ 14（官方最低支援） |
| HA | Multi-AZ（或雲端 HA replica），自動故障轉移 |
| 備份 | 自動備份 + **PITR**；定期做 **test restores**（production guide 明載） |
| 加密 | 靜態加密（雲端預設）+ TLS（force SSL） |
| 容量 | 依 ingestion rate × retention × attribute 基數估算；監控 disk 使用率 |
| Schema 隔離 | 多團隊共享 DB 時，各 instance 設 `PHOENIX_SQL_DATABASE_SCHEMA`（例如 `phoenix_team_a`） |

### 4.4 身分與安全

**認證**：
1. `PHOENIX_ENABLE_AUTH=True`（注意：啟用後會**停止收集 trace**，直到建立 API key——要排維護時段啟用）。
2. `PHOENIX_SECRET`：≥32 字元隨機值，放 secret store（Vault / AWS Secrets Manager / K8s Secret），**所有 replica 必須相同**（JWT 簽章）。
3. 首次啟動的 admin（`admin@localhost`），用 `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` 設定初始密碼，登入後立即改。

**授權（RBAC）**：
- `admin`：完全控制（使用者、system API keys、secrets、AI provider 設定）
- `member`：開發者（送 traces、跑 experiments、建 datasets）
- `viewer`：唯讀（管理層、跨團隊觀看用）
- 大公司也可以考慮用**內建 OAuth2 authorization server**（`PHOENIX_ENABLE_OAUTH2_AUTHORIZATION_SERVER`）讓 CLI/MCP client 互動登入。

**API keys**：
- System API key：給應用程式寫入 traces、Phoenix Client、API 呼叫。
- 分 environment 使用不同 key（prod/staging 分開），輪替機制納入既有 secret rotation 流程。

**網路層**：
- Phoenix pod 用 **Cilium NetworkPolicy 轉 allow-list 模式**：只允許 → DB、cluster DNS、明確的 LLM provider 網域白名單。
- **阻擋 egress 到 private IP 範圍與 `169.254.169.254`**（metadata endpoint）——官方 production guide 明示這是 compromisation 後攻擊者的第一目標。
- Ingress 限內網/VPN；UI 若要對外，一律經 TLS + SSO 層（如公司既有 IdP 閘道）。

### 4.5 多團隊 / 多環境隔離策略

| 策略 | 做法 | 適用 |
|---|---|---|
| 環境隔離 | prod / staging / dev 各一套 instance + 各自 DB | 必做（production data 與開發完全隔離） |
| 團隊隔離 | 每團隊一套 instance + 各自 DB（或共享 DB 不同 schema） | 團隊間資料敏感度高的情況 |
| Schema 隔離 | 共享一台 DB，`PHOENIX_SQL_DATABASE_SCHEMA=team_a` 等 | 想省 DB 成本、可接受共享運算資源 |
| Centralized | 全公司單一 instance，RBAC 分權限 | 初期統一視野（官方 resource tags 2026 下半年後可做更細粒度） |

**建議（大型企業）**：**prod/staging/dev 三套環境 + prod 內以 schema 隔離分團隊**，DB 數量控制在可管理範圍。若法遵要求嚴格資料隔離，再升級為每團隊獨立 DB。

### 4.6 Phoenix 自身的監控告警

- `PHOENIX_ENABLE_PROMETHEUS=True` → 9090 metrics 進公司既有 Prometheus/Grafana。
- 自身 OTLP instrumentation（`PHOENIX_SERVER_INSTRUMENTATION_OTLP_TRACE_COLLECTOR_*_ENDPOINT`）送進自己的 tracing 體系。
- 告警建議：
  - Phoenix pod 無法就緒（503）
  - PG 連線數 / 磁碟使用率 > 80%
  - ingestion 延遲或 drop（batch 管線）
  - authentication 失敗次數突增（暴力破解）
  - retention cleanup 失敗

---

## 5. 部署與升級 Runbook

### 首次部署

1. 建立 managed PG（≥14，Multi-AZ，PITR，獨立 instance）。
2. 建立 K8s namespace（如 `observability`）、secret（`PHOENIX_SECRET`、DB URL、admin 初始密碼）。
3. Helm install `arizephoenix/phoenix-helm`，pin chart 版本 + 對應 Phoenix image `version-X.X.X`。
4. 以 `PHOENIX_ENABLE_AUTH=False` 先起（或直接 True + 準備好 API key；注意 auth 啟用後 trace 暫停的注意事項），驗證 UI 登入。
5. 建立 system API key → 寫入各應用程式的 secret。
6. 逐步接入各團隊的 traces，驗證端到端。
7. 開 Prometheus 監控 + 告警。

### 升級（最重要：migration）

Phoenix 升級可能含 **DB schema migration**，runbook 基本原則：

1. **先備份 DB**（PITR 快照或 pg_dump）。
2. 讀官方 release notes，確認 migration 是否需要停機（若有 DB 大改版）。
3. 滾動升級：一次升級一個 replica（或先升級一個驗證再推進）。
4. 驗證：UI 登入 → 新 trace 進得來 → 舊資料查得到 → evals 正常。
5. 保留 rollback 路徑：image 回上一版 + DB 若 migration 不可逆，用備份還原。

### 安全事件應變

- 若懷疑 pod 被入侵：依 NetworkPolicy 已擋 metadata endpoint 與內網掃描；斷開 egress → 輪替所有 API key / `PHOENIX_SECRET` → 還原 DB。

---

## 6. 備份與災難復原（DR）

| 項目 | 目標值（建議） |
|---|---|
| RPO | ≤ 5 分鐘（PITR 連續 WAL 歸檔） |
| RTO | ≤ 1 小時（Managed PG 自動故障轉移 + Helm 重部署） |
| 備份頻率 | 每日快照 + 連續 WAL（雲端 managed 服務標準） |
| 驗證 | 每季 test restore 一次，並驗證 UI 可查詢還原後資料 |
| 多區域 | 若公司要求跨 region DR：PG cross-region read replica + Phoenix 無狀態直接重建。注意 trace 延遲，通常用近端 region 為主 |

---

## 7. 規模估算指引

| 輸入 | 如何測量 | 對資源的影響 |
|---|---|---|
| 每日 span 數 | 從既有 OpenTelemetry 管線撈 | CPU（處理）+ 網路 |
| attribute 基數 | 唯一 label/attribute 數量 | **記憶體**（高基數是記憶體殺手） |
| 保留天數 | 公司 policy | 磁碟與 DB 索引、replica 讀取 |
| 並發查詢 | UI 使用者數 | replicas 數 + read replica 需求 |

實務建議：先以 2 replicas × 2 vCPU / 4 GiB + PG 最小 HA 規格起，跑 1~2 週真實負載，用 Prometheus 補資源曲線再調。**別一開始就買大**——Phoenix 主要瓶頸在 DB 磁碟 I/O 與記憶體，水平擴 replica 通常是後續最容易的一步。

---

## 8. 待確認決策清單（Open Questions）

- [ ] Q1：部署平台——公司已有 EKS/GKE/AKS 嗎？還是需一併規劃？
- [ ] Q2：場景規模——預估每日 trace / LLM call 量級？（1K / 100K / 1M+）
- [ ] Q3：團隊數與隔離需求——幾組團隊要用？需要嚴格資料隔離（法遵）嗎？
- [ ] Q4：SSO 需求——是否已有公司 IdP（Okta/Azure AD）？OSS 不支援 SAML，這點是否可接受（先用本地帳號）？
- [ ] Q5：是否已評估 Arize AX（企業版）？若 SSO/多租戶是硬需求，AX 要進決策矩陣。
- [ ] Q6：升級窗口——是否接受每月例行維護時段跑 migration？

---

## 附錄 A：參考文件

- Self-Hosting 總覽：https://docs.arize.com/phoenix/self-hosting
- Architecture（scaling/tenancy）：https://docs.arize.com/phoenix/self-hosting/architecture
- Configuration（env vars）：https://docs.arize.com/phoenix/self-hosting/configuration
- Production Guide：https://docs.arize.com/phoenix/production-guide
- Authentication：https://docs.arize.com/phoenix/self-hosting/features/authentication
- RBAC：https://docs.arize.com/phoenix/settings/access-control-rbac
- Docker/Compose：https://docs.arize.com/phoenix/self-hosting/deployment-options/docker
- Helm：https://docs.arize.com/phoenix/self-hosting/deployment-options/kubernetes-helm
- Data retention：https://docs.arize.com/phoenix/settings/data-retention