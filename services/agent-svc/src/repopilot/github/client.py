"""The GitHub REST calls RepoPilot needs. Used on the host only: the token never reaches a
sandbox, and every write is a draft pull request built from the approved change."""

import base64

import httpx
from pydantic import BaseModel


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str, *, rate_limited: bool = False):
        super().__init__(f"GitHub {status}: {message}")
        self.status, self.rate_limited = status, rate_limited


def _rate_limited(resp: httpx.Response, message: str) -> bool:
    """GitHub answers a rate limit with 429, or with 403 like a missing permission; only the
    headers and message tell them apart."""
    if resp.status_code == 429:
        return True
    return resp.status_code == 403 and (
        resp.headers.get("x-ratelimit-remaining") == "0"
        or "retry-after" in resp.headers
        or "rate limit" in message.lower()
    )


class Issue(BaseModel):
    number: int
    title: str
    body: str
    html_url: str
    state: str


class GitHubClient:
    def __init__(
        self,
        token: str,
        api_url: str = "https://api.github.com",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=api_url,
            transport=transport,
            timeout=60.0,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "repopilot",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _req(self, method: str, path: str, **kwargs) -> httpx.Response:
        resp = await self._http.request(method, path, **kwargs)
        if resp.status_code >= 400:
            try:
                message = str(resp.json().get("message", resp.text))
            except (ValueError, AttributeError):  # not JSON, or not an object
                message = resp.text
            raise GitHubError(
                resp.status_code, message[:500], rate_limited=_rate_limited(resp, message)
            )
        return resp

    async def get_issue(self, repo: str, number: int) -> Issue:
        data = (await self._req("GET", f"/repos/{repo}/issues/{number}")).json()
        return Issue(
            number=data["number"],
            title=data["title"],
            body=data.get("body") or "",
            html_url=data["html_url"],
            state=data["state"],
        )

    async def default_branch(self, repo: str) -> tuple[str, str]:
        """(branch name, head commit sha) of the default branch."""
        branch = (await self._req("GET", f"/repos/{repo}")).json()["default_branch"]
        ref = (await self._req("GET", f"/repos/{repo}/git/ref/heads/{branch}")).json()
        return branch, ref["object"]["sha"]

    async def tarball(self, repo: str, ref: str) -> bytes:
        return (await self._req("GET", f"/repos/{repo}/tarball/{ref}")).content

    async def commit_tree(self, repo: str, commit_sha: str) -> str:
        return (await self._req("GET", f"/repos/{repo}/git/commits/{commit_sha}")).json()["tree"][
            "sha"
        ]

    async def create_blob(self, repo: str, content: bytes) -> str:
        body = {"content": base64.b64encode(content).decode(), "encoding": "base64"}
        return (await self._req("POST", f"/repos/{repo}/git/blobs", json=body)).json()["sha"]

    async def create_tree(self, repo: str, base_tree: str, entries: list[dict]) -> str:
        body = {"base_tree": base_tree, "tree": entries}
        return (await self._req("POST", f"/repos/{repo}/git/trees", json=body)).json()["sha"]

    async def create_commit(self, repo: str, message: str, tree: str, parents: list[str]) -> str:
        body = {"message": message, "tree": tree, "parents": parents}
        return (await self._req("POST", f"/repos/{repo}/git/commits", json=body)).json()["sha"]

    async def create_ref(self, repo: str, ref: str, sha: str) -> None:
        await self._req("POST", f"/repos/{repo}/git/refs", json={"ref": ref, "sha": sha})

    async def update_ref(self, repo: str, ref: str, sha: str) -> None:
        """Force-move `ref` (e.g. "heads/repopilot/abc") to `sha`."""
        await self._req("PATCH", f"/repos/{repo}/git/refs/{ref}", json={"sha": sha, "force": True})

    async def find_pull(self, repo: str, branch: str) -> str | None:
        """The open pull request from `branch` of the same repository, if any."""
        owner = repo.split("/", 1)[0]
        pulls = (
            await self._req(
                "GET", f"/repos/{repo}/pulls", params={"head": f"{owner}:{branch}", "state": "open"}
            )
        ).json()
        return pulls[0]["html_url"] if pulls else None

    async def create_pull(self, repo: str, *, title: str, head: str, base: str, body: str) -> str:
        data = {"title": title, "head": head, "base": base, "body": body, "draft": True}
        return (await self._req("POST", f"/repos/{repo}/pulls", json=data)).json()["html_url"]
