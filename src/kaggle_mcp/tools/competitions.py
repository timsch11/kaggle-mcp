"""Competition tools: discover, read rules/data, download, leaderboard, submit."""

from __future__ import annotations

import time

from kaggle_mcp.core import App, check_slug, enum_value, pick
from kaggle_mcp.tools.files import finalize_download, read_upload
from kaggle_mcp.workspace import safe_filename

SVC = "competitions.CompetitionApiService"

SORT = {k: f"COMPETITION_SORT_BY_{k.upper()}" for k in [
    "grouped", "best", "prize", "earliest_deadline", "latest_deadline", "number_of_teams", "relevance", "recently_created",
]}
CATEGORY = {k: f"HOST_SEGMENT_{k.upper()}" for k in [
    "featured", "getting_started", "masters", "playground", "recruitment", "research", "community", "analytics",
]}
GROUP = {k: f"COMPETITION_LIST_TAB_{k.upper()}" for k in ["general", "entered", "community", "hosted", "everything"]}

COMP_KEYS = [
    "ref", "title", "url", "description", "category", "reward", "deadline", "teamCount", "userHasEntered",
    "userRank", "maxDailySubmissions", "maxTeamSize", "evaluationMetric", "isKernelsSubmissionsOnly",
    "submissionsDisabled", "mergerDeadline", "newEntrantDeadline", "organizationName", "awardsPoints",
]


def register(app: App) -> None:
    c = app.client

    @app.tool(title="List / search competitions")
    async def competitions_list(
        search: str | None = None,
        category: str | None = None,
        group: str | None = None,
        sort_by: str | None = None,
        page: int = 1,
    ) -> dict:
        """Search competitions.
        category: featured, getting_started, masters, playground, recruitment, research, community, analytics.
        group: general, entered (ones you joined), community, hosted, everything.
        sort_by: grouped, best, prize, earliest_deadline, latest_deadline, number_of_teams, relevance, recently_created."""
        body = {
            "search": search,
            "category": enum_value(category, CATEGORY, "category"),
            "group": enum_value(group, GROUP, "group"),
            "sortBy": enum_value(sort_by, SORT, "sort_by"),
            "page": max(1, page),
        }
        resp = await c.call(SVC, "ListCompetitions", body)
        keys = ["ref", "title", "category", "reward", "deadline", "teamCount", "userHasEntered", "isKernelsSubmissionsOnly"]
        return {"competitions": [pick(x, keys) for x in resp.get("competitions", [])], "page": page}

    @app.tool(title="Competition details")
    async def competition_get(competition: str) -> dict:
        """Details of a competition (slug like 'titanic' or a kaggle.com/competitions URL):
        deadline, metric, submission limits, whether it is code-only, etc."""
        slug = check_slug(competition, "competition")
        return pick(await c.call(SVC, "GetCompetition", {"competitionName": slug}), COMP_KEYS)

    @app.tool(title="Competition pages (overview, rules, data)")
    async def competition_pages(competition: str, page_name: str | None = None, max_chars: int | None = None) -> dict:
        """Read competition description pages (overview, evaluation, rules, data description...).
        Without page_name returns page names and a short preview of each."""
        slug = check_slug(competition, "competition")
        resp = await c.call(SVC, "ListCompetitionPages", {"competitionName": slug, "pageName": page_name})
        pages = resp.get("pages", [])
        if page_name:
            return {"pages": [{"name": p.get("name"), "content": app.truncate(p.get("content") or "", max_chars)} for p in pages]}
        return {"pages": [{"name": p.get("name"), "preview": (p.get("content") or "")[:300]} for p in pages]}

    @app.tool(title="List competition data files")
    async def competition_files(competition: str, page_token: str | None = None) -> dict:
        """List the data files of a competition (name, size)."""
        slug = check_slug(competition, "competition")
        resp = await c.call(SVC, "ListDataFiles", {"competitionName": slug, "pageToken": page_token, "pageSize": 200})
        files = [pick(f, ["name", "totalBytes", "creationDate", "description"]) for f in resp.get("files", [])]
        return {"files": files, "nextPageToken": resp.get("nextPageToken") or None}

    @app.tool(title="Download competition data", local_write=True)
    async def competition_download(competition: str, file_name: str | None = None, dest_dir: str | None = None) -> dict:
        """Download one competition file (`file_name`) or all data as a zip into the workspace.
        You must have joined the competition and accepted its rules on kaggle.com.
        Use workspace_unzip to extract archives."""
        slug = check_slug(competition, "competition")
        target = app.workspace.resolve(dest_dir or f"competitions/{slug}")
        if file_name:
            dest = target / safe_filename(file_name)
            n = await c.download(SVC, "DownloadDataFile", {"competitionName": slug, "fileName": file_name}, dest, app.settings.max_download_bytes)
        else:
            dest = target / f"{slug}.zip"
            n = await c.download(SVC, "DownloadDataFiles", {"competitionName": slug}, dest, app.settings.max_download_bytes)
        return finalize_download(app, dest, n)

    @app.tool(title="Competition leaderboard")
    async def competition_leaderboard(competition: str, page_size: int = 50, page_token: str | None = None) -> dict:
        """Top of the public leaderboard (team, score, date)."""
        slug = check_slug(competition, "competition")
        resp = await c.call(SVC, "GetLeaderboard", {"competitionName": slug, "pageSize": max(1, min(page_size, 200)), "pageToken": page_token})
        rows = [pick(s, ["teamId", "teamName", "score", "submissionDate"]) for s in resp.get("submissions", [])]
        return {"leaderboard": rows, "nextPageToken": resp.get("nextPageToken") or None}

    @app.tool(title="My competition submissions")
    async def competition_submissions(competition: str, page_token: str | None = None) -> dict:
        """Your submissions to a competition with status and public/private scores."""
        slug = check_slug(competition, "competition")
        resp = await c.call(SVC, "ListSubmissions", {"competitionName": slug, "pageToken": page_token, "pageSize": 50})
        keys = ["ref", "date", "description", "fileName", "status", "publicScore", "privateScore", "errorDescription"]
        return {"submissions": [pick(s, keys) for s in resp.get("submissions", [])], "nextPageToken": resp.get("nextPageToken") or None}

    @app.tool(title="Submit a file to a competition", writes=True)
    async def competition_submit(
        competition: str,
        message: str,
        file_path: str | None = None,
        content: str | None = None,
        file_name: str = "submission.csv",
    ) -> dict:
        """Submit predictions to a (non code-only) competition. Uses one of your daily submissions.
        Provide either `file_path` (relative to the workspace) or inline text `content`
        (e.g. CSV) with `file_name`. Check results with competition_submissions."""
        slug = check_slug(competition, "competition")
        data, name = read_upload(app, file_path, content, file_name)
        start = await c.call(SVC, "StartSubmissionUpload", {
            "competitionName": slug,
            "fileName": name,
            "contentLength": data.stat().st_size if not isinstance(data, bytes) else len(data),
            "lastModifiedEpochSeconds": int(time.time()),
        })
        if not start.get("createUrl") or not start.get("token"):
            raise ValueError("Kaggle did not return an upload URL")
        await c.upload_to_url(start["createUrl"], data)
        resp = await c.call(SVC, "CreateSubmission", {
            "competitionName": slug, "blobFileTokens": start["token"], "submissionDescription": message[:500],
        })
        return pick(resp, ["message", "ref"])

    @app.tool(title="Submit a notebook to a code competition", writes=True)
    async def competition_submit_notebook(
        competition: str,
        notebook_ref: str,
        message: str,
        version: int | None = None,
        file_name: str = "submission.csv",
    ) -> dict:
        """Submit a notebook's output to a code competition. The notebook must have a
        successful run that writes `file_name`. `version` defaults to the latest."""
        slug = check_slug(competition, "competition")
        owner, kslug = await app.own_ref(notebook_ref, "notebook_ref")
        resp = await c.call(SVC, "CreateCodeSubmission", {
            "competitionName": slug, "kernelOwner": owner, "kernelSlug": kslug, "kernelVersion": version,
            "fileName": file_name, "submissionDescription": message[:500],
        })
        return pick(resp, ["message", "ref"])
