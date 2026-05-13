# 00 — Session Recap

日期基準：2026-05-13。

## 使用者原始需求

新創團隊經常參加新的比賽。每個比賽會有：
- 截止日期
- 應繳資料
- 比賽簡章
- 團隊成員分工
- 產品與能力分支
- Kanban board
- 團隊行事曆

使用者希望 AI 可以透過 PM 的自然語言指令完成：
- 比賽簡章解析
- 任務拆分
- 任務分派
- 團隊成員工作分配
- 上團隊行事曆
- 串接 Google Drive / Docs / Sheets / Calendar
- 本地端搭建平台
- 不使用 Notion

## 先前討論結論

### 1. 開源工具現況

截至 2026-05-13，尚未看到成熟開源產品完整包辦：

```text
比賽簡章 → 自動拆任務 → 自動派工 → Kanban → 團隊行事曆 → 持續追蹤
```

但已有足夠成熟的零件可以組出 80–90% 自動化：

- Plane：Kanban / issue / project management
- OpenProject：正式 PMO / Gantt / milestone
- Windmill：code-first workflow automation
- Activepieces：low-code / no-code automation
- Docling / MarkItDown：PDF / Office / Markdown 文件解析
- Crawl4AI / Playwright MCP：競賽網站擷取
- Claude Code / MCP：coding agent 與外部工具整合
- Google Workspace APIs / MCP：Drive / Calendar / Gmail / Chat / People
- Custom Google API adapter：Docs / Sheets / Drive 檔案移動等高階操作

### 2. Google 深度整合

可行。建議使用：

```text
Google Drive    = 檔案庫
Google Docs     = 提案文件、討論事項、會議紀錄
Google Sheets   = 比賽追蹤表、任務矩陣、RACI、team capacity
Google Calendar = deadline、checkpoint、mock pitch、submission reminder
Plane           = Kanban / task execution
Windmill        = workflow automation
Claude Code     = 開發與 MCP glue code
```

### 3. Google 個人帳號 + AI Pro

可行做 MVP，但要分清楚：

- Google AI Pro 是使用者端 Gemini / storage / Workspace AI 功能。
- 真正讓本地平台寫入 Drive / Docs / Sheets / Calendar 的是 Google OAuth + Google APIs / MCP。
- 個人帳號沒有 Workspace Admin Console，不能用 domain-wide delegation 代表所有團隊成員。
- 多人協作時，每個使用者需自行 OAuth 授權，或未來升級 Google Workspace domain。

### 4. MVP 功能

第一版只做：

1. 從 Google Drive 讀取比賽簡章。
2. 把簡章轉成 structured JSON。
3. 寫入 Google Sheets 比賽追蹤表。
4. 產生 Google Docs 提案大綱。
5. 建立 Plane Kanban tasks。
6. 建立 Google Calendar deadline / checkpoint。

暫不做：
- 自動寄信
- 自動正式提交
- 自動刪檔
- 自動改所有人主行事曆
- 自動對外分享文件

### 5. 核心安全原則

- AI 先產生 plan。
- PM review。
- PM approve 後才執行 write actions。
- 所有外部寫入皆要 audit log。
- MCP tools 僅暴露高階 business action，不暴露危險低階 API。
