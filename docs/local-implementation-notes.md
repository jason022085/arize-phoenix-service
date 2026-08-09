# 本地 Phoenix 專案隔離實作筆記

> 對應 `docs/production-deployment-plan.md` 的「環境/團隊隔離」策略，在本地用多 instance 實作。
> 驗證結果：**專案 A 與專案 B 互相看不到對方的 trace**（API level：401/404 拒絕；DB level：不同 schema）。

## 架構

```
專案 A → Phoenix instance A  http://localhost:6006  OTLP gRPC :4317  資料 data/a
專案 B → Phoenix instance B  http://localhost:6007  OTLP gRPC :4318  資料 data/b
```

每個 instance：獨立帳號、獨立 DB（SQLite，位於各自 `PHOENIX_WORKING_DIR`）、獨立 system API key。
這個形狀 = 公司 K8s 的縮影：之後每組「instance + DB」換成 K8s 的 Deployment + PG schema 即可。

## 憑證（本地 demo 用，勿用於正式環境）

| 項目 | Instance A (6006) | Instance B (6007) |
|---|---|---|
| UI 登入 email | `admin@localhost` | `admin@localhost` |
| UI 登入密碼 | `ProjectA` | `ProjectB` |
| System API key | `.local-keys/instance_a.key` | `.local-keys/instance_b.key` |

> 注意：bootstrap admin 第一次登入會強制改密碼（`reset_password=True` 設計），改完即不再要求。
> 改密碼不影響 API key。

## 啟動（兩個背景行程）

```bash
cd "D:\GitHub Repos\arize-phoenix-service"

# Instance A
PYTHONPATH= PHOENIX_ENABLE_AUTH=True \
  PHOENIX_SECRET=<32+字元隨機hex> \
  PHOENIX_PORT=6006 PHOENIX_GRPC_PORT=4317 \
  PHOENIX_WORKING_DIR=data/a \
  ./.venv/Scripts/python.exe -m phoenix.server.main serve

# Instance B（換 port / working dir / secret）
PYTHONPATH= PHOENIX_ENABLE_AUTH=True \
  PHOENIX_SECRET=<另一個hex> \
  PHOENIX_PORT=6007 PHOENIX_GRPC_PORT=4318 \
  PHOENIX_WORKING_DIR=data/b \
  ./.venv/Scripts/python.exe -m phoenix.server.main serve
```

`PHOENIX_SECRET` 產生：`python3 -c "import secrets; print(secrets.token_hex(32))"`

## 建立 system API key（一次性）

登入拿 cookie → GraphQL 建 key：

```bash
# 登入（204 = 成功，cookie 存 jar）
curl -s -o nul -c jar_a.txt -X POST http://localhost:6006/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@localhost","password":"ProjectA"}'

# 建 key，jwt 在回應的 data.createSystemApiKey.jwt（存檔即可）
curl -s -b jar_a.txt -X POST http://localhost:6006/graphql \
  -H "Content-Type: application/json" \
  -d '{"query":"mutation { createSystemApiKey(input: {name: \"local-demo\"}) { jwt apiKey { name } } }"}'
```

## 送測試 trace & 驗證隔離

```bash
PYTHONPATH= ./.venv/Scripts/python.exe scripts/send_demo_traces.py A   # 3 spans → project-demo-a
PYTHONPATH= ./.venv/Scripts/python.exe scripts/send_demo_traces.py B   # 2 spans → project-demo-b
PYTHONPATH= ./.venv/Scripts/python.exe scripts/verify_isolation.py
```

驗證腳本預期輸出（全綠=隔離正確）：

```
A 的 key 查 A: projects=['default','project-demo-a']       200
B 的 key 查 B: projects=['default','project-demo-b']       200
B 的 key 打 A:                                            401 Invalid token
A 的 key 打 B:                                            401 Invalid token
無 key 直接查:                                             401 Invalid token
```

## 升級：共用 DB 分 schema（PostgreSQL）✅ 已實作

現況：兩台 instance 共用同一台 PostgreSQL（`phoenix_central`），各用獨立 schema 隔離。

### PostgreSQL 來源（本機免安裝）

EDB zip binaries（免 admin、免服務、用完可關）：https://get.enterprisedb.com/postgresql/postgresql-16.6-1-windows-x64-binaries.zip

```bash
unzip -q -o ~/.pg-dist/pg-16.6.zip -d ~/.pg-dist/
~/.pg-dist/pgsql/bin/initdb -D data/pgdata -U postgres -E UTF8 --locale=C -A trust
# 啟動：用 Hermes terminal(background=true) 跑（勿用 pg_ctl start——前台 postgres 會被
# terminal timeout 連坐殺掉，exception 0xC0000142）
~/.pg-dist/pgsql/bin/postgres.exe -D data/pgdata -p 5432
~/.pg-dist/pgsql/bin/createdb -h localhost -p 5432 -U postgres phoenix_central
```

### 啟動兩台（關鍵參數）

```bash
# Instance A —— schema: team_a
PHOENIX_SQL_DATABASE_URL="postgresql://postgres@localhost:5432/phoenix_central" \
PHOENIX_SQL_DATABASE_SCHEMA=team_a \
PHOENIX_PORT=6006 PHOENIX_GRPC_PORT=4317 ... serve

# Instance B —— schema: team_b（同 URL，schema 不同）
PHOENIX_SQL_DATABASE_URL="postgresql://postgres@localhost:5432/phoenix_central" \
PHOENIX_SQL_DATABASE_SCHEMA=team_b \
PHOENIX_PORT=6007 PHOENIX_GRPC_PORT=4318 ... serve
```

### 切換注意事項

1. **先裝 PG driver**：`uv pip install "arize-phoenix[pg]"`（PG 支援是 extra，`asyncpg`）。
2. **schema 自動建立**：`PHOENIX_SQL_DATABASE_SCHEMA` 指定的 schema 不存在時 Phoenix 首啟會自動建
   （官方文件明載；`config.py:3236` 確認 SQLite 連線時此設定會被忽略）。
3. **API key 必須重建**：key 的 token 記錄存在 DB 裡，從 SQLite 換到 PG 等於換了 token store，
   舊 key 全部失效 → 跑 `scripts/recreate_keys.py`（登入 bootstrap admin 重發 key 覆蓋 `.local-keys/`）。
4. **UI 密碼回到初始值**：換 DB 後帳號是全新 bootstrap（admin@localhost / 初始密碼），登入後又會被要求改密碼。
5. **Windows kill Python 不乾淨**：`process kill` 可能留下佔住 port 的殘骸 process →
   用 `netstat -ano | grep LISTEN` 找出 PID，`MSYS_NO_PATHCONV=1 taskkill /PID <pid> /F` 強殺
   （git-bash 的 `//PID` 會被 MSYS 原樣傳給 taskkill 而出錯）。

### 驗證（雙層）

```bash
# API 層：隔離照舊全綠（401/404）
PYTHONPATH= ./.venv/Scripts/python.exe scripts/verify_isolation.py

# DB 層：schema 各自完整、資料分開
psql -h localhost -p 5432 -U postgres -d phoenix_central \
  -c "SELECT table_schema, count(*) FROM information_schema.tables WHERE table_schema IN ('team_a','team_b') GROUP BY 1;"
psql ... -c "SELECT 'team_a', count(*) FROM team_a.spans UNION ALL SELECT 'team_b', count(*) FROM team_b.spans;"
```

## 踩過的坑（必讀）

1. **PYTHONPATH 污染**：本機 `PYTHONPATH` 指向 hermes-agent venv，會遮蔽專案 venv 的套件
   （fastapi 版本錯亂）。所有 phoenix/otlp 指令前要 `PYTHONPATH=` 清空。
2. **Python 版本**：Phoenix 19.19.1 在 **Python 3.11 會掛**（`MappingProxyType` dataclass
   預設值被 3.11 dataclasses 拒絕，官方 main 分支也還有）。**用 Python 3.12 建 venv**
   （`uv venv --python 3.12 .venv`）。
3. **curl 與 MSYS `/tmp`**：Windows native curl 不懂 `/tmp`（寫檔失敗）。輸出檔用 repo
   內相對路徑；`-o nul` 取代 `/dev/null`。
4. **長輸出會被終端截斷顯示**：JWT 等長字串看到的 `...` 多半是顯示截斷，資料本身完整，
   用「能不能用」驗證（如帶 header 打 API），別用肉眼判斷。
5. **OTLP 批次要 flush**：`BatchSpanProcessor` 預設 5 秒才送出，腳本結尾要
   `provider.shutdown()`（會 flush），不要只關 exporter，否則 trace 不會進 server。
6. **啟用 auth 後，trace 收集中斷**：直到建立 system API key 才恢復——正式環境要排維護時段。

## 安全提醒

- `.local-keys/*.key`、`data/`、`.venv/`、`jar_*.txt` 已在 `.gitignore`，不要手動 commit。
- 這些是本地 demo 憑證；正式環境密碼要換、key 進 secret store。
- 正式環境建議 `PHOENIX_ENABLE_STRONG_PASSWORD_POLICY=True`、`PHOENIX_USE_SECURE_COOKIES=True`。