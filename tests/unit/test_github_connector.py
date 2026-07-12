import asyncio

import httpx

from app.domains.connector.github.github import GitHubConnector


def test_github_repo_metadata_uses_repo_api():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/octo/demo"
        return httpx.Response(
            200,
            json={
                "id": 1,
                "full_name": "octo/demo",
                "private": False,
                "default_branch": "main",
                "html_url": "https://github.com/octo/demo",
                "description": "demo repo",
                "stargazers_count": 2,
                "forks_count": 3,
                "open_issues_count": 4,
            },
        )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://api.github.com",
        ) as client:
            return await GitHubConnector(client).execute(
                "repo_metadata",
                {
                    "run_id": "run_1",
                    "action_id": "act_1",
                    "owner": "octo",
                    "repo": "demo",
                },
            )

    result = asyncio.run(run())

    assert result.status == "SUCCESS"
    assert result.data["full_name"] == "octo/demo"
    assert result.data["default_branch"] == "main"


def test_github_repo_metadata_maps_not_found():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404)),
            base_url="https://api.github.com",
        ) as client:
            return await GitHubConnector(client).execute(
                "repo_metadata",
                {
                    "run_id": "run_1",
                    "action_id": "act_1",
                    "owner": "octo",
                    "repo": "missing",
                },
            )

    result = asyncio.run(run())

    assert result.status == "FAILED"
    assert result.error["code"] == "NOT_FOUND"
