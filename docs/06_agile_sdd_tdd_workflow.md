# 06 — Agile + SDD + TDD Workflow

## Agile Cadence

### Sprint Length

1 week for MVP.

### Ceremonies

- Sprint Planning: select P0/P1 backlog items.
- Daily Async Check-in: status, blocker, next action.
- Review: demo working workflow.
- Retro: what failed in spec / tests / integration.

## SDD — Specification-driven Development

所有實作先從 spec 開始。

```text
User problem
  → Product requirement
  → Spec / acceptance criteria
  → API contract
  → Test
  → Implementation
  → Review
```

## TDD

每個功能都要先寫測試。

Example cycle:

```bash
# failing test
uv run pytest tests/test_action_plan.py -q

# implement
$EDITOR src/competitionops/...

# green
uv run pytest

# quality
uv run ruff check .
uv run mypy src
```

## Backlog Prioritization

- P0: 沒有它不能 demo。
- P1: demo 後馬上有價值。
- P2: 產品化需要，但 MVP 可先略過。

## Definition of Ready

- 有清楚 user story。
- 有 acceptance criteria。
- 有安全分類。
- 有資料 schema。
- 有測試方向。

## Definition of Done

- 測試通過。
- 型別檢查通過。
- lint 通過。
- 文件更新。
- dry-run / approval / audit 補齊。
- 無 secrets。
