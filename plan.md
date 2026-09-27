# NegaDownloader：需要登入影片的下載流程規劃

## 目標

讓以 Docker 執行、預計部署於 Unraid 的 NegaDownloader，在 yt-dlp 因登入需求而無法取得影片時，能讓用戶完成登入後重試下載。優先提供應用程式內的互動式登入流程；另外支援匯入 yt-dlp 可讀取的 cookies.txt，作為遠端瀏覽器不可用時的備援。

用戶只在第三方網站的登入頁輸入帳密；NegaDownloader 不收集或保存網站密碼。此流程只適用於用戶本來就有權限觀看的內容，並不保證網站一定允許下載。

## 目前假設

- 專案工作目錄目前沒有既有程式碼或部署文件；本計畫以新增登入與下載服務為起點。
- 主要使用者是單一家庭／私人 Unraid 部署，不先處理多租戶帳號與大量平行下載。
- 第一個互動式登入整合目標為 Instagram，架構保留其他 yt-dlp 支援網站的擴充空間。
- 使用者可由瀏覽器存取 Unraid 上的 NegaDownloader 網頁介面。

## 建議方案

### 優先：應用程式內的遠端瀏覽器登入

用獨立的 Chromium 設定檔啟動可互動瀏覽器，經由受保護的 WebSocket/VNC 閘道顯示在 NegaDownloader 網頁介面。下載服務在本機端不能直接讓用戶的電腦跳出視窗，因此產品呈現為下載頁中的登入對話框／嵌入式瀏覽器工作區，而不是宿主機視窗，也不以 iframe 直接嵌入 Instagram 頁面。

用戶完成網站登入、兩步驟驗證或網站要求的人工確認後，按下「使用此登入狀態重試」。服務從專用瀏覽器工作階段取得網站 cookies，轉成 Netscape 格式的暫存 cookies 檔，再由 yt-dlp 以 `--cookies` 讀取。這樣不必要求用戶預先從日常瀏覽器匯出 cookies，也不需把網站密碼交給 NegaDownloader。

瀏覽器設定檔需專供 NegaDownloader 使用，避免把用戶其他網站的 cookies 一併匯出。首版只開放 Instagram 登入，匯出 cookies 時限縮到該整合需要的網域；未來新增網站時，各自定義允許網域。

### 備援：匯入 cookies.txt

設定頁提供 `.txt` 匯入及移除功能，只接受 Mozilla/Netscape cookie 格式。匯入前驗證格式與必要欄位，介面僅顯示是否已設定及最後更新時間，不回顯 cookie 值。匯入後允許用戶測試登入狀態並重試原工作。

## 使用流程

1. 用戶貼上影片網址並建立下載工作。
2. Worker 先以現有的匿名下載方式執行 yt-dlp。
3. 只有在錯誤訊息符合已知登入／存取限制條件時，工作才轉為 `NEEDS_AUTH`；其他錯誤直接顯示可讀原因，不一律要求登入。
4. 工作頁顯示「此網站需要登入或人工驗證」，開啟遠端瀏覽器登入對話框。已登入的用戶可直接重用現有工作階段。
5. 用戶在第三方網站頁面自行登入並完成網站要求的驗證，再按「使用此登入狀態重試」。
6. 服務產生限縮網域的 Netscape cookies 檔，以相同 Docker 網路出口 IP 重試 yt-dlp。
7. 成功時清除暫存 cookies、更新工作狀態並提供下載檔案；失敗時保留清楚錯誤及再次登入／匯入 cookies 的入口。
8. 用戶可移除已匯入的 cookies；管理介面不提供清除 Instagram 瀏覽器設定檔的按鈕。

## 下載位置與工作清理

Docker 部署時由主機端選擇掛載到 `/downloads` 的資料夾；部署後，NegaDownloader 可在此掛載路徑內切換相對子資料夾。工作建立時會記錄當時的目的地，避免切換設定後改變佇列中工作的位置。WebUI 分別提供清空尚未開始工作的佇列及清除已結束工作紀錄；清除紀錄不刪除下載媒體。

工作狀態：

```text
QUEUED → RUNNING → SUCCEEDED
                  ↘ NEEDS_AUTH → QUEUED → RUNNING
                  ↘ FAILED
                  ↘ CANCELLED
```

## 系統組成

- **Web UI/API**：新增下載工作、顯示狀態與進度、呈現登入對話框、cookies.txt 匯入與清除。
- **下載 Worker**：呼叫 yt-dlp 與 ffmpeg，管理工作重試及輸出目錄。
- **登入瀏覽器**：Playwright 啟動持久化的獨立 Chromium 使用者資料目錄；以 VNC/noVNC 或等效遠端顯示介面提供人工操作。瀏覽器控制端點只允許由 NegaDownloader 後端代理，不能直接公開到 LAN/WAN。
- **持久化資料**：`/config` 保存工作資料庫、登入瀏覽器設定檔及匯入的登入狀態；`/downloads` 保存下載檔案。部署文件說明這兩個目錄在 Unraid 的 host path 對應方式。
- **Docker 部署**：首版以單一 NegaDownloader image 包含應用程式、yt-dlp、ffmpeg、Playwright Chromium 及虛擬顯示元件，降低 Unraid 初次安裝的設定量；瀏覽器存取仍限於應用程式內部代理。服務啟動、關閉與 Chromium 孤兒程序清理由容器 entrypoint 管理。

## 安全與隱私要求

- cookies 與瀏覽器設定檔等同登入憑證：存於 `/config`、限制檔案權限、禁止寫入一般日誌或工作錯誤訊息；暫存匯出檔於重試完成後立即刪除。
- 遠端瀏覽器與匯入功能必須經過 NegaDownloader 本身的存取控制；若部署在反向代理後，文件需要求使用 HTTPS，並明確提醒不要將服務或 VNC 埠直接公開至網際網路。
- Docker `/config` 保存登入狀態與工作資料；文件說明如何保護設定目錄。
- 網址只接受 `http` / `https`，避免將任意本機檔案或非 HTTP 協定交給 yt-dlp；依既有產品設計加入合理的 URL/網路存取限制，避免服務被用來探測 Unraid 內網資源。
- 不嘗試自動破解 CAPTCHA、繞過網站驗證或規避存取限制；需要驗證時交由用戶在官方網站頁面自行處理。用戶若無權存取該內容，服務不得協助取得。

## 分階段實作

### Phase 1：下載工作與 cookies.txt 備援

- 建立可追蹤的工作狀態與 yt-dlp 執行紀錄。
- 支援受限格式驗證的 Netscape cookies.txt 匯入、替換、移除。
- 將 cookies 以 `--cookies` 傳給 yt-dlp；錯誤與日誌遮蔽敏感資料。
- 加入 `/config`、`/downloads` Docker volume 與 Unraid 部署說明。

### Phase 2：遠端登入工作區

- 加入獨立持久化 Chromium 工作階段及網頁內遠端顯示/輸入介面。
- 先完成 Instagram 登入與工作階段重用；不在 UI 取得或保存用戶帳密。
- 實作 cookies 擷取、網域過濾、Netscape 格式轉換、登入後重試及清除工作階段。
- 在 UI 解釋這是一個跑在 Unraid 上的遠端瀏覽器，不是用戶電腦上的彈出視窗。

### Phase 3：穩定性與擴充

- 將登入錯誤分類、登入狀態過期、網站 checkpoint、網路錯誤與 yt-dlp extractor 失效分開處理。
- 加入工作逾時／取消、重試上限、下載併發限制與瀏覽器程序回收。
- 依實際需要擴充其他 yt-dlp 網站整合及對應 cookies 網域清單。
- 完成 Unraid 安裝、更新、備份、還原與疑難排解文件。

## 驗收條件

- 不需要登入的公開影片可照原有流程下載。
- 登入限制能讓工作進入 `NEEDS_AUTH`，並在網頁介面打開可互動的遠端登入工作區。
- 用戶可在遠端瀏覽器手動登入（含人工完成網站支援的 2FA/checkpoint），返回工作頁後重試並完成其可觀看的測試影片下載。
- 匯入有效 Netscape cookies.txt 後，可用它重試下載；格式錯誤時提供明確訊息，不洩漏 cookie 內容。
- 重啟容器後，設定的登入狀態及下載工作仍依 volume 設定保存；用戶執行登出/清除後，舊登入狀態不再使用。
- Cookies、帳號密碼、登入頁畫面內容不寫入一般應用程式日誌；VNC/瀏覽器控制服務無法由未授權的直接埠存取。
- Unraid 使用者可依文件建立容器，掛載設定與下載目錄並從網頁介面完成上述流程。

## 主要限制與風險

- Instagram 可能依帳號、IP、裝置信任、地區或短時間下載頻率要求額外驗證或封鎖請求；Cookies 不保證能讓 yt-dlp 下載所有已登入可觀看的影片。網站驗證或防護改變時，使用者可能仍需在瀏覽器處理，或該影片無法下載。
- yt-dlp FAQ 提醒網站可能同時要求相同的 cookies 與網路出口 IP；因此登入瀏覽器和下載 Worker 應使用同一個容器/網路出口。其文件也指出 cookies 檔含有高敏感性登入資料。
- 持久化登入雖省去每次匯入 cookies，卻讓 `/config` 成為敏感資料；應限制 NAS 檔案存取並納入備份保護考量。
- 使用者需確認下載、保存及分享影片符合平台規則與內容權利人授權。

## 參考資料

- [yt-dlp FAQ：How do I pass cookies to yt-dlp?](https://github.com/yt-dlp/yt-dlp/wiki/FAQ#how-do-i-pass-cookies-to-yt-dlp)：說明 `--cookies`、`--cookies-from-browser`、Netscape 格式，以及 cookie 檔的敏感性與同 IP 注意事項。
- [Playwright BrowserType：launchPersistentContext](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-persistent-context)：持久化 user data directory 可保存 browser session 資料，且不能同時啟動多個使用相同目錄的 browser instance。
- [Playwright BrowserContext：cookies](https://playwright.dev/docs/api/class-browsercontext#browser-context-cookies)：提供取得 browser context cookies 的 API，作為轉換成 yt-dlp cookie 檔的實作基礎。
