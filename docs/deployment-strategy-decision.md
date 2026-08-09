# Phoenix 部署策略決策：每 App 自建 vs 中央平台統一建

> 版本：v1.0（2026-08-10）
> 適用：以 OSS Arize Phoenix 自架為前提（本 repo 已驗證的架構）
> 用途：提供給團隊/平台部門討論、決定治理模式的決策文件

---

## 1. 決策問題

LLM 應用程式（app/agent/MCP server）的 trace 觀測平台，公司應該：

- **A. 每個 app 自己建 Phoenix**（各自 instance、各自為政）
- **B. 統一建一個中央 Phoenix**（所有 app 共用）

---

## 2. 關鍵前提：先拆解「統一」的兩種意思

「統一建 Phoenix」有兩種解讀，結論完全不同：

| 解讀 | 意思 | OSS Phoenix 辦得到嗎 |
|---|---|---|
| **B1. 單一 instance 給所有 app 用** | 一個中央服務、所有資料在同一份 | ❌ **辦不到專案隔離**：OSS 是單租戶，RBAC 只有 admin/member/viewer，無法 per-project 權限（細粒度 resource tags 官方規劃中、尚未釋出） |
| **B2. 平台統一管理（instance 可多個）** | 一個平台團隊負責所有 Phoenix instance，共用 PG、監控、升級、範本 | ✅ 這才是可行且推薦的模式 |

**核心洞察：在 OSS 下，A 和 B2 其實都是「多 instance」——真正的差別是「誰來管」。**
因此本決策的本質是**治理模式**（分散 vs 集中），不是 instance 數量。

---

## 3. 三種模式總覽

```
A. 每 app 自建                B2. 中央平台統一建（推薦）        B1. 單一 instance（不可行）
┌─────┐ ┌─────┐ ┌─────┐     ┌─ 平台團隊（SRE/Observability）─┐   ┌───────────┐
│App1 │ │App2 │ │App3 │     │  Helm values 範本              │   │ Phoenix ×1 │
│PX+DB│ │PX+DB│ │PX+DB│     │  ├─ instance app1 (schema_1)  │   │ 所有 app   │
└─────┘ └─────┘ └─────┘     │  ├─ instance app2 (schema_2)  │   │ 的 trace   │
各自維運、各自升級、         │  └─ instance app3 (schema_3)  │   │ 全混一起   │
各自備份、互看不見           │  共用 PG + 監控 + 升級流程     │   │ 無法分權限 │
（隔離最強，成本最高）       └───────────────────────────────┘   └───────────┘
```

---

## 4. 對照表

| 維度 | A. 每 app 自建 | B2. 中央平台統一建 |
|---|---|---|
| 隔離程度 | 最徹底（物理分離） | 同樣能做到（schema 隔離 / 獨立 DB） |
| 維運成本 | ❌ N 個 app = N 套升級、備份、監控、故障處理 | ✅ 一套流程管全部 |
| 標準化 | ❌ 各團隊自行其是（auth、retention、告警各異） | ✅ 統一模板：auth、retention、key 輪替、告警 |
| 公司級視野 | ❌ 無法彙總「全公司 LLM 成本/品質」 | ⚠️ 單 team 好查；**跨 instance 總覽 OSS 仍做不到**（→ 見 §6） |
| 升級風險 | 一次只影響一個 app | 平台控管：staging 驗證 → 滾動 → 有 runbook 與 rollback |
| 資源效率 | 每個 app 各自一台 overhead | 共享一台 PG；低量級（千級 trace/日）一台綽綽有餘 |
| 擴展新 app | 從零摸索、重新設定 | 自助式：申請 `team_X` → 開 instance + schema → 給 key |

---

## 5. 決策樹

```
公司有平台團隊（SRE/Observability）嗎？
├─ 沒有 ──→ 先走 A（每 app 自建），等有人願意當平台主人再收斂
└─ 有 ──→ 法遵要求「連共用 DB 都不行」？
          ├─ 是 ──→ A（或 B2 變體：每 app 獨立 DB，平台仍統一管範本/升級）
          └─ 否 ──→ 某 app 量級懸殊、怕被別人塞車影響？
                    ├─ 是 ──→ 大部分走 B2，巨型 app 例外獨立
                    └─ 否 ──→ ✅ B2（中央平台 + 多 instance + schema 隔離）
```

---

## 6. OSS 的限制與何時該考慮 Arize AX

| 需求 | OSS 自架 | Arize AX（商業版） |
|---|---|---|
| 單 instance 內 per-project 權限 | ❌ | ✅（org/space 多租戶 + SAML SSO） |
| 跨 instance/app 彙總儀表板 | ❌（各 instance 資料物理分離） | ✅ |
| 企業級 SSO / JIT 使用者佈建 | ❌（本地帳號 + 內建 OAuth2） | ✅ |
| 高吞吐 OLAP 分析 | ❌（PG 有其極限） | ✅（專屬 OLAP 資料庫） |

**建議**：若「公司高層要一個網址看全部 + 又能分權限」是硬需求，直接評估 AX；
否則先用 B2（OSS 自架）跑起來，等需求浮現再遷移（資料在 PostgreSQL，遷移成本可控）。

---

## 7. 推薦架構（B2）：中央平台藍圖

```
                    ┌──────────────────────────────────────┐
                    │ 平台團隊（Observability）            │
                    │  - Helm values 範本（本 repo deploy/）│
                    │  - 共用 managed PG（schema 隔離）    │
                    │  - 共用 ingress / Prometheus / 告警   │
                    │  - 升級 runbook（備份→滾動→驗證→回滾）│
                    └───────┬──────────────┬──────────────┘
        App1 team        App2 team        App3 team
            │                │                │
    phoenix-app1      phoenix-app2      phoenix-app3
    (schema app1)     (schema app2)     (schema app3)
            └────────────────┴────────────────┘
                    共用 PostgreSQL ≥14
                    （或法遵嚴格：各自獨立 DB）
```

- 本 repo 已驗證的技術骨幹：`deploy/values-team-a.yaml` / `values-team-b.yaml`
  （共用 PG + `database.postgres.schema` 隔離）＋ `verify_isolation.py` 隔離驗證。
- 各 app 開發期另用個人本地 instance（本 repo `docs/local-implementation-notes.md`），與中央平台分開。

---

## 8. 給討論會的問題清單

1. 有沒有平台團隊（或願意成立的）？沒有 → 短期先 A。
2. 老闆需要的總覽是「技術除錯視圖」還是「公司級成本/品質儀表板」？後者 → AX 要進決策。
3. 法遵是否要求 app 之間「連基礎設施都不可共用」？是 → 每 app 獨立 DB 的變體。
4. 各 app 量級是否懸殊到需要 burst isolation？
5. 誰負責升級與 migration？升級是 Phoenix 最大的操作風險（DB schema migration），平台團隊接手會比每個 app 各自踩雷划算。

---

## 附錄：本 repo 與此決策的對應

| 決策選項 | 對應 repo 產物 |
|---|---|
| B2 的技術驗證 | `deploy/values-team-a.yaml`、`values-team-b.yaml`（schema 隔離）、`verify_isolation.py` |
| 升級/備份 runbook | `docs/production-deployment-plan.md` §5、§6 |
| 平台標準化設定 | `deploy/k8s-deployment-guide.md`（auth、retention、NetworkPolicy） |
| 開發期個人 instance | `docs/local-implementation-notes.md` |