# NegaDownloader 對外 API 與 Token 存取規劃

## 目標與範圍

讓其他電腦或 agent 以服務網址及 API token 建立下載工作、查詢進度、取消自己的工作，並取得完成的檔案。API 工作與 WebUI 工作共用服務端已保存的網站 Cookies，讓遠端客戶端能使用既有登入狀態下載。第一版沿用現有 FastAPI、SQLite 與單一下載 worker，不另建一套佇列；原有 WebUI 登入與使用方式維持可用。

## 實作狀態（2026-10-25）

已加入 token 管理介面、`/api/v1/jobs` 系列端點、工作歸屬與權限檢查、共用 Cookies 流程驗證、限流、冪等建立及 README 範例。API token 可由 WebUI 建立與撤銷；每個 token 只能操作自己建立的工作。既有後端與前端測試已通過。

仍有限制：目前的 URL 檢查只涵蓋提交時的目標，無法全面保證 yt-dlp 後續轉址或重新解析不會觸及內網。部署時應只授權可信任的電腦或 agent；若需提供給不受信任的客戶端，須先增加可靠的出口網路隔離並驗證轉址案例。

## 現況盤點

- `app/main.py` 已有 `/api/jobs`、取消、檔案下載等端點，但一律使用瀏覽器 Session；寫入還要求 CSRF token。這些端點不是可直接用 Bearer token 呼叫的公開契約。
- `app/services/storage.py` 的 `jobs` 表只有工作資料，沒有建立者或 token 身分；目前 `list_jobs()` 會列出所有工作。
- `app/services/downloader.py` 預設會把共用的已匯入 Cookies 套用到下載。這正是對外 API 要沿用的核心能力；授予 token 等同授予使用該服務已保存網站登入狀態的能力。
- `_validate_public_http_url()` 會檢查提交當下的網址及 DNS，但 yt-dlp 後續可能重新解析或跟隨轉址；開放給遠端 agent 前需補強 SSRF 防護。
- Docker 已把 WebUI 的 `8080` 發布至主機預設 `20880`，並有反向代理 TLS 的部署建議。對外 API 可共用此 HTTPS 入口，不需另開 VNC 埠。

## 建議設計

### 1. 獨立、版本化的 API

新增 `/api/v1/*` 給機器客戶端。保留現有 `/api/*` 作為 WebUI 內部介面，避免更改原本的 Session/CSRF 行為。API 使用 `Authorization: Bearer <token>`；不接受 URL query 參數或 Cookie 當作 v1 API 的認證。FastAPI OpenAPI 需顯示 Bearer 驗證及請求／回應 schema。

| 端點 | 用途 | 權限 |
| --- | --- | --- |
| `POST /api/v1/jobs` | 以 `{"url":"https://..."}` 建立工作，回傳 `201`、工作 ID 與狀態 | `jobs:create` |
| `GET /api/v1/jobs` | 分頁列出此 token 建立的工作 | `jobs:read` |
| `GET /api/v1/jobs/{id}` | 查詢單一工作、進度與安全化錯誤 | `jobs:read` |
| `POST /api/v1/jobs/{id}/cancel` | 取消自己尚在佇列或執行中的工作 | `jobs:cancel` |
| `GET /api/v1/jobs/{id}/file` | 下載自己已完成的檔案 | `files:read` |

沿用現有狀態 `QUEUED`、`RUNNING`、`SUCCEEDED`、`FAILED`、`NEEDS_AUTH`、`CANCELLED`。建立工作後由客戶端輪詢單一工作端點；第一版先不做 webhook、SSE 或 WebSocket。`NEEDS_AUTH` 回傳明確狀態與提示，由管理員在 WebUI 處理登入；token 不可控制遠端瀏覽器。

v1 不提供全域清空佇列／紀錄、Cookies 匯入或刪除、下載目錄設定、yt-dlp 更新或回復、瀏覽器登入控制。這些操作仍留在 WebUI 管理介面。

### 2. Token 建立、儲存及撤銷

- 在已登入的 WebUI 新增「API Token」管理頁；建立、列表、撤銷都需原有 Session 與 CSRF。建立時填名稱、權限、有效期限；產生至少 256 bit 隨機 token，只顯示完整值一次。
- SQLite 新增 `api_tokens` 表：`id`、名稱、token 前綴、token 雜湊、權限、建立時間、到期時間、最後使用時間、撤銷時間。資料庫不保存明文 token；用固定格式前綴定位後，以雜湊與常數時間比較驗證。
- 預設權限為 `jobs:create`、`jobs:read`、`jobs:cancel`；`files:read` 由管理員明確勾選。Token 到期或撤銷後立即拒絕，回傳 `401`；權限不足回傳 `403`。
- 建立 token 的介面要明確告知管理員：持有 `jobs:create` 的客戶端可以利用所有適用的共用 Cookies 下載登入後可觀看的內容。只將 token 交給可信任的電腦或 agent；遺失時立即撤銷。
- 不把 token 印到應用程式日誌、錯誤、回應或下載檔名。文件提供安全保存與輪替步驟。API 呼叫只經 HTTPS 或可信任的私有網路／VPN；經公網部署時必須使用 TLS 反向代理。

### 3. 工作歸屬與共用 Cookies

- `jobs` 表新增 nullable `api_token_id`。WebUI 建立的既有及新工作保持 `NULL`；v1 建立的工作記錄 token ID。
- v1 查詢、取消與取檔必須先確認該工作屬於目前 token；不屬於該 token 或不存在都回 `404`，避免探查別人的工作。WebUI 管理員仍可查看與處理全部工作。撤銷 token 不刪除已建立工作或媒體，管理員仍可由 WebUI 清理。
- v1 建立的工作沿用目前 worker 的共用 Cookie jar 選擇邏輯：有適用的 Cookies 就自動使用；沒有則依現有流程嘗試下載。客戶端無須額外欄位或 `cookies:use` 權限。WebUI 更新 Cookies 後，尚未開始或重新排入佇列的 API 工作也使用當時最新的共用 Cookies。
- API 不提供 Cookie 值、檔案或瀏覽器設定檔的讀取／匯出端點。`NEEDS_AUTH` 工作由管理員在 WebUI 完成登入或匯入 Cookies，並由 WebUI 重試；API 客戶端可接著查詢同一工作的狀態。

### 4. 遠端濫用與網路安全

- URL 驗證沿用 HTTP/HTTPS、公網 IP、拒絕帳密與本機網域限制；在允許不受信任的 agent 提交網址前，還需確認 yt-dlp 重新解析、轉址、代理設定及 extractor 的網路行為不會連到內網或雲端中繼資料位址。若無法可靠封鎖，部署層應限制容器連內網，且首版只允許可信任的 token 持有者。
- 加入每 token 的建立頻率及未完成工作數上限，避免 agent 重試迴圈塞滿單一 worker 或磁碟；超限回 `429`，附 `Retry-After`。既有下載逾時設定繼續適用。
- `POST /jobs` 建議支援 `Idempotency-Key`：同一 token 與 key 重送同一請求時回原工作，內容不同回 `409`，避免網路逾時造成重複下載。Key 設保存期限及資料庫唯一約束。
- 檔案端點重用既有「檔案必須位於下載根目錄且確實存在」檢查，加上 token 歸屬驗證；回應設 `Cache-Control: private, no-store`。API 回應只顯示安全化 URL、檔名與錯誤，不暴露主機絕對路徑。

## 預期呼叫方式

```http
POST /api/v1/jobs HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json
Idempotency-Key: video-20261025-001

{"url":"https://example.com/video"}
```

```json
{"id":"<job-id>","status":"QUEUED","progress":0,"created_at":"<UTC timestamp>"}
```

客戶端再呼叫 `GET /api/v1/jobs/{id}` 輪詢。完成後，具 `files:read` 權限的同一 token 呼叫 `/file`；若工作需要登入，回 `NEEDS_AUTH`，並請管理員到 WebUI 處理。

## 實作順序

1. **資料與驗證**：新增 token 表與工作歸屬欄位；資料庫遷移要保留既有資料。封裝 token 建立、雜湊驗證、到期／撤銷與 scope 判斷。
2. **共用 Cookies 驗證**：確認 v1 建立的工作沿用現有 worker 的 Cookie 快照流程；測試有／無適用 Cookies、更新 Cookies 後重試、容器重啟後恢復工作，且 API 回應不洩漏 Cookie 值。
3. **v1 介面**：實作建立、列表、詳情、取消、取檔；重用現有 URL 檢查、序列化與工作服務，但驗證路徑與 WebUI 分離。補分頁、頻率／佇列上限與冪等重試。
4. **管理 UI 與文件**：加入 token 建立／撤銷介面，更新 README、`.env.example` 與 OpenAPI 範例；說明 HTTPS、權限選擇、輪替及 agent 輪詢方式。
5. **部署前安全檢查**：驗證轉址及解析後內網阻擋策略；若仍有缺口，文件明確限制在可信任的 VPN／私有網路與可信任 agent 使用。

## 驗收條件

- 新 token 可由另一台電腦帶 Bearer header 建立工作，輪詢至完成並在授權時下載檔案；無須瀏覽器 Cookie 或 CSRF。
- 缺少、錯誤、到期、撤銷的 token 都不能使用 v1；權限不足不能取檔或取消。
- A token 看不到、無法取消或下載 B token 的工作；WebUI 仍可管理所有工作，既有資料遷移後可正常使用。
- v1 工作預設使用適用的共用 Cookies；不需要客戶端另行匯入 Cookie 或指定開關。API 回應不含 Cookie 值，管理員在 WebUI 更新登入狀態後可重試原工作。
- 非公開網址、內網／本機目標與測試轉址案例被阻擋；超量建立工作有明確限制，重送冪等請求不產生重複工作。
- 現有 WebUI Session/CSRF 測試、下載佇列、Cookie 登入及 yt-dlp 更新流程沒有退化。

## 實作前待定的產品選擇

此計畫以「每個 token 只能管理自己建立的工作」作為工作資料的預設範圍；所有 token 建立的工作都能使用共用 Cookies。若實際需求是多個 agent 共用同一批工作，應先定義共同帳號／專案及其授權模型，再調整工作可見範圍。若 agent 只需提交下載、不需從 API 取回檔案，可不授予 `files:read`。
