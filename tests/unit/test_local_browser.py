from types import SimpleNamespace

import pytest

from app.core.schemas import ActionRequest, Decision, DecisionResponse
from app.runtime.config import LocalConfig, LocalPaths
from app.runtime.local import LocalRuntime


@pytest.mark.asyncio
async def test_local_browser_journals_each_action_without_caching_repeated_clicks(
    tmp_path, monkeypatch
):
    pytest.importorskip("playwright")
    from app.domains.agent.services import browser_prototype_agent as browser

    paths = LocalPaths(tmp_path / "config" / "config.toml", tmp_path / "data")
    config = LocalConfig(workspace=str(tmp_path), credential_store="env", browser_enabled=True)
    model = browser.BrowserPageModel(
        snapshot=[{"element_id": "1", "role": "button", "label": "Next"}],
        locator_candidates=[],
        selector_map={"1": {}},
    )

    async def build_model(page):
        return model

    async def settle(page, **kwargs):
        pass

    monkeypatch.setattr(browser, "_build_page_model", build_model)
    monkeypatch.setattr(browser, "_settle_page", settle)
    calls = []
    with LocalRuntime(config, paths) as runtime:

        async def execute(page, selectors, action):
            calls.append(action)
            assert (
                runtime.database.db.execute(
                    "SELECT COUNT(*) FROM intents WHERE state='started'"
                ).fetchone()[0]
                == 1
            )

        monkeypatch.setattr(browser, "execute_action", execute)
        plan = []
        for index in range(2):
            request = ActionRequest(
                run_id="run_browser",
                action_id=f"act_{index}",
                source="cli",
                domain="browser",
                action_type="BROWSER_CLICK",
                target_system="browser",
                target="https://example.test",
                payload={"label": "Next", "role": "button"},
            )
            decision = DecisionResponse(
                run_id=request.run_id, action_id=request.action_id, decision=Decision.ALLOW
            )
            plan.append(({"type": "click", "label": "Next", "role": "button"}, request, decision))
        events = await browser._execute_atomic_on_page(
            page=SimpleNamespace(url="https://example.test"),
            url="https://example.test",
            step_plan=plan,
            settle_ms=0,
            timeout_ms=1000,
            multi_action=True,
            events=[],
        )
        assert len(events) == len(calls) == 2
        assert (
            runtime.database.db.execute(
                "SELECT COUNT(*) FROM intents WHERE state='completed'"
            ).fetchone()[0]
            == 2
        )


def test_local_screenshot_ignores_model_selected_destination(tmp_path):
    pytest.importorskip("playwright")
    from app.domains.agent.services import browser_prototype_agent as browser

    paths = LocalPaths(tmp_path / "config" / "config.toml", tmp_path / "data")
    config = LocalConfig(workspace=str(tmp_path), credential_store="env", browser_enabled=True)
    with LocalRuntime(config, paths):
        action = browser._prepare_action(
            action={"type": "screenshot", "path": "/untrusted/location.png"},
            page_model=browser.BrowserPageModel([], [], {}),
            action_id="act_screenshot",
        )
        assert action["path"] == str(
            paths.data / "artifacts" / "browser" / "screenshots" / "act_screenshot.png"
        )
