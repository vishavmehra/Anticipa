# ruff: noqa: E501
"""Document store: local round trip, exact bytes, key safety, Supabase Storage REST (mocked), backend selection."""

from __future__ import annotations

import httpx
import pytest
import respx

from anticipa.datafabric import docstore
from anticipa.datafabric.docstore import (
    LocalDocumentStore,
    SupabaseStorageDocumentStore,
    content_hash,
    get_document_store,
    make_storage_key,
    validate_key,
)

KEY = "syn-20261106-s42/0aaa0000-0000-5000-8000-000000000001/0ddd0000-0000-5000-8000-000000000001.txt"
TEXT = "Family history\r\nमाँ को कैंसर\nالأم بسرطان\n"


def test_local_round_trip_keeps_exact_text_and_hash(tmp_path):
    store = LocalDocumentStore(tmp_path)
    assert not store.exists(KEY)
    digest = store.write_text(KEY, TEXT)
    assert digest == content_hash(TEXT)
    assert store.exists(KEY)
    assert store.read_text(KEY) == TEXT  # CRLF, Devanagari and Arabic survive byte for byte
    assert (tmp_path / KEY).read_bytes() == TEXT.encode("utf-8")
    store.write_text(KEY, "changed")  # overwrite is allowed (idempotent regeneration)
    assert store.read_text(KEY) == "changed"
    assert not [p for p in tmp_path.rglob("*") if p.name.endswith(".tmp")]


def test_local_layout_matches_contract(tmp_path):
    key = make_storage_key("syn-20261106-s42", "r1", "d1")
    assert key == "syn-20261106-s42/r1/d1.txt"
    LocalDocumentStore(tmp_path).write_text(key, "x")
    assert (tmp_path / "syn-20261106-s42" / "r1" / "d1.txt").is_file()


def test_missing_document_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        LocalDocumentStore(tmp_path).read_text(KEY)


@pytest.mark.parametrize(
    "bad", ["", "/abs/x.txt", "../x.txt", "a/../../x.txt", "a//b.txt", ".hidden/x", "a/b c.txt", "a\\b.txt"]
)
def test_unsafe_keys_are_rejected(tmp_path, bad):
    with pytest.raises(ValueError):
        validate_key(bad)
    with pytest.raises(ValueError):
        LocalDocumentStore(tmp_path).write_text(bad, "x")


BASE = "https://example-project.supabase.co"
SECRET = "service-role-test-key"


@respx.mock
def test_supabase_write_read_exists_and_missing():
    store = SupabaseStorageDocumentStore(BASE, SECRET, client=httpx.Client())
    up = respx.post(f"{BASE}/storage/v1/object/documents/{KEY}").mock(
        return_value=httpx.Response(200, json={"Key": "ok"})
    )
    get = respx.get(f"{BASE}/storage/v1/object/authenticated/documents/{KEY}").mock(
        return_value=httpx.Response(200, content=TEXT.encode("utf-8"))
    )
    head = respx.head(f"{BASE}/storage/v1/object/authenticated/documents/{KEY}").mock(
        return_value=httpx.Response(200)
    )
    assert store.write_text(KEY, TEXT) == content_hash(TEXT)
    sent = up.calls.last.request
    assert sent.content == TEXT.encode("utf-8")
    assert sent.headers["x-upsert"] == "true"
    assert sent.headers["authorization"] == f"Bearer {SECRET}" and sent.headers["apikey"] == SECRET
    assert sent.headers["content-type"].startswith("text/plain")
    assert store.read_text(KEY) == TEXT and get.called
    assert store.exists(KEY) and head.called

    get.mock(return_value=httpx.Response(404, json={"error": "not_found"}))
    head.mock(return_value=httpx.Response(404))
    with pytest.raises(FileNotFoundError):
        store.read_text(KEY)
    assert not store.exists(KEY)
    get.mock(
        return_value=httpx.Response(
            400, json={"statusCode": "404", "error": "not_found", "message": "Object not found"}
        )
    )
    with pytest.raises(FileNotFoundError):
        store.read_text(KEY)


@respx.mock
def test_supabase_server_error_is_not_swallowed():
    store = SupabaseStorageDocumentStore(BASE, SECRET, client=httpx.Client())
    respx.post(f"{BASE}/storage/v1/object/documents/{KEY}").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        store.write_text(KEY, "x")


@respx.mock
def test_supabase_ensure_bucket_creates_private_bucket_and_tolerates_existing():
    store = SupabaseStorageDocumentStore(BASE, SECRET, client=httpx.Client())
    route = respx.post(f"{BASE}/storage/v1/bucket").mock(
        return_value=httpx.Response(200, json={"name": "documents"})
    )
    store.ensure_bucket()
    assert route.calls.last.request.read() == b'{"id":"documents","name":"documents","public":false}'
    route.mock(
        return_value=httpx.Response(
            409, json={"error": "Duplicate", "message": "The resource already exists"}
        )
    )
    store.ensure_bucket()


def test_get_document_store_selects_backend_from_settings(monkeypatch, tmp_path):
    get_document_store.cache_clear()
    monkeypatch.setenv("ANTICIPA_DOCUMENT_STORE", "local")
    monkeypatch.setenv("ANTICIPA_DOCUMENT_DIR", str(tmp_path))
    store = get_document_store()
    assert isinstance(store, LocalDocumentStore) and store.root == tmp_path
    assert isinstance(store, docstore.DocumentStore)  # structural protocol

    get_document_store.cache_clear()
    monkeypatch.setenv("ANTICIPA_DOCUMENT_STORE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", BASE)
    monkeypatch.setenv("SUPABASE_SECRET_KEY", SECRET)
    from anticipa.config import get_settings

    get_settings.cache_clear()
    try:
        assert isinstance(get_document_store(), SupabaseStorageDocumentStore)
    finally:
        get_settings.cache_clear()
        get_document_store.cache_clear()
