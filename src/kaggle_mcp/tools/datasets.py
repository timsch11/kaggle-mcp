"""Dataset tools: search, inspect, download, create and version datasets."""

from __future__ import annotations

import time

from kaggle_mcp.core import App, enum_value, pick
from kaggle_mcp.tools.files import finalize_download, read_upload
from kaggle_mcp.workspace import safe_filename

SVC = "datasets.DatasetApiService"
BLOBS = "blobs.BlobApiService"

SORT = {k: f"DATASET_SORT_BY_{k.upper()}" for k in [
    "hottest", "votes", "updated", "active", "published", "relevance", "last_viewed", "usability",
    "download_count", "notebook_count",
]}
DS_KEYS = [
    "ref", "title", "subtitle", "url", "ownerRef", "totalBytes", "lastUpdated", "downloadCount", "voteCount",
    "usabilityRating", "licenseName", "isPrivate", "currentVersionNumber", "description",
]


def register(app: App) -> None:
    c = app.client

    @app.tool(title="List / search datasets")
    async def datasets_list(
        search: str | None = None,
        user: str | None = None,
        mine: bool = False,
        sort_by: str | None = None,
        page: int = 1,
    ) -> dict:
        """Search public datasets, a user's datasets (`user`), or your own (`mine=true`).
        sort_by: hottest, votes, updated, active, published, relevance, last_viewed, usability,
        download_count, notebook_count."""
        group = "DATASET_SELECTION_GROUP_MY" if mine else ("DATASET_SELECTION_GROUP_USER" if user else "DATASET_SELECTION_GROUP_PUBLIC")
        body = {"search": search, "user": user, "group": group, "sortBy": enum_value(sort_by, SORT, "sort_by"), "page": max(1, page)}
        resp = await c.call(SVC, "ListDatasets", body)
        keys = ["ref", "title", "subtitle", "totalBytes", "lastUpdated", "downloadCount", "voteCount", "usabilityRating", "isPrivate"]
        return {"datasets": [pick(d, keys) for d in resp.get("datasets", [])], "page": page}

    @app.tool(title="Dataset details")
    async def dataset_get(ref: str) -> dict:
        """Details and description of a dataset ('owner/slug' or kaggle.com/datasets URL)."""
        owner, slug = await app.own_ref(ref, "dataset")
        resp = await c.call(SVC, "GetDataset", {"ownerSlug": owner, "datasetSlug": slug})
        out = pick(resp, DS_KEYS)
        if "description" in out:
            out["description"] = app.truncate(out["description"], 8000)
        if resp.get("tags"):
            out["tags"] = [t.get("name") for t in resp["tags"] if t.get("name")]
        return out

    @app.tool(title="List dataset files")
    async def dataset_files(ref: str, version: int | None = None, page_token: str | None = None) -> dict:
        """List files in a dataset (name, size, columns where known)."""
        owner, slug = await app.own_ref(ref, "dataset")
        resp = await c.call(SVC, "ListDatasetFiles", {
            "ownerSlug": owner, "datasetSlug": slug, "datasetVersionNumber": version, "pageToken": page_token, "pageSize": 200,
        })
        files = []
        for f in resp.get("datasetFiles", []):
            item = pick(f, ["name", "totalBytes", "creationDate", "description"])
            cols = [col.get("name") for col in f.get("columns", []) if col.get("name")]
            if cols:
                item["columns"] = cols[:100]
            files.append(item)
        return {"files": files, "nextPageToken": resp.get("nextPageToken") or None, "error": resp.get("errorMessage")}

    @app.tool(title="Download a dataset", local_write=True)
    async def dataset_download(ref: str, file_name: str | None = None, version: int | None = None, dest_dir: str | None = None) -> dict:
        """Download a whole dataset (zip) or a single `file_name` into the workspace."""
        owner, slug = await app.own_ref(ref, "dataset")
        target = app.workspace.resolve(dest_dir or f"datasets/{owner}__{slug}")
        dest = target / (safe_filename(file_name) if file_name else f"{slug}.zip")
        body = {"ownerSlug": owner, "datasetSlug": slug, "fileName": file_name, "datasetVersionNumber": version}
        n = await c.download(SVC, "DownloadDataset", body, dest, app.settings.max_download_bytes)
        return finalize_download(app, dest, n)

    async def _upload_files(paths: list[str]) -> list[dict]:
        if not paths:
            raise ValueError("At least one file is required")
        if len(paths) > 200:
            raise ValueError("Too many files (max 200 per call)")
        tokens = []
        for rel in paths:
            data, name = read_upload(app, rel, None, "")
            size = data.stat().st_size if not isinstance(data, bytes) else len(data)
            start = await c.call(BLOBS, "StartBlobUpload", {
                "type": "DATASET", "name": name, "contentLength": size, "lastModifiedEpochSeconds": int(time.time()),
            })
            if not start.get("createUrl") or not start.get("token"):
                raise ValueError(f"Kaggle did not return an upload URL for {rel}")
            await c.upload_to_url(start["createUrl"], data)
            tokens.append({"token": start["token"]})
        return tokens

    @app.tool(title="Create a dataset", writes=True)
    async def dataset_create(
        ref: str,
        title: str,
        file_paths: list[str],
        is_private: bool = True,
        license_name: str = "unknown",
        subtitle: str | None = None,
        description: str | None = None,
    ) -> dict:
        """Create a new dataset from files in the workspace. `ref` is the dataset slug
        (your username is filled in automatically; 'username/slug' also works).
        Private by default. license_name e.g. 'CC0-1.0', 'CC-BY-SA-4.0', 'MIT', 'unknown'."""
        owner, slug = await app.own_ref(ref, "dataset")
        if len(title.strip()) < 6:
            raise ValueError("Title must be at least 6 characters")
        files = await _upload_files(file_paths)
        resp = await c.call(SVC, "CreateDataset", {
            "ownerSlug": owner, "slug": slug, "title": title.strip(), "licenseName": license_name,
            "isPrivate": is_private, "files": files, "subtitle": subtitle, "description": description,
        })
        out = pick(resp, ["ref", "url", "status", "error", "invalidTags"])
        if not resp.get("error"):
            out["next_step"] = "Kaggle is processing the upload; poll dataset_status until READY before downloading."
        return out

    @app.tool(title="New dataset version", writes=True)
    async def dataset_create_version(ref: str, file_paths: list[str], version_notes: str, delete_old_versions: bool = False) -> dict:
        """Publish a new version of one of your datasets with the given workspace files
        (the new version contains exactly these files)."""
        owner, slug = await app.own_ref(ref, "dataset")
        files = await _upload_files(file_paths)
        resp = await c.call(SVC, "CreateDatasetVersion", {
            "ownerSlug": owner, "datasetSlug": slug,
            "body": {"versionNotes": version_notes, "deleteOldVersions": delete_old_versions, "files": files},
        })
        out = pick(resp, ["ref", "url", "status", "error", "invalidTags"])
        if not resp.get("error"):
            out["next_step"] = "The new version is processing; poll dataset_status until READY."
        return out

    @app.tool(title="Dataset processing status")
    async def dataset_status(ref: str) -> dict:
        """Processing status of a dataset after create/version (READY, FAILED, ...)."""
        owner, slug = await app.own_ref(ref, "dataset")
        return await c.call(SVC, "GetDatasetStatus", {"ownerSlug": owner, "datasetSlug": slug})

    @app.tool(title="Delete a dataset", writes=True, destructive=True)
    async def dataset_delete(ref: str, confirm_ref: str) -> dict:
        """Permanently delete one of your datasets. `confirm_ref` must repeat `ref` exactly.
        Only available when the server runs with KAGGLE_MCP_ALLOW_DELETE=true."""
        owner, slug = await app.own_ref(ref, "dataset")
        if await app.own_ref(confirm_ref, "dataset") != (owner, slug):
            raise ValueError("confirm_ref must exactly match ref")
        resp = await c.call(SVC, "DeleteDataset", {"ownerSlug": owner, "datasetSlug": slug})
        return {"deleted": not resp.get("error"), "error": resp.get("error")}
