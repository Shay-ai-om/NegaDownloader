# NegaDownloader API 使用教學

本指南說明如何從另一台電腦或 agent 使用 NegaDownloader 的 HTTP API 建立下載工作、查看進度、取消工作及取得檔案。API 工作會沿用服務內已保存的共用 Cookies。

## 1. 準備 API Token

1. 使用管理密碼登入 NegaDownloader WebUI。
2. 捲動至 **API Token** 區塊，輸入名稱並選擇有效期限與權限。
3. 按 **建立 Token**，立即複製並保存完整 token。完整值只會顯示這一次。
4. 若 token 遺失或不再使用，按該 token 旁的 **撤銷**。撤銷會立即阻止後續 API 請求。

Token 預設有效一年，也可選 30 天或永不過期。權限如下：

| 權限 | 能做什麼 |
| --- | --- |
| `jobs:create` | 建立下載工作；工作會使用服務端適用的共用 Cookies |
| `jobs:read` | 查看此 token 建立的工作及下載狀態 |
| `jobs:cancel` | 取消此 token 建立且仍在佇列或執行中的工作 |
| `files:read` | 下載此 token 建立且已完成的工作檔案 |

建立工作、查詢工作、取消工作預設已勾選；下載檔案權限需另外勾選。每個 token 只看得到自己建立的工作，WebUI 管理員仍能查看所有工作。

**安全提醒：**具有 `jobs:create` 權限的客戶端可以使用服務已保存的登入狀態下載內容。只把 token 交給可信任的電腦或 agent。透過 HTTPS 或可信任的私有網路／VPN 使用 API；不要把 token 放在網址、程式碼倉庫或日誌中。

## 2. 連線與驗證

API 使用與 WebUI 相同的服務網址。例如：

```text
https://downloader.example.com
```

每個請求都在 HTTP header 加上：

```http
Authorization: Bearer YOUR_API_TOKEN
```

API 不需要瀏覽器登入 Cookie 或 CSRF token。OpenAPI 互動文件位於服務網址的 `/docs`。

以下範例使用 `curl`。請把 `BASE_URL` 換成服務網址，並將 `YOUR_API_TOKEN` 換成建立時複製的 token。

```sh
BASE_URL='https://downloader.example.com'
TOKEN='YOUR_API_TOKEN'
```

Windows PowerShell 可改用：

```powershell
$BaseUrl = 'https://downloader.example.com'
$Token = 'YOUR_API_TOKEN'
$Headers = @{ Authorization = "Bearer $Token" }
```

## 3. 建立下載工作

呼叫 `POST /api/v1/jobs`，JSON body 的 `url` 必須是公開網站的完整 HTTP 或 HTTPS 網址。

```sh
curl -X POST "$BASE_URL/api/v1/jobs" \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: agent-video-20261025-001' \
  -d '{"url":"https://example.com/video"}'
```

PowerShell 範例：

```powershell
$Body = @{ url = 'https://example.com/video' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$BaseUrl/api/v1/jobs" `
  -Headers ($Headers + @{ 'Idempotency-Key' = 'agent-video-20261025-001' }) `
  -ContentType 'application/json' -Body $Body
```

成功建立時回傳 `201 Created`，格式如下：

```json
{
  "id": "工作 ID",
  "url": "https://example.com/video",
  "status": "QUEUED",
  "progress": 0.0,
  "filename": null,
  "error": null,
  "created_at": "2026-10-25T08:30:00+00:00",
  "updated_at": "2026-10-25T08:30:00+00:00"
}
```

請保存回傳的 `id`，後續用它查詢、取消或下載。

### 避免網路重試重複建立工作

可選擇傳入 `Idempotency-Key`，長度需為 8 至 128 個英數字或 `.`、`_`、`:`、`-`。同一 token 在 24 小時內以相同 key 和相同網址重送，會回傳原工作並使用 `200 OK`；若網址不同，回傳 `409 Conflict`。每次新的下載請使用新的 key。未提供 key 時，每次請求都會建立新工作。

每個 token 每分鐘最多建立 10 個新工作，最多同時有 20 個排隊或下載中的工作。超過限制時回傳 `429 Too Many Requests`，並提供 `Retry-After` header。

## 4. 查詢進度

### 查詢單一工作

```sh
curl "$BASE_URL/api/v1/jobs/JOB_ID" \
  -H "Authorization: Bearer $TOKEN"
```

PowerShell：

```powershell
Invoke-RestMethod -Method Get -Uri "$BaseUrl/api/v1/jobs/JOB_ID" -Headers $Headers
```

`progress` 是 0 至 100 的百分比。`filename` 只會在輸出檔案準備好時有值；API 不會回傳服務主機上的檔案絕對路徑。

### 列出自己的工作

```sh
curl "$BASE_URL/api/v1/jobs?limit=50&offset=0" \
  -H "Authorization: Bearer $TOKEN"
```

`limit` 預設為 50、最大為 100；`offset` 預設為 0。回應包含工作陣列及分頁資訊：

```json
{
  "jobs": [],
  "total": 0,
  "limit": 50,
  "offset": 0
}
```

### 工作狀態

| 狀態 | 說明 |
| --- | --- |
| `QUEUED` | 等待下載 |
| `RUNNING` | 正在下載；查看 `progress` 取得進度 |
| `SUCCEEDED` | 下載完成，可使用檔案端點（需 `files:read`） |
| `NEEDS_AUTH` | 需要登入或更新 Cookies |
| `FAILED` | 下載失敗；查看 `error` |
| `CANCELLED` | 工作已取消 |

agent 可每隔數秒呼叫單一工作端點，直到狀態變成 `SUCCEEDED`、`NEEDS_AUTH`、`FAILED` 或 `CANCELLED`。

## 5. 使用共用 Cookies 與登入處理

建立工作時，服務會依網址自動使用已保存且適用的 Cookies。agent 不必也不能透過此 API 讀取、匯出或上傳共用 Cookie jar。

如果工作變成 `NEEDS_AUTH`，管理員請登入 WebUI，在該工作使用遠端瀏覽器完成網站登入，或匯入新的 `cookies.txt`，再按 **使用登入狀態重試** 或 **使用 cookies 重試**。agent 繼續查詢原工作 ID 即可；工作重新排入佇列後狀態會回到 `QUEUED`。

## 6. 取消工作

呼叫 `POST /api/v1/jobs/{id}/cancel`。只可取消自己建立且仍在排隊或執行中的工作。

```sh
curl -X POST "$BASE_URL/api/v1/jobs/JOB_ID/cancel" \
  -H "Authorization: Bearer $TOKEN"
```

成功時回傳：

```json
{"status":"CANCELLED"}
```

已完成或已結束的工作不能取消，會回傳 `409 Conflict`。

## 7. 下載完成檔案

token 必須有 `files:read` 權限，且工作狀態為 `SUCCEEDED`。

```sh
curl -L "$BASE_URL/api/v1/jobs/JOB_ID/file" \
  -H "Authorization: Bearer $TOKEN" \
  -o video.mp4
```

PowerShell：

```powershell
Invoke-WebRequest -Uri "$BaseUrl/api/v1/jobs/JOB_ID/file" `
  -Headers $Headers -OutFile '.\video.mp4'
```

若尚未完成或找不到檔案，回傳 `404 Not Found`。別的 token 建立的工作也會回傳 `404`。

## 8. 錯誤回應

錯誤通常是 JSON 格式，訊息在 `detail` 欄位：

```json
{"detail":"API token 無效或已過期。"}
```

| HTTP 狀態碼 | 常見原因 | 建議處理 |
| --- | --- | --- |
| `400` | 網址或 `Idempotency-Key` 格式無效 | 檢查請求內容 |
| `401` | Token 缺少、錯誤、到期或已撤銷 | 確認 header；必要時在 WebUI 建立新 token |
| `403` | Token 沒有該操作權限 | 在 WebUI 撤銷並建立具所需權限的新 token |
| `404` | 工作不存在、屬於另一個 token，或檔案尚未就緒 | 確認工作 ID 與狀態 |
| `409` | Idempotency key 用於不同網址，或工作已無法取消 | 換新 key 或查詢最新狀態 |
| `422` | JSON 欄位缺少、型別錯誤或超出長度 | 依 `detail` 修正 body |
| `429` | 達到 token 的建立頻率或待處理工作上限 | 等待 `Retry-After` 秒數後重試 |

## 9. 端點速查

| 方法 | 路徑 | 必要權限 |
| --- | --- | --- |
| `POST` | `/api/v1/jobs` | `jobs:create` |
| `GET` | `/api/v1/jobs` | `jobs:read` |
| `GET` | `/api/v1/jobs/{id}` | `jobs:read` |
| `POST` | `/api/v1/jobs/{id}/cancel` | `jobs:cancel` |
| `GET` | `/api/v1/jobs/{id}/file` | `files:read` |

## 10. 網址與部署限制

建立工作只接受公開網站的完整 HTTP/HTTPS 網址，並會拒絕 localhost、私有 IP 與網址中的帳號密碼。yt-dlp 可能在處理時再次解析網域或跟隨轉址，因此目前應只把建立工作權限交給可信任的 agent。若需開放給不受信任的客戶端，部署端還需限制容器對內網的連線。
