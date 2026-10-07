# 市售食品查詢系統

目前版本：V2.6.0｜發布標籤：`V2.6.0`

以原生 HTML、CSS 與 JavaScript 建立的食品查詢系統。商品資料由 Product Repository 載入與驗證，再交由搜尋及畫面顯示使用。

## V2.6.0

- 新增 TFDA 完整商品候選轉換器，從 50,650 筆追溯追蹤資料中產生 1,894 筆同時具有完整 8 項營養與成分的候選商品。
- 1,894 筆候選的 `candidateId` 與 `traceabilityCode` 一對一保留；不同 `traceabilityCode` 始終維持獨立紀錄。
- `barcode` 與 `brand` 維持 `null`，不由公司名稱、追溯串接碼或其他欄位自行推測。
- `ingredientsRaw` 完整保存政府來源原文，解析後的成分維持原始排列順序。
- 成分 parser 支援 `()`、`（）`、`[]`、`〔〕`、`﹝﹞`，並安全忽略連續 `、、`、`、、、` 及末尾 `、` 所產生的空項目。
- 成分解析失敗由 144 筆降至 46 筆；45 筆真正括號錯誤與 1 筆開頭分隔符維持待人工確認，不自動修正原文或推測成分邊界。
- 29 組具有相同公司、商品名稱與規格的候選不自動合併，避免不同追溯串接碼及來源欄位差異遭到覆蓋。
- 候選資料以 gzip JSON 保存於 `data/candidates/tfda_candidates.json.gz`。

### V2.6.0 資料工具

| 檔案 | 職責 |
|---|---|
| `module/build_tfda_candidates.py` | 將完整營養與成分的 TFDA 紀錄轉為候選商品，安全解析成分並統計資料異常 |
| `data/candidates/tfda_candidates.json.gz` | 1,894 筆 TFDA 完整商品候選 |

## V2.5.0

- 新增 TFDA 食品追溯追蹤資料蒐集器，官方來源為政府資料開放平臺的「食品追溯追蹤系統消費者查詢資料集」。
- 完整蒐集並以 gzip 保存 50,650 筆商品原始紀錄，不自行補正來源內容。
- 其中 10,752 筆具有完整 8 項每份營養，2,030 筆具有內容物標示，1,894 筆同時具有完整營養與內容物標示。
- 建立 TFDA 商品完整度分類器，分為 `complete_nutrition`、`has_ingredients`、`complete_nutrition_and_ingredients` 與 `incomplete`。
- `traceabilityCode` 與 `barcode` 明確分離；食品追溯追蹤串接碼不會被視為或轉換成商品條碼。
- anomaly 保留原始資料並只作標記，不自行修正。共有 14 筆異常紀錄、77 個 anomaly 項目；原始與 processed 資料逐筆比對後確認沒有遺失。
- 原始資料 `data/raw/tfda_traceability.json.gz` 與篩選結果 `data/processed/tfda_products_usable.json.gz` 均使用 gzip 壓縮保存。

### V2.5.0 資料工具

| 檔案 | 職責 |
|---|---|
| `module/collect_tfda_traceability.py` | 下載官方 ZIP、保留原始欄位、解析可用營養欄位並原子寫入 gzip |
| `module/filter_tfda_products.py` | 依營養與內容物完整度分類，保留串接碼與 anomaly |
| `data/raw/tfda_traceability.json.gz` | TFDA 原始與結構化商品紀錄，共 50,650 筆 |
| `data/processed/tfda_products_usable.json.gz` | TFDA 商品完整度分類結果，共 50,650 筆 |

## V1.2.0

發布標籤：`V1.2.0`

## 已完成功能

- 食品名稱、品牌與條碼的部分文字搜尋。
- 單筆符合時直接顯示商品詳情；多筆符合時先顯示清單，點選後查看詳情。
- 商品基本資訊：名稱、品牌、條碼與每份規格。
- 八項每份營養成分：熱量、蛋白質、脂肪、飽和脂肪、反式脂肪、碳水化合物、糖與鈉。
- 每日參考值百分比與進度條；百分比最多顯示一位小數，整數不顯示小數。糖與反式脂肪僅顯示含量。超過 100% 時保留實際百分比，進度條最多填滿。
- JSON Schema 定義商品資料格式、數值型別與日期格式。
- 商品資料驗證：id、name 為必要欄位；排除無效商品，重複 id 保留第一筆，無效營養數值不進入計算。驗證問題以 `console.warn` 記錄。
- Product Repository 資料存取層：載入並保存有效商品、搜尋與依 id 取得商品。
- 多資料來源架構：`sources` 保存來源，`nutritionSource` 指定營養資料引用的來源。
- 顯示可開啟新分頁的來源連結、最後更新日期與距資料確認經過天數。超過 180 天顯示時效提醒，日期缺失或無效時顯示「資料更新時間未知」。
- 初始、載入中、單筆成功、多筆結果、查無商品、載入失敗與無可用資料狀態。
- RWD 響應式版面，支援桌面與行動裝置。
- 營養累加清單：加入商品、重複加入增加數量、增減數量（UI 最低為 1）、移除與清空。localStorage 只保存商品 id 與數量，重新整理或重新開啟頁面後仍保留。
- 營養累加總計：透過 Repository 取得每份營養，乘上清單數量後加總八項營養，清單操作後立即更新。空清單隱藏總計；缺資料的營養項目顯示「目前尚無資料」。
- 累加總計的六項每日參考值狀態：低於 80% 正常顯示、80% 至未滿 100% 顯示「接近每日參考值」、100% 以上顯示「已達或超過每日參考值」。使用未四捨五入的計算百分比判斷，不作健康判斷；糖與反式脂肪不計算狀態。

## 示範商品

目前資料只有「統一布丁」，商品 id 為 `uni-pudding-100g`，每份 100 g，條碼為 `4710088430922`。

來源為全聯小時達商品資訊，資料確認日期為 `2026-09-16`。`ingredients` 為空陣列，畫面不顯示食品成分、添加物或過敏原。

可輸入「統一」、「布丁」、「統一布丁」或條碼查詢。

## 本機執行

不需建置或安裝前端套件。使用 Python 3，在專案目錄啟動靜態 HTTP 伺服器：

```sh
python -m http.server 8000 --bind 127.0.0.1
```

瀏覽器開啟 `http://127.0.0.1:8000/`。請透過 HTTP 伺服器執行，不要直接以 `file://` 開啟 HTML，以免商品 JSON 無法載入。

## 程式與資料結構

| 檔案 | 職責 |
|---|---|
| `index.html`、`styles.css` | 首頁與響應式樣式 |
| `app.js`、`results-view.js` | 頁面流程、狀態與結果清單 |
| `product-repository.js` | 商品載入、驗證、保存與存取介面 |
| `product-search.js` | 部分文字搜尋 |
| `product-validation.js` | 商品與來源資料驗證 |
| `product-view.js`、`nutrition-view.js` | 商品詳情與營養顯示 |
| `nutrition-config.js` | 集中管理每日參考值 |
| `product-sources.js` | 依來源 id 解析商品來源 |
| `data-freshness.js`、`data-freshness-config.js` | 日期解析、天數計算與時效門檻 |
| `daily-intake-store.js`、`daily-intake-view.js` | 營養累加清單的 localStorage 資料與操作畫面 |
| `daily-nutrition.js`、`daily-nutrition-view.js` | 營養累加計算與顯示 |
| `daily-reference-status.js` | 集中管理累加總計狀態門檻與中性提示 |
| `data/products.json` | 商品資料 |
| `data/product-schema.json` | JSON Schema（Draft 2020-12） |

Schema 定義文件格式；瀏覽器載入資料時執行的是 `product-validation.js` 的驗證函式，並非 JSON Schema 引擎。

## 測試狀態

目前專案未保存自動化測試套件或測試指令。V1.2.0 發布檢查以 JSON Schema 驗證與臨時瀏覽器回歸測試進行；臨時測試頁不納入發布。

## 資料說明

更新日期及時效提示不代表資料一定正確或為最新版本，請以商品最新包裝標示為準。每日參考值為本專案的集中設定，百分比用於顯示每份營養或累加清單總量的占比；狀態提示不是健康判斷。
