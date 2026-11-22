"""Cross-cutting tools: account check, universal search, ChatGPT-compatible
``search`` / ``fetch``, and models."""

from __future__ import annotations

import json

from kaggle_mcp.client import KaggleAPIError
from kaggle_mcp.core import App, check_slug, enum_value, pick, split_ref
from kaggle_mcp.notebooks import parse_notebook, render_notebook

SEARCH = "search.SearchApiService"
MODELS = "models.ModelApiService"

DOC_TYPES = {"competition": "COMPETITION", "dataset": "DATASET", "notebook": "KERNEL", "model": "MODEL", "discussion": "TOPIC", "user": "USER"}
MODEL_SORT = {k: f"LIST_MODELS_ORDER_BY_{k.upper()}" for k in [
    "hotness", "download_count", "vote_count", "notebook_count", "publish_time", "create_time", "update_time",
]}
_PREFIX = {"COMPETITION": "competition", "DATASET": "dataset", "KERNEL": "notebook", "MODEL": "model"}
_URL = {"competition": "competitions", "dataset": "datasets", "notebook": "code", "model": "models"}


def _doc_ref(doc: dict) -> tuple[str, str] | None:
    """Return (kind, ref) for an entity search document, or None if unsupported."""
    kind = _PREFIX.get(doc.get("documentType", ""))
    slug = doc.get("slug")
    if not kind or not slug:
        return None
    if kind == "competition":
        return kind, slug
    if "/" in slug:
        return kind, slug
    owner = (doc.get("ownerUser") or {}).get("userName") or (doc.get("ownerOrganization") or {}).get("slug")
    return (kind, f"{owner}/{slug}") if owner else None


def _url(kind: str, ref: str) -> str:
    return f"https://www.kaggle.com/{_URL[kind]}/{ref}"


def register(app: App) -> None:
    c = app.client

    async def _entities(query: str, types: list[str] | None, page_size: int, page_token: str | None = None) -> dict:
        filters = {"query": query}
        if types:
            filters["documentTypes"] = [enum_value(t, DOC_TYPES, "type") for t in types]
        return await c.call(SEARCH, "ListEntities", {
            "filters": filters, "pageSize": max(1, min(page_size, 50)), "pageToken": page_token,
        })

    @app.tool(title="Check Kaggle connection")
    async def kaggle_auth_status() -> dict:
        """Verify the server's Kaggle API credentials work and show server policy
        (read-only mode, deletion, workspace), plus the account's username.
        Never reveals the credentials."""
        source = c.credential_source
        await c.call("kernels.KernelsApiService", "GetAcceleratorQuotaStatistics", {})
        return {
            "authenticated": True,
            "username": await c.username(),
            "credential_source": source,
            "read_only": app.settings.read_only,
            "delete_enabled": app.settings.allow_delete,
            "workspace": str(app.workspace.root),
        }

    @app.tool(title="Search all of Kaggle")
    async def search_kaggle(query: str, types: list[str] | None = None, page_size: int = 20, page_token: str | None = None) -> dict:
        """Full-text search across Kaggle. `types` filters to any of:
        competition, dataset, notebook, model, discussion, user."""
        resp = await _entities(query, types, page_size, page_token)
        items = []
        for d in resp.get("documents", []):
            item = pick(d, ["documentType", "title", "slug", "votes", "updateTime"])
            r = _doc_ref(d)
            if r:
                item["ref"], item["url"] = r[1], _url(*r)
            items.append(item)
        return {"results": items, "total": resp.get("totalDocuments"), "nextPageToken": resp.get("nextPageToken") or None}

    # --- ChatGPT connector / deep-research compatible pair -----------------
    @app.tool(title="Search")
    async def search(query: str) -> dict:
        """Search Kaggle competitions, datasets, notebooks and models. Returns ids usable with `fetch`."""
        resp = await _entities(query, ["competition", "dataset", "notebook", "model"], 20)
        results = []
        for d in resp.get("documents", []):
            r = _doc_ref(d)
            if r:
                results.append({"id": f"{r[0]}:{r[1]}", "title": d.get("title") or r[1], "url": _url(*r)})
        return {"results": results}

    @app.tool(title="Fetch")
    async def fetch(id: str) -> dict:
        """Fetch the full content of a search result by id (e.g. 'notebook:owner/slug',
        'dataset:owner/slug', 'competition:titanic', 'model:owner/slug')."""
        kind, _, ref = id.partition(":")
        kind = kind.strip().lower()
        if kind not in _URL:
            raise ValueError("id must start with competition:, dataset:, notebook: or model:")
        meta: dict
        if kind == "competition":
            ref = check_slug(ref, "competition")
            meta = await c.call("competitions.CompetitionApiService", "GetCompetition", {"competitionName": ref})
            title = meta.get("title", ref)
            text = json.dumps(pick(meta, ["title", "description", "category", "reward", "deadline", "evaluationMetric", "teamCount", "maxDailySubmissions", "isKernelsSubmissionsOnly"]), indent=1)
            try:
                pages = await c.call("competitions.CompetitionApiService", "ListCompetitionPages", {"competitionName": ref})
                text += "\n\n" + "\n\n".join(f"## {p.get('name')}\n{p.get('content', '')}" for p in pages.get("pages", []))
            except KaggleAPIError:
                pass  # pages are optional extra context
        elif kind == "dataset":
            owner, slug = split_ref(ref, "dataset")
            ref = f"{owner}/{slug}"
            meta = await c.call("datasets.DatasetApiService", "GetDataset", {"ownerSlug": owner, "datasetSlug": slug})
            title = meta.get("title", ref)
            text = f"# {title}\n{meta.get('subtitle', '')}\n\n{meta.get('description', '')}"
            files = meta.get("files") or []
            if files:
                text += "\n\n## Files\n" + "\n".join(f"- {f.get('name')} ({f.get('totalBytes', '?')} bytes)" for f in files[:200])
        elif kind == "notebook":
            owner, slug = split_ref(ref)
            ref = f"{owner}/{slug}"
            resp = await c.call("kernels.KernelsApiService", "GetKernel", {"userName": owner, "kernelSlug": slug})
            meta = resp.get("metadata") or {}
            blob = resp.get("blob") or {}
            title = meta.get("title", ref)
            text = blob.get("source") or ""
            if (blob.get("kernelType") or meta.get("kernelType")) == "notebook":
                try:
                    text = render_notebook(parse_notebook(text), (blob.get("language") or "python").lower())
                except ValueError:
                    pass
        else:
            owner, slug = split_ref(ref, "model")
            ref = f"{owner}/{slug}"
            meta = await c.call(MODELS, "GetModel", {"ownerSlug": owner, "modelSlug": slug})
            title = meta.get("title", ref)
            text = f"# {title}\n{meta.get('subtitle', '')}\n\n{meta.get('description', '')}"
        return {
            "id": f"{kind}:{ref}",
            "title": title,
            "text": app.truncate(text),
            "url": _url(kind, ref),
            "metadata": {"type": kind},
        }

    # --- Models ---------------------------------------------------------
    @app.tool(title="List / search models")
    async def models_list(search: str | None = None, owner: str | None = None, sort_by: str | None = None, page_size: int = 20, page_token: str | None = None) -> dict:
        """Search Kaggle Models. sort_by: hotness, download_count, vote_count, notebook_count,
        publish_time, create_time, update_time."""
        resp = await c.call(MODELS, "ListModels", {
            "search": search, "owner": owner, "sortBy": enum_value(sort_by, MODEL_SORT, "sort_by"),
            "pageSize": max(1, min(page_size, 50)), "pageToken": page_token,
        })
        keys = ["ref", "title", "subtitle", "author", "voteCount", "updateTime", "isPrivate"]
        return {"models": [pick(m, keys) for m in resp.get("models", [])], "nextPageToken": resp.get("nextPageToken") or None}

    @app.tool(title="Model details")
    async def model_get(ref: str) -> dict:
        """Details of a model ('owner/model-slug'), including its variations (framework/instance)
        which are used in notebook model_sources as 'owner/model/framework/variation/version'."""
        owner, slug = split_ref(ref, "model")
        m = await c.call(MODELS, "GetModel", {"ownerSlug": owner, "modelSlug": slug})
        out = pick(m, ["ref", "title", "subtitle", "author", "url", "isPrivate", "voteCount", "publishTime", "updateTime"])
        out["description"] = app.truncate(m.get("description") or "", 8000)
        out["variations"] = [
            pick(i, ["slug", "framework", "versionNumber", "licenseName", "fineTunable", "url"])
            for i in m.get("instances", [])
        ]
        return out
