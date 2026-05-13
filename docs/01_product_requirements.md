# 01 — Product Requirements

## Product Name

CompetitionOps Agent

## One-liner

給新創團隊使用的 Google Workspace-native 競賽 PM 自動化 agent：讀簡章、拆任務、寫 Docs、更新 Sheets、排 Calendar、同步 Kanban。

## Target Users

- 新創團隊 founder / PM
- 學生競賽團隊
- 黑客松團隊
- 接近 PMO 需求的小型研發團隊

## Pain Points

1. 每場比賽規則不同，PM 要反覆讀簡章。
2. Deadline、頁數限制、影片規格、匿名規則容易漏。
3. 任務拆分與人員分派仰賴 PM 經驗。
4. Google Drive、Docs、Sheets、Calendar、Kanban 分散。
5. 團隊不想用 Notion。
6. AI 若直接寫入外部工具，可能造成誤刪、誤改、誤邀請。

## Goals

- 10 分鐘內從比賽簡章產出第一版作戰計畫。
- 自動建立文件、任務與時程，但所有高風險動作需要 PM approval。
- 保持本地端可控，資料優先留在使用者 Google account 與自架平台。
- 支援 Claude Code 進行長期迭代開發。

## Non-goals

- 不做 Notion clone。
- 不做完整 CRM。
- 不做自動正式提交比賽。
- 不在 MVP 階段做 domain-wide delegation。
- 不讓 AI 自動刪除 Drive 檔案或對外分享敏感資料。

## Personas

### PM / Founder

需要快速判斷比賽是否值得投、要交什麼、誰負責、何時完成。

### Tech Lead

需要知道技術文件、demo、prototype、architecture diagram 的交付要求。

### Business / Design

需要知道 pitch deck、market sizing、commercial feasibility、video script 的交付要求。

## MVP Success Metrics

- 解析一份比賽簡章後，能輸出至少：
  - 1 筆 CompetitionBrief
  - 5 個以上 TaskDraft
  - 3 個以上 CalendarEventDraft
  - 1 份 Google Docs outline
  - 1 張 Google Sheets tracking row
- 90% write actions 在 dry-run 階段可被 PM 理解與修改。
- 所有測試通過。
