import pytest
from competitionops.adapters.fake import FakeExternalActionExecutor
from competitionops.schemas import ExternalAction


@pytest.mark.asyncio
async def test_fake_adapter_is_dry_run_by_default() -> None:
    action = ExternalAction(
        action_id="a1",
        type="google.docs.create",
        target_system="google_docs",
        payload={"title": "Demo"},
    )
    result = await FakeExternalActionExecutor().execute(action, dry_run=True)
    assert result.status == "dry_run"
    assert result.external_id == "fake_a1"
