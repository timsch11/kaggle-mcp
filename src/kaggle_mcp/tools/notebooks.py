"""Notebook (Kaggle "kernel") tools: browse, read, write, execute, monitor."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from mcp.server.mcpserver import Context

from kaggle_mcp.core import App, enum_value, pick, split_ref
from kaggle_mcp.notebooks import (
    Cell,
    CellEdit,
    apply_edits,
    cells_from_percent_script,
    new_notebook,
    parse_notebook,
    prepare_for_push,
    render_notebook,
)
from kaggle_mcp.workspace import safe_filename

SVC = "kernels.KernelsApiService"

SORT = {
    k: k.upper()
    for k in [
        "hotness", "comment_count", "date_created", "date_run", "relevance",
        "score_ascending", "score_descending", "view_count", "vote_count",
    ]
}
TERMINAL = {"COMPLETE", "ERROR", "CANCEL_ACKNOWLEDGED"}
META_KEYS = [
    "ref", "title", "author", "lastRunTime", "language", "kernelType", "isPrivate", "enableGpu",
    "enableTpu", "enableInternet", "machineShape", "currentVersionNumber", "totalVotes",
    "datasetDataSources", "competitionDataSources", "kernelDataSources", "modelDataSources",
]


def _parse_log(raw: str | None) -> str:
    """Kaggle logs are a JSON list of {stream_name, time, data}; flatten to text."""
    if not raw:
        return ""
    try:
        entries = json.loads(raw)
        if isinstance(entries, list):
            return "".join(str(e.get("data", "")) for e in entries if isinstance(e, dict))
    except (json.JSONDecodeError, TypeError):
        pass
    return raw


def _build_text(
    kernel_type: str,
    language: str,
    cells: list[Cell] | None,
    notebook_json: str | None,
    source: str | None,
) -> str:
    given = [x is not None for x in (cells, notebook_json, source)]
    if sum(given) != 1:
        raise ValueError("Provide exactly one of: cells, notebook_json, source")
    if kernel_type == "script":
        if source is None:
            raise ValueError("Scripts take 'source' (plain code)")
        return source
    if cells is not None:
        nb = new_notebook(cells, language)
    elif notebook_json is not None:
        nb = parse_notebook(notebook_json)
    else:
        nb = new_notebook(cells_from_percent_script(source or ""), language)
    return prepare_for_push(nb)


def _save_result(resp: dict, run: bool) -> dict:
    out = pick(resp, ["ref", "url", "versionNumber", "error"])
    for k in ("invalidTags", "invalidDatasetSources", "invalidCompetitionSources", "invalidKernelSources", "invalidModelSources"):
        if resp.get(k):
            out[k] = resp[k]
    if resp.get("error"):
        out["ok"] = False
    else:
        out["ok"] = True
        out["next_step"] = (
            "Run started. Use notebook_wait or notebook_status, then notebook_output."
            if run
            else "Saved without running (quick save)."
        )
    return out


def register(app: App) -> None:
    c = app.client

    @app.tool(title="List / search notebooks")
    async def notebooks_list(
        search: str | None = None,
        user: str | None = None,
        mine: bool = False,
        competition: str | None = None,
        dataset: str | None = None,
        language: Literal["all", "python", "r", "sqlite", "julia"] | None = None,
        kernel_type: Literal["all", "script", "notebook"] | None = None,
        sort_by: str | None = None,
        page_size: int = 20,
        page_token: str | None = None,
    ) -> dict:
        """Search Kaggle notebooks. Use `mine=true` to list your own notebooks (including private).
        `dataset` is 'owner/slug'; `competition` is the competition slug.
        sort_by: hotness, comment_count, date_created, date_run, relevance, score_ascending,
        score_descending, view_count, vote_count."""
        if dataset:
            split_ref(dataset, "dataset")
        body = {
            "search": search,
            "user": user,
            "group": "PROFILE" if mine else "EVERYONE",
            "competition": competition,
            "dataset": dataset,
            "language": language,
            "kernelType": kernel_type,
            "sortBy": enum_value(sort_by, SORT, "sort_by"),
            "pageSize": max(1, min(page_size, 100)),
            "pageToken": page_token,
        }
        resp = await c.call(SVC, "ListKernels", body)
        kernels = [pick(k, ["ref", "title", "author", "lastRunTime", "language", "kernelType", "isPrivate", "totalVotes"]) for k in resp.get("kernels", [])]
        return {"notebooks": kernels, "nextPageToken": resp.get("nextPageToken") or None}

    @app.tool(title="Read a notebook")
    async def notebook_get(
        ref: str,
        format: Literal["cells", "ipynb", "metadata"] = "cells",
        include_outputs: bool = True,
        version: str | None = None,
        offset: int = 0,
        max_chars: int | None = None,
    ) -> dict:
        """Read a notebook's code and settings. `ref` is 'owner/slug', a kaggle.com/code URL,
        or a bare slug for one of your own notebooks.
        format='cells' returns numbered cells (use these indices with notebook_update edits),
        'ipynb' returns the raw notebook JSON, 'metadata' returns settings only.
        Long content is paged with offset/max_chars."""
        owner, slug = await app.own_ref(ref)
        resp = await c.call(SVC, "GetKernel", {"userName": owner, "kernelSlug": slug, "versionLabel": version})
        meta = pick(resp.get("metadata"), META_KEYS)
        blob = resp.get("blob") or {}
        result: dict = {"metadata": meta}
        if format == "metadata":
            return result
        src = blob.get("source") or ""
        lang = (blob.get("language") or meta.get("language") or "python").lower()
        if format == "cells" and (blob.get("kernelType") or meta.get("kernelType")) == "notebook":
            try:
                src = render_notebook(parse_notebook(src), lang, include_outputs)
            except ValueError:
                pass  # fall back to raw source
        offset = max(0, offset)
        result["total_chars"] = len(src)
        result["offset"] = offset
        result["content"] = app.truncate(src[offset:], max_chars)
        return result

    @app.tool(title="Create a notebook", writes=True)
    async def notebook_create(
        ref: str,
        title: str,
        cells: list[Cell] | None = None,
        notebook_json: str | None = None,
        source: str | None = None,
        kernel_type: Literal["notebook", "script"] = "notebook",
        language: Literal["python", "r"] = "python",
        is_private: bool = True,
        enable_internet: bool = True,
        enable_gpu: bool = False,
        enable_tpu: bool = False,
        machine_shape: str | None = None,
        dataset_sources: list[str] | None = None,
        competition_sources: list[str] | None = None,
        kernel_sources: list[str] | None = None,
        model_sources: list[str] | None = None,
        run: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict:
        """Create a new notebook on Kaggle and (by default) run it end-to-end.

        `ref` is the notebook slug, e.g. 'titanic-baseline' (your username is filled in
        automatically; 'username/slug' also works). The slug should match the title,
        e.g. title 'Titanic Baseline' -> 'titanic-baseline'. Provide content as exactly one of:
        `cells` (list of {cell_type, source}), `notebook_json` (full .ipynb JSON string), or
        `source` (plain code; for notebooks '# %%' / '# %% [markdown]' markers split cells).
        Sources: datasets 'owner/slug', competitions 'slug', kernels 'owner/slug',
        models 'owner/model/framework/variation/version'.
        machine_shape optionally selects the accelerator (e.g. 'NvidiaTeslaT4', 'NvidiaTeslaT4Highmem', 'TpuV5E8').
        Private by default. run=false saves without executing."""
        owner, slug = await app.own_ref(ref)
        if len(title.strip()) < 5:
            raise ValueError("Title must be at least 5 characters")
        text = _build_text(kernel_type, language, cells, notebook_json, source)
        body = {
            "slug": f"{owner}/{slug}",
            "newTitle": title.strip(),
            "text": text,
            "language": language,
            "kernelType": kernel_type,
            "isPrivate": is_private,
            "enableGpu": enable_gpu,
            "enableTpu": enable_tpu,
            "enableInternet": enable_internet,
            "machineShape": machine_shape,
            "datasetDataSources": dataset_sources or [],
            "competitionDataSources": competition_sources or [],
            "kernelDataSources": kernel_sources or [],
            "modelDataSources": model_sources or [],
            "sessionTimeoutSeconds": timeout_seconds,
            "kernelExecutionType": None if run else "QUICK_SAVE",
        }
        return _save_result(await c.call(SVC, "SaveKernel", body), run)

    @app.tool(title="Edit / update a notebook", writes=True)
    async def notebook_update(
        ref: str,
        edits: list[CellEdit] | None = None,
        cells: list[Cell] | None = None,
        notebook_json: str | None = None,
        source: str | None = None,
        title: str | None = None,
        is_private: bool | None = None,
        enable_internet: bool | None = None,
        enable_gpu: bool | None = None,
        enable_tpu: bool | None = None,
        machine_shape: str | None = None,
        dataset_sources: list[str] | None = None,
        competition_sources: list[str] | None = None,
        kernel_sources: list[str] | None = None,
        model_sources: list[str] | None = None,
        run: bool = True,
        timeout_seconds: int | None = None,
    ) -> dict:
        """Save a new version of an existing notebook you own, then (by default) run it.

        Either apply targeted `edits` to the current cells (ops: replace/insert/append/delete with
        0-based indices from notebook_get format='cells'), or replace all content with one of
        `cells`, `notebook_json` or `source`. With neither, the current content is re-saved
        (useful to change settings). Settings left as null keep their current value; source lists
        replace the existing list when given."""
        owner, slug = await app.own_ref(ref)
        current = await c.call(SVC, "GetKernel", {"userName": owner, "kernelSlug": slug})
        meta = current.get("metadata") or {}
        blob = current.get("blob") or {}
        kernel_type = blob.get("kernelType") or meta.get("kernelType") or "notebook"
        language = (blob.get("language") or meta.get("language") or "python").lower()

        replacing = any(x is not None for x in (cells, notebook_json, source))
        if edits and replacing:
            raise ValueError("Use either 'edits' or full replacement content, not both")
        if replacing:
            text = _build_text(kernel_type, language, cells, notebook_json, source)
        elif edits:
            if kernel_type != "notebook":
                raise ValueError("Cell edits only apply to notebooks; pass 'source' for scripts")
            text = prepare_for_push(apply_edits(parse_notebook(blob.get("source") or ""), edits))
        else:
            src = blob.get("source") or ""
            text = prepare_for_push(parse_notebook(src)) if kernel_type == "notebook" else src

        def keep(new, key, default=None):
            if new is not None:
                return new
            value = meta.get(key, default)
            # The API reports an unset machine shape as the string "None".
            return default if value in (None, "", "None") else value

        body = {
            "slug": f"{owner}/{slug}",
            "newTitle": title or meta.get("title"),
            "text": text,
            "language": language,
            "kernelType": kernel_type,
            "isPrivate": keep(is_private, "isPrivate", True),
            "enableGpu": keep(enable_gpu, "enableGpu", False),
            "enableTpu": keep(enable_tpu, "enableTpu", False),
            "enableInternet": keep(enable_internet, "enableInternet", True),
            "machineShape": keep(machine_shape, "machineShape"),
            "dockerImage": meta.get("dockerImage"),
            "categoryIds": meta.get("categoryIds") or [],
            "datasetDataSources": keep(dataset_sources, "datasetDataSources", []),
            "competitionDataSources": keep(competition_sources, "competitionDataSources", []),
            "kernelDataSources": keep(kernel_sources, "kernelDataSources", []),
            "modelDataSources": keep(model_sources, "modelDataSources", []),
            "sessionTimeoutSeconds": timeout_seconds,
            "kernelExecutionType": None if run else "QUICK_SAVE",
        }
        return _save_result(await c.call(SVC, "SaveKernel", body), run)

    @app.tool(title="Run a notebook", writes=True)
    async def notebook_run(ref: str, machine_shape: str | None = None, timeout_seconds: int | None = None) -> dict:
        """Execute an existing notebook you own as-is (saves a new version and runs all cells).
        Poll with notebook_wait / notebook_status, then read results with notebook_output."""
        return await notebook_update(ref=ref, machine_shape=machine_shape, timeout_seconds=timeout_seconds, run=True)

    @app.tool(title="Notebook run status")
    async def notebook_status(ref: str) -> dict:
        """Status of the latest run: QUEUED, RUNNING, COMPLETE, ERROR, CANCEL_REQUESTED, CANCEL_ACKNOWLEDGED."""
        owner, slug = await app.own_ref(ref)
        resp = await c.call(SVC, "GetKernelSessionStatus", {"userName": owner, "kernelSlug": slug})
        return {"ref": f"{owner}/{slug}", "status": resp.get("status", "QUEUED"), "failureMessage": resp.get("failureMessage")}

    @app.tool(title="Wait for a notebook run")
    async def notebook_wait(ref: str, ctx: Context, timeout_seconds: int = 300, poll_seconds: int = 15) -> dict:
        """Poll a notebook run until it finishes or `timeout_seconds` (max 900) elapses.
        If still running at the timeout, call again. Reports progress while waiting."""
        timeout = max(10, min(timeout_seconds, 900))
        poll = max(5, min(poll_seconds, 60))
        start = time.monotonic()
        while True:
            st = await notebook_status(ref)
            elapsed = int(time.monotonic() - start)
            if st["status"] in TERMINAL:
                st["elapsed_seconds"] = elapsed
                st["finished"] = True
                return st
            if elapsed >= timeout:
                st["elapsed_seconds"] = elapsed
                st["finished"] = False
                return st
            try:
                await ctx.report_progress(elapsed, timeout, f"{st['status']} ({elapsed}s)")
            except Exception:
                pass
            await asyncio.sleep(min(poll, timeout - elapsed))

    @app.tool(title="Notebook output & logs")
    async def notebook_output(ref: str, include_log: bool = True, log_tail_chars: int = 20000, page_token: str | None = None) -> dict:
        """List output files of the latest run and return the tail of the execution log
        (stdout/stderr, errors). Download files with notebook_download_output."""
        owner, slug = await app.own_ref(ref)
        resp = await c.call(SVC, "ListKernelSessionOutput", {"userName": owner, "kernelSlug": slug, "pageToken": page_token})
        files = [f.get("fileName") for f in resp.get("files", []) if f.get("fileName")]
        out: dict = {"files": files, "nextPageToken": resp.get("nextPageToken") or None}
        if include_log:
            log = _parse_log(resp.get("log"))
            tail = max(1000, min(log_tail_chars, app.settings.max_output_chars))
            out["log_total_chars"] = len(log)
            out["log"] = log[-tail:] if len(log) > tail else log
        return out

    @app.tool(title="Download notebook outputs", local_write=True)
    async def notebook_download_output(ref: str, file_name: str | None = None, dest_dir: str | None = None) -> dict:
        """Download output files of the latest run into the local workspace
        (all files, or just `file_name`). Returns workspace-relative paths."""
        owner, slug = await app.own_ref(ref)
        target_dir = app.workspace.resolve(dest_dir or f"notebooks/{owner}__{slug}/output")
        saved, token = [], None
        while True:
            resp = await c.call(SVC, "ListKernelSessionOutput", {"userName": owner, "kernelSlug": slug, "pageToken": token})
            for f in resp.get("files", []):
                name, url = f.get("fileName"), f.get("url")
                if not name or not url or (file_name and name != file_name):
                    continue
                # Keep sub-directories but sanitize each component.
                parts = [safe_filename(p) for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
                dest = app.workspace.resolve(app.workspace.relative(target_dir) + "/" + "/".join(parts))
                size = await c.download_url(url, dest, app.settings.max_download_bytes)
                saved.append({"path": app.workspace.relative(dest), "bytes": size})
            token = resp.get("nextPageToken")
            if not token:
                break
        if file_name and not saved:
            raise ValueError(f"No output file named {file_name!r}")
        return {"saved": saved}

    @app.tool(title="GPU/TPU quota")
    async def accelerator_quota() -> dict:
        """Remaining weekly GPU and TPU hours for your account."""
        return await c.call(SVC, "GetAcceleratorQuotaStatistics", {})

    @app.tool(title="Delete a notebook", writes=True, destructive=True)
    async def notebook_delete(ref: str, confirm_ref: str) -> dict:
        """Permanently delete one of your notebooks. `confirm_ref` must repeat `ref` exactly.
        Only available when the server runs with KAGGLE_MCP_ALLOW_DELETE=true."""
        owner, slug = await app.own_ref(ref)
        if await app.own_ref(confirm_ref) != (owner, slug):
            raise ValueError("confirm_ref must exactly match ref")
        resp = await c.call(SVC, "DeleteKernel", {"userName": owner, "kernelSlug": slug})
        return {"deleted": not resp.get("errorMessage"), "error": resp.get("errorMessage")}
