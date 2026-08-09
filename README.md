# Arize Phoenix 軌跡彙總中心

LLM 應用程式跑起來像個黑箱：prompt 丟進去、文字吐出來，中間到底發生什麼事沒人知道。
**Arize Phoenix** 就是打開黑箱的工具——它把應用程式每次 LLM 呼叫的**軌跡（trace）**收集起來：
用了哪個模型、幾點呼叫、花了多少 token、回應了什麼、耗時多久，全部彙整成一個可搜尋、可視覺化的網頁介面。

這個 repo 是做「**軌跡彙總中心**」的落地方案：本機先實作、驗證，之後照藍圖上公司 Kubernetes。

---

## 目前做到哪裡

| 階段 | 狀態 |
|---|---|
| 本機多 instance 部署（Phoenix 19.19.1 + auth） | ✅ 跑起來了（6006 / 6007 兩台） |
| 專案隔離：A 看不到 B 的 trace | ✅ 已驗證（API 層 401/404 + DB 層分 schema） |
| **共用 PostgreSQL + 分 schema 隔離** | ✅ 已實作（一台 PG，`team_a` / `team_b`） |
| 生產部署藍圖（K8s + PostgreSQL） | 📄 `docs/production-deployment-plan.md` |
| 本機實作筆記（啟動、憑證、踩坑） | 📄 `docs/local-implementation-notes.md` |
| Auth 登入功能的程式碼地圖 | 📄 `docs/phoenix-auth-code-map.md` |

---

## 為什麼要「專案隔離」？為什麼是一個專案一個 instance？

關鍵限制：**Phoenix 開源版是單租戶**。共用同一個 instance 時，所有人的 trace 都在同一份資料裡，
RBAC 只能分 admin / member / viewer 三種角色，**無法細分到「這個專案的人只能看這個專案的 trace」**。

所以正解（也是官方架構文件建議的）是：**每個專案一個獨立 instance**——獨立帳號、獨立資料庫、獨立 API key。
隔離是物理性的，不是靠權限設定撐出來的：

```
專案 A → Phoenix instance A  http://localhost:6006  資料 data/a  鑰匙 key_a
專案 B → Phoenix instance B  http://localhost:6007  資料 data/b  鑰匙 key_b
```

之後搬上 K8s，這組形狀原封不動：只是本機的「port + 資料夾」換成「Deployment + PG schema」。

---

## 實驗結果（真的跑過的驗證）

各送了測試 trace（A 3 條、B 2 條）之後的隔離測試：

| 測試 | 結果 |
|---|---|
| A 的 key 查 A instance | ✅ 200，只看到 `project-demo-a` |
| B 的 key 查 B instance | ✅ 200，只看到 `project-demo-b` |
| **B 的 key 打 A 的端點** | ❌ **401 Invalid token** |
| **A 的 key 打 B 的端點** | ❌ **401 Invalid token** |
| 不帶 key 直接查 | ❌ **401 Invalid token** |
| A 查 B 的 project | ❌ **404 找不到** |

想自己重跑：`scripts/verify_isolation.py`。

---

## 檔案地圖

```
arize-phoenix-service/
├── README.md                       ← 你正在看這份
├── IDEA.md                         ← 最初的點子
├── docs/
│   ├── production-deployment-plan.md   生產環境藍圖：K8s 架構、高可用、升級/DR runbook
│   ├── local-implementation-notes.md   本機實作手冊：啟動指令、憑證、踩過的坑
│   └── phoenix-auth-code-map.md        auth 登入功能的原始碼導覽（含行號與 grep 關鍵字）
├── scripts/
│   ├── send_demo_traces.py             用 OTLP 送測試 trace 到指定 instance
│   └── verify_isolation.py             驗證跨 instance 隔離（401/404 測試）
├── data/                           SQLite 資料（a / b 各一間）
├── .local-keys/                    system API key（勿進 git）
└── .venv/                          Python 3.12 環境
```

---

## 快速開始

```bash
# 送測試 trace 到 A / B
PYTHONPATH= ./.venv/Scripts/python.exe scripts/send_demo_traces.py A
PYTHONPATH= ./.venv/Scripts/python.exe scripts/send_demo_traces.py B

# 驗證隔離
PYTHONPATH= ./.venv/Scripts/python.exe scripts/verify_isolation.py

# 開 UI
# http://localhost:6006  admin@localhost / ProjectA
# http://localhost:6007  admin@localhost / ProjectB
```

> 為什麼前面要 `PYTHONPATH=`？本機系統環境的 `PYTHONPATH` 指向別的 venv，會汙染套件版本，
> 詳見實作筆記的「踩過的坑」。

---

## 名詞對照

| 術語 | 意思 |
|---|---|
| trace / span | 一次 LLM 呼叫（或其子步驟）的完整紀錄 |
| OTLP | 應用程式把 trace 送給 Phoenix 的通訊協定（HTTP/gRPC） |
| instance | 一台獨立運作的 Phoenix 服務（含 UI + collector + 資料庫） |
| System API key | 應用程式寫入 trace 用的憑證（Bearer JWT） |
| 單租戶 | 一個 instance 的所有資料對所有登入者可見（僅分三種角色） |
| OpenInference | 讓各種 LLM 框架的 trace 格式統一的規範 |

---

## 下一步

- [x] 資料庫升級：SQLite ➜ **PostgreSQL**（多 instance 共享 DB + schema 隔離）✅ 已完成
- [ ] 部署到 **Kubernetes**（藍圖已備好：Helm、replicas、Cilium network policy）
- [ ] 接入 **MCP server**，把 agent 呼叫也納入追蹤