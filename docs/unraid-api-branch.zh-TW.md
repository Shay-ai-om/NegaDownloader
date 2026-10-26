# 在 Unraid 使用 `api` 分支版映像

`api` 分支的每次推送會由 GitHub Actions 建置並發布專用的 `api` 標籤，不會覆蓋正式版的 `latest`。

可用映像名稱：

- GitHub Container Registry：`ghcr.io/shay-ai-om/negadownloader:api`
- Docker Hub：`<你的 Docker Hub namespace>/negadownloader:api`

Docker Hub 映像只有在 GitHub Actions 已設定 `DOCKERHUB_USERNAME` 變數和 `DOCKERHUB_TOKEN` secret 時才會發布。推送分支後，先到 GitHub repository 的 **Actions** 頁面確認 **Publish Docker image** 工作完成；Docker Hub 發布工作若有啟用，也要確認該工作成功。映像建置完成後才能在 Unraid 拉取新標籤。

## 將已安裝的容器切換至 `api`

1. 在 Unraid 開啟 **Docker** 頁面，點選 NegaDownloader 圖示並選擇 **Edit**。
2. 將 **Repository** 欄位改成其中一個映像名稱，例如：

   ```text
   ghcr.io/shay-ai-om/negadownloader:api
   ```

   如果要用 Docker Hub，填入你的 namespace：

   ```text
   your-namespace/negadownloader:api
   ```

3. 保留原本的環境變數，尤其是 `NEGADOWNLOADER_PASSWORD`、`PUID` 和 `PGID`。
4. 確認兩個容器路徑與原有主機路徑都維持不變：

   | 容器路徑 | 用途 |
   | --- | --- |
   | `/app/data` | 資料庫、設定、Cookies 及登入瀏覽器資料 |
   | `/app/download` | 下載的媒體檔案 |

5. 按 **Apply**。Unraid 會拉取 `api` 映像並重建容器。完成後回到 Docker 頁面確認容器啟動，再開啟 NegaDownloader 檢查工作紀錄與設定是否仍在。

保留 `/app/data` 與 `/app/download` 的相同主機掛載路徑，容器重建後就會繼續使用既有資料。更新前可先備份 appdata。

## 之後更新到最新的 `api`

每次 `api` 分支有新提交，Actions 會更新同一個 `:api` 標籤。等映像發布成功後，在 Unraid 的 Docker 頁面選擇 **Check for Updates**，再套用 NegaDownloader 更新。如果 Unraid 尚未顯示更新，進入容器 **Edit**，重新套用相同的 `:api` Repository 以拉取目前映像。

## 回復版本

在容器 **Edit** 頁面把 Repository 改回原本使用的標籤，例如：

```text
ghcr.io/shay-ai-om/negadownloader:latest
```

`latest` 對應 `main` 分支，不一定等於切換前安裝的映像。若需要回到特定版本，使用該次 GitHub Actions 建置發布的 `sha-...` 標籤或版本標籤（例如 `v1.2.3`）。回復時仍保留原本的兩個資料夾掛載。

