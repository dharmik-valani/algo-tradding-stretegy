from algo.paper.desk_store import load_runtime, save_runtime


def test_runtime_roundtrip(tmp_path, monkeypatch):
    import algo.paper.desk_store as store

    monkeypatch.setattr(store, "RUNTIME_PATH", tmp_path / "paper_runtime.json")
    payload = {
        "saved_at": "2026-09-07T07:00:00+00:00",
        "runners": {"demo": {"type": "basket", "selected": ["TECHM"], "brokers": {}}},
    }
    save_runtime(payload)
    loaded = load_runtime()
    assert loaded["runners"]["demo"]["selected"] == ["TECHM"]
    assert loaded["saved_at"] == payload["saved_at"]
