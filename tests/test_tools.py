"""Functional tests for tools against a fake Kaggle API."""

from __future__ import annotations

import json

import httpx

from kaggle_mcp.notebooks import CellEdit, apply_edits, cells_from_percent_script, new_notebook, parse_notebook, prepare_for_push

from .conftest import result_data

NB = {
    "cells": [
        {"cell_type": "markdown", "metadata": {}, "source": ["# Title"]},
        {"cell_type": "code", "metadata": {}, "source": "import pandas as pd", "outputs": [{"output_type": "stream", "text": "hi"}], "execution_count": 3},
        {"cell_type": "code", "metadata": {}, "source": "print(1)", "outputs": [], "execution_count": None},
    ],
    "metadata": {},
    "nbformat": 4,
    "nbformat_minor": 4,
}
META = {
    "ref": "me/my-notebook", "title": "My Notebook", "language": "python", "kernelType": "notebook",
    "isPrivate": True, "enableGpu": True, "enableInternet": False, "machineShape": "NvidiaTeslaT4",
    "datasetDataSources": ["owner/data"], "competitionDataSources": ["titanic"],
}


def _get_kernel(fake):
    fake.on("kernels.KernelsApiService/GetKernel", {
        "metadata": META,
        "blob": {"source": json.dumps(NB), "language": "python", "kernelType": "notebook", "slug": "me/my-notebook"},
    })


# --- notebook helpers --------------------------------------------------------
def test_apply_edits():
    nb = json.loads(json.dumps(NB))
    apply_edits(nb, [
        CellEdit(op="replace", index=2, source="print(2)"),
        CellEdit(op="insert", index=0, cell_type="markdown", source="intro"),
        CellEdit(op="append", source="x = 1"),
        CellEdit(op="delete", index=1),
    ])
    assert [c["source"] for c in nb["cells"]] == ["intro", "import pandas as pd", "print(2)", "x = 1"]


def test_prepare_for_push_strips_outputs():
    pushed = json.loads(prepare_for_push(json.loads(json.dumps(NB))))
    assert pushed["cells"][1]["outputs"] == []
    assert pushed["cells"][0]["source"] == "# Title"


def test_percent_script_split():
    cells = cells_from_percent_script("# %% [markdown]\n# Hello\n# %%\nx = 1\n# %%\nprint(x)\n")
    assert [(c.cell_type, c.source) for c in cells] == [("markdown", "Hello"), ("code", "x = 1"), ("code", "print(x)")]
    assert cells_from_percent_script("print(1)")[0].source == "print(1)"


# --- notebook tools ----------------------------------------------------------
async def test_notebook_get_cells(make_server, fake):
    _get_kernel(fake)
    data = result_data(await make_server().call_tool("notebook_get", {"ref": "https://www.kaggle.com/code/me/my-notebook"}))
    assert "### Cell 2 [code]" in data["content"] and "print(1)" in data["content"]
    assert data["metadata"]["machineShape"] == "NvidiaTeslaT4"
    assert fake.bodies("kernels.KernelsApiService/GetKernel")[0] == {"userName": "me", "kernelSlug": "my-notebook"}


async def test_notebook_create(make_server, fake):
    fake.on("kernels.KernelsApiService/SaveKernel", {"ref": "/code/me/titanic-baseline", "url": "https://kaggle.com/code/me/titanic-baseline", "versionNumber": 1})
    data = result_data(await make_server().call_tool("notebook_create", {
        "ref": "me/titanic-baseline", "title": "Titanic Baseline",
        "cells": [{"cell_type": "markdown", "source": "# Hi"}, {"cell_type": "code", "source": "print(1)"}],
        "competition_sources": ["titanic"], "enable_gpu": True,
    }))
    assert data["ok"] and data["versionNumber"] == 1
    body = fake.bodies("kernels.KernelsApiService/SaveKernel")[0]
    assert body["slug"] == "me/titanic-baseline" and body["kernelType"] == "notebook"
    assert body["isPrivate"] is True and body["enableGpu"] is True
    assert body["competitionDataSources"] == ["titanic"]
    assert "kernelExecutionType" not in body  # default = save & run
    assert [c["source"] for c in json.loads(body["text"])["cells"]] == ["# Hi", "print(1)"]


async def test_notebook_create_quick_save(make_server, fake):
    fake.on("kernels.KernelsApiService/SaveKernel", {"ref": "r", "url": "u", "versionNumber": 1})
    await make_server().call_tool("notebook_create", {"ref": "me/x-script", "title": "X script", "source": "print(1)", "kernel_type": "script", "run": False})
    body = fake.bodies("kernels.KernelsApiService/SaveKernel")[0]
    assert body["kernelExecutionType"] == "QUICK_SAVE" and body["text"] == "print(1)"


async def test_notebook_update_edits_preserve_settings(make_server, fake):
    _get_kernel(fake)
    fake.on("kernels.KernelsApiService/SaveKernel", {"ref": "r", "url": "u", "versionNumber": 2})
    await make_server().call_tool("notebook_update", {
        "ref": "me/my-notebook", "edits": [{"op": "replace", "index": 2, "source": "print('changed')"}],
    })
    body = fake.bodies("kernels.KernelsApiService/SaveKernel")[0]
    cells = json.loads(body["text"])["cells"]
    assert cells[2]["source"] == "print('changed')" and cells[1]["outputs"] == []
    assert body["enableGpu"] is True and body["enableInternet"] is False
    assert body["machineShape"] == "NvidiaTeslaT4"
    assert body["datasetDataSources"] == ["owner/data"] and body["newTitle"] == "My Notebook"


async def test_notebook_run_and_wait(make_server, fake):
    _get_kernel(fake)
    fake.on("kernels.KernelsApiService/SaveKernel", {"ref": "r", "url": "u", "versionNumber": 3})
    fake.on("kernels.KernelsApiService/GetKernelSessionStatus", {"status": "COMPLETE"})
    server = make_server()
    assert result_data(await server.call_tool("notebook_run", {"ref": "me/my-notebook"}))["ok"]
    st = result_data(await server.call_tool("notebook_wait", {"ref": "me/my-notebook", "timeout_seconds": 10}))
    assert st["finished"] and st["status"] == "COMPLETE"


async def test_notebook_output_parses_log(make_server, fake):
    log = json.dumps([{"stream_name": "stdout", "time": 1, "data": "hello\n"}, {"stream_name": "stderr", "time": 2, "data": "warn\n"}])
    fake.on("kernels.KernelsApiService/ListKernelSessionOutput", {"files": [{"fileName": "submission.csv", "url": "https://storage.googleapis.com/o/s.csv"}], "log": log})
    data = result_data(await make_server().call_tool("notebook_output", {"ref": "me/nb"}))
    assert data["files"] == ["submission.csv"] and data["log"] == "hello\nwarn\n"


async def test_notebook_download_output_sanitizes(make_server, fake, workspace):
    fake.on("kernels.KernelsApiService/ListKernelSessionOutput", {"files": [
        {"fileName": "../../evil.sh", "url": "https://storage.googleapis.com/o/e"},
        {"fileName": "sub/dir/out.csv", "url": "https://storage.googleapis.com/o/c"},
    ]})
    fake.on("GET storage.googleapis.com/o/e", lambda r: httpx.Response(200, content=b"x"))
    fake.on("GET storage.googleapis.com/o/c", lambda r: httpx.Response(200, content=b"a,b"))
    data = result_data(await make_server().call_tool("notebook_download_output", {"ref": "me/nb"}))
    paths = sorted(s["path"] for s in data["saved"])
    assert paths == ["notebooks/me__nb/output/evil.sh", "notebooks/me__nb/output/sub/dir/out.csv"]
    assert not any(p.name == "evil.sh" for p in workspace.parent.glob("*"))


# --- competitions ------------------------------------------------------------
async def test_competition_submit_inline(make_server, fake):
    fake.on("competitions.CompetitionApiService/StartSubmissionUpload", {"token": "blobtok", "createUrl": "https://storage.googleapis.com/up/1"})
    fake.on("PUT storage.googleapis.com/up/1", lambda r: httpx.Response(200))
    fake.on("competitions.CompetitionApiService/CreateSubmission", {"message": "Successfully submitted", "ref": 42})
    data = result_data(await make_server().call_tool("competition_submit", {
        "competition": "titanic", "message": "first try", "content": "PassengerId,Survived\n892,0\n",
    }))
    assert data == {"message": "Successfully submitted", "ref": 42}
    start = fake.bodies("competitions.CompetitionApiService/StartSubmissionUpload")[0]
    assert start["fileName"] == "submission.csv" and start["contentLength"] == 27
    put = [r for r in fake.requests if r.method == "PUT"][0]
    assert put.content == b"PassengerId,Survived\n892,0\n" and "authorization" not in put.headers
    assert fake.bodies("competitions.CompetitionApiService/CreateSubmission")[0]["blobFileTokens"] == "blobtok"


async def test_competitions_list_enum_mapping(make_server, fake):
    fake.on("competitions.CompetitionApiService/ListCompetitions", {"competitions": [{"ref": "titanic", "title": "Titanic", "extra": 1}]})
    data = result_data(await make_server().call_tool("competitions_list", {"category": "getting-started", "sort_by": "prize"}))
    assert data["competitions"] == [{"ref": "titanic", "title": "Titanic"}]
    body = fake.bodies("competitions.CompetitionApiService/ListCompetitions")[0]
    assert body["category"] == "HOST_SEGMENT_GETTING_STARTED" and body["sortBy"] == "COMPETITION_SORT_BY_PRIZE"


# --- datasets ----------------------------------------------------------------
async def test_dataset_create_uploads_workspace_files(make_server, fake, workspace):
    (workspace / "data.csv").write_text("a,b\n1,2\n")
    fake.on("blobs.BlobApiService/StartBlobUpload", {"token": "t1", "createUrl": "https://storage.googleapis.com/up/d"})
    fake.on("PUT storage.googleapis.com/up/d", lambda r: httpx.Response(200))
    fake.on("datasets.DatasetApiService/CreateDataset", {"ref": "me/my-data", "url": "https://kaggle.com/datasets/me/my-data", "status": "ok"})
    data = result_data(await make_server().call_tool("dataset_create", {"ref": "me/my-data", "title": "My dataset", "file_paths": ["data.csv"]}))
    assert data["ref"] == "me/my-data"
    body = fake.bodies("datasets.DatasetApiService/CreateDataset")[0]
    assert body["files"] == [{"token": "t1"}] and body["isPrivate"] is True
    assert fake.bodies("blobs.BlobApiService/StartBlobUpload")[0]["type"] == "DATASET"


# --- ChatGPT search/fetch ------------------------------------------------------
async def test_search_and_fetch(make_server, fake):
    fake.on("search.SearchApiService/ListEntities", {"documents": [
        {"documentType": "DATASET", "title": "Iris", "slug": "iris", "ownerUser": {"userName": "uciml"}},
        {"documentType": "COMPETITION", "title": "Titanic", "slug": "titanic"},
    ]})
    fake.on("datasets.DatasetApiService/GetDataset", {"title": "Iris", "description": "Flowers", "files": [{"name": "Iris.csv", "totalBytes": 5}]})
    server = make_server()
    res = result_data(await server.call_tool("search", {"query": "iris"}))
    assert res["results"][0] == {"id": "dataset:uciml/iris", "title": "Iris", "url": "https://www.kaggle.com/datasets/uciml/iris"}
    assert res["results"][1]["id"] == "competition:titanic"
    doc = result_data(await server.call_tool("fetch", {"id": "dataset:uciml/iris"}))
    assert doc["id"] == "dataset:uciml/iris" and "Flowers" in doc["text"] and "Iris.csv" in doc["text"]


# --- workspace -----------------------------------------------------------------
async def test_workspace_write_read(make_server):
    server = make_server()
    await server.call_tool("workspace_write", {"path": "sub/a.csv", "content": "x,y\n1,2\n"})
    data = result_data(await server.call_tool("workspace_read", {"path": "sub/a.csv"}))
    assert data["content"] == "x,y\n1,2\n" and data["has_more"] is False
    from mcp.server.mcpserver.exceptions import ToolError
    import pytest

    with pytest.raises(ToolError, match="exists"):
        await server.call_tool("workspace_write", {"path": "sub/a.csv", "content": "z"})


def test_new_notebook_roundtrip():
    from kaggle_mcp.notebooks import Cell

    nb = new_notebook([Cell(source="1+1")])
    assert parse_notebook(json.dumps(nb))["cells"][0]["cell_type"] == "code"


def test_clean_ref_and_pick():
    from kaggle_mcp.core import clean_ref, pick

    assert clean_ref("/code/me/nb") == "me/nb"
    assert clean_ref("/datasets/me/ds") == "me/ds"
    assert clean_ref("https://www.kaggle.com/competitions/titanic") == "titanic"
    assert clean_ref("owner/slug") == "owner/slug"
    assert pick({"ref": "/code/a/b", "machineShape": "None", "x": False}, ["ref", "machineShape", "x"]) == {"ref": "a/b", "x": False}


async def test_save_result_ref_normalized(make_server, fake):
    fake.on("kernels.KernelsApiService/SaveKernel", {"ref": "/code/me/abc-notebook", "url": "u", "versionNumber": 1})
    data = result_data(await make_server().call_tool("notebook_create", {"ref": "me/abc-notebook", "title": "Abc notebook", "source": "print(1)"}))
    assert data["ref"] == "me/abc-notebook"


# --- username resolution --------------------------------------------------------
async def test_bare_slug_resolves_username_once(make_server, fake):
    fake.on("security.OAuthService/IntrospectToken", {"active": True, "username": "alice"})
    fake.on("kernels.KernelsApiService/SaveKernel", {"ref": "/code/alice/my-test", "url": "u", "versionNumber": 1})
    fake.on("kernels.KernelsApiService/GetKernelSessionStatus", {"status": "RUNNING"})
    server = make_server()
    data = result_data(await server.call_tool("notebook_create", {"ref": "my-test", "title": "My test notebook", "source": "print(1)", "run": False}))
    assert data["ref"] == "alice/my-test"
    assert fake.bodies("kernels.KernelsApiService/SaveKernel")[0]["slug"] == "alice/my-test"
    await server.call_tool("notebook_status", {"ref": "my-test"})
    assert fake.bodies("kernels.KernelsApiService/GetKernelSessionStatus")[0]["userName"] == "alice"
    assert len(fake.bodies("security.OAuthService/IntrospectToken")) == 1  # cached


async def test_full_ref_skips_lookup(make_server, fake):
    fake.on("kernels.KernelsApiService/GetKernelSessionStatus", {"status": "COMPLETE"})
    await make_server().call_tool("notebook_status", {"ref": "bob/nb"})
    assert fake.bodies("security.OAuthService/IntrospectToken") == []


async def test_auth_status_reports_username(make_server, fake):
    fake.on("kernels.KernelsApiService/GetAcceleratorQuotaStatistics", {})
    fake.on("security.OAuthService/IntrospectToken", {"active": True, "username": "alice"})
    data = result_data(await make_server().call_tool("kaggle_auth_status", {}))
    assert data["username"] == "alice" and "token" not in str(data).lower().replace("credential_source", "")


async def test_inactive_token_errors(make_server, fake):
    fake.on("security.OAuthService/IntrospectToken", {"active": False})
    from mcp.server.mcpserver.exceptions import ToolError
    import pytest

    with pytest.raises(ToolError, match="username"):
        await make_server().call_tool("notebook_status", {"ref": "my-test"})
