# Phoenix Auth 程式碼地圖（帳號密碼登入功能從哪來）

> 版本：基於 **arize-phoenix 19.19.1**（本 repo `.venv`），2026-08-09 逐行驗證。
> 所有路徑相對於 `.venv/Lib/site-packages/phoenix/`。
> **注意：行號會隨版本漂移**，升級後請用文末的 grep 關鍵字重新定位。

## 0. 一句話總結

開關是 `config.py` 讀的 `PHOENIX_ENABLE_AUTH` → `serve.py:376` 傳給 `create_app` → `app.py:1129` 決定
要不要掛 `/auth/login` router 與 auth middleware → 帳號在 `facilitator.py` 首次啟動時用 PBKDF2 建立 →
之後每個請求由 `BearerTokenAuthBackend` 驗證 cookie/bearer token → `api/auth.py` 的權限類別決定能不能做。

## 1. 開關（環境變數 → 程式）

| 開關 | 讀取處 | 作用 |
|---|---|---|
| `PHOENIX_ENABLE_AUTH=True` | `config.py:376` 定義、`:1224` `get_env_enable_auth()` | **總開關**，預設 False |
| `PHOENIX_SECRET` | `config.py:398`、`:1355` `get_env_phoenix_secret()` | JWT 簽章密鑰（>=32 字元）；所有 replica 必須相同 |
| `PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD` | `config.py:416` | bootstrap admin 初始密碼（只在首次啟動讀） |
| `PHOENIX_ADMIN_SECRET` | `bearer_auth.py:69`（`get_env_phoenix_admin_secret()`） | 後門：設了之後，任何請求帶此值當 bearer token 直接放行（適用 CI/機器人） |
| `PHOENIX_USE_SECURE_COOKIES` / `PHOENIX_CSRF_TRUSTED_ORIGINS` | `auth.py:101` 附近（cookie 設定） | 正式環境建議開啟 |
| `PHOENIX_ENABLE_STRONG_PASSWORD_POLICY=True` | `auth.py:80` `validate_password_format` | 密碼規則：12 字元+大小寫+數字+特殊字元 |
| `PHOENIX_BRUTE_FORCE_LOGIN_PROTECTION_MAX_ATTEMPTS` | `auth.py routers:80` `_check_brute_force_limit` | 預設 5 次失敗鎖 5 分鐘 |

**核心驗證邏輯**：`config.py:1450` `get_env_auth_settings()`

```python
enable_auth = get_env_enable_auth()
phoenix_secret = get_env_phoenix_secret()
if enable_auth and not phoenix_secret:
    raise ValueError("`PHOENIX_SECRET` must be set when auth is enabled with `PHOENIX_ENABLE_AUTH`")
```

→ 開 auth 沒設 secret 直接啟動失敗（我們本地啟動時兩個都設了）。

## 2. 啟動組裝（開關 → app）

```text
main.py（CLI entry，subparsers: serve / db）
  └─ cli/commands/serve.py:248   auth_settings = get_env_auth_settings()
                            :376  app = create_app(authentication_enabled=auth_settings.enable_auth)
                                  └─ server/app.py 組裝：
                                     :833  GraphQL router dependencies=(Depends(is_authenticated),)
                                           （auth 啟用時才掛）          ← UI/API 查詢靠這層
                                     :1123 create_v1_router(authentication_enabled)
                                           （REST /v1：traces、projects…）← 收 trace 靠這層
                                     :1129 if authentication_enabled:
                                              include create_auth_router() + oauth2 routers
                                     :993  backend=BearerTokenAuthBackend(token_store)
                                           （Starlette AuthenticationMiddleware）← 每個請求都過
                                     :860  401 handler → 重導 /login        ← 登入畫面的來源
```

## 3. bootstrap admin 建立（首次啟動、DB 初始化時）

`db/facilitator.py:137-158`

```python
# SYSTEM user（內部用，不是人）
system_user = models.LocalUser(..., reset_password=False,
    password_salt=secrets.token_bytes(...), password_hash=secrets.token_bytes(...))

# ADMIN user（admin@localhost）
salt = secrets.token_bytes(DEFAULT_SECRET_LENGTH)
password = get_env_default_admin_initial_password()      # ← PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD
hash_ = await loop.run_in_executor(None, compute_password_hash, ...)
admin_user = models.LocalUser(..., email=DEFAULT_ADMIN_EMAIL,  # "admin@localhost"
    password_salt=salt, password_hash=hash_, reset_password=True)  # ← 強制改密碼的 flag
```

- **密碼演算法**：`auth.py:22` `compute_password_hash()` = **PBKDF2-HMAC-SHA256**
  （`pbkdf2_hmac("sha256", password_bytes, salt, NUM_ITERATIONS)`）
- **`reset_password=True`**：`db/models.py:2157`（欄位）+ `:2280`（LocalUser 預設 True）
  → 第一次登入 UI 強制要求換密碼；`auth.py routers:325` 換完設回 `False`。
- 由 admin 在 UI 建的其他使用者走不同路徑（`reset_password=False`，`models.py:2314`）。
- 上面 hash 用隨機 salt，所以兩台 instance 即使同密碼，DB 裡的 hash 也不同。

## 4. 登入流程（`POST /auth/login`）

`server/api/routers/auth.py`

```text
create_auth_router()  :99    prefix="/auth"；rate limiter 掛在 /auth/login 等路徑（暴力破解鎖定）
         │
_login()              :144   ① data=email/password → sanitize_email(:53，trim+小寫)
                             ② 查 User 表（lower(email) 比對，joinedload role）
                             ③ is_valid_password()（auth.py:37：重算 PBKDF2 比對，constant-time 比較）
                             ④ 失敗 → _record_brute_force_failure(:94) + 401
         │
_create_auth_response():373  create_access_and_refresh_tokens()
                             set_access_token_cookie()   → cookie: phoenix-access-token
                             set_refresh_token_cookie()  → cookie: phoenix-refresh-token
                             回傳 204 No Content
```

**JWT 相關**（產生/驗證/撤銷都在 `server/jwt_store.py`）：

| 類別 | 行號 | 用途 |
|---|---|---|
| `JwtStore` | :73 | JWT 簽章/讀取入口（用 `PHOENIX_SECRET`） |
| `_AccessTokenStore` | :419 | 短效 access token（有 DB 記錄、可撤銷） |
| `_RefreshTokenStore` | :519 | 長效 refresh token（輪換用） |
| `_ApiKeyStore` | :623 | system/user API key（也是 JWT，存 DB token id） |

`auth.py:327` `class Token(str)`：token 型別。

## 5. 每個請求的驗證（middleware）

`server/bearer_auth.py:50` `BearerTokenAuthBackend.authenticate()`

```text
① conn.scope 內已有內部 principal → 直接放行（內部 in-process dispatch）
② Authorization header 存在：
     scheme=bearer → token
       若 PHOENIX_ADMIN_SECRET 有設且 token 等於它 → 以 SYSTEM user 放行（後門）
③ 否則 phoenix-access-token cookie → token
④ token_store.read(token) 驗證簽章/期限/audience
   → PhoenixUser(claims.subject, claims) 或 None（None = 401）
```

- gRPC OTLP 的 key 驗證：`bearer_auth.py:124` `ApiKeyInterceptor`（`AsyncServerInterceptor`）。
- 401 之後：`app.py:860` 的 exception handler 在 auth 啟用時把瀏覽器請求重導到 `/login`。

## 6. 授權層（誰能幹嘛）

`server/api/auth.py`（Strawberry permission classes）：

| 類別 | 行號 | 意義 |
|---|---|---|
| `Authorization` (ABC) | :13 | 基底 |
| `IsNotReadOnly` | :18 | 非唯讀 |
| `IsAuthEnabled` | :25 | auth 啟用時才生效 |
| `IsNotViewer` | :32 | member + admin |
| `IsLocked` | :41 | 帳號未被鎖 |
| `IsAdmin` | :76 | 僅 admin |
| `IsAdminIfAuthEnabled` | :85 | 開 auth 才要求 admin |

範例：建 system API key 的 mutation（`api_key_mutations.py:85`）：

```python
@strawberry.mutation(permission_classes=[IsNotReadOnly, IsNotViewer, IsAdmin, IsLocked])
async def create_system_api_key(...)
```

角色定義：`db/models.py`（UserRole、`reset_password` 欄位 :2157）；`/settings` UI 管理使用者與 system API key。

## 7. 六個關鍵檔案速查

| 想了解 | 看哪 | grep 關鍵字（版本漂移時用） |
|---|---|---|
| 開關/驗證 | `config.py`（:1224, :1355, :1450） | `ENV_PHOENIX_ENABLE_AUTH`、`get_env_auth_settings` |
| 組裝 | `server/app.py`（:833, :1123, :1129）+ `cli/commands/serve.py`（:376） | `authentication_enabled`、`create_auth_router` |
| 建帳號 | `db/facilitator.py`（:137） | `DEFAULT_ADMIN_EMAIL`、`get_env_default_admin_initial_password` |
| 密碼演算法 | `auth.py`（:22, :37） | `compute_password_hash`、`pbkdf2_hmac` |
| 登入/換密碼 | `server/api/routers/auth.py`（:144, :297） | `_login`、`_reset_password` |
| 請求驗證/授權 | `bearer_auth.py`（:50）+ `api/auth.py` | `BearerTokenAuthBackend`、`class IsAdmin` |

## 8. 本地實驗對應（這份 repo 的實際設定）

| 項目 | Instance A (6006) | Instance B (6007) |
|---|---|---|
| `PHOENIX_ENABLE_AUTH` | `True` | `True` |
| `PHOENIX_PORT` / `PHOENIX_GRPC_PORT` | 6006 / 4317 | 6007 / 4318 |
| `PHOENIX_WORKING_DIR` | `data/a` | `data/b` |
| 登入 email | `admin@localhost` | `admin@localhost` |
| 登入密碼（已於 UI 重設） | `ProjectA` | `ProjectB` |
| System API key | `.local-keys/instance_a.key` | `.local-keys/instance_b.key` |

> 重設密碼不影響 API key（JWT 簽章獨立於密碼）；但舊的 curl cookie jar 裡的
> access token 在到期前仍有效（JWT 無狀態）。