def test_app_imports():
    from apps.studio.server.app import app

    assert app.title == "Lion Studio Server"


def test_stats_route():
    from fastapi.testclient import TestClient

    from apps.studio.server.app import app

    client = TestClient(app)
    r = client.get("/api/stats")
    assert r.status_code == 200
    data = r.json()
    # stats shape: {"playbooks": int, "agents": int, "runs": int, "shows": int}
    for key in ("playbooks", "agents", "runs", "shows"):
        assert key in data
        assert isinstance(data[key], int)


def test_shows_list():
    from fastapi.testclient import TestClient

    from apps.studio.server.app import app

    client = TestClient(app)
    r = client.get("/api/shows")
    assert r.status_code == 200
    assert isinstance(r.json(), list)
