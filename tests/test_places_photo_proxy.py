"""The photo proxy: a photo name first, the place's current photo when that name has expired."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import places_router
from app.services import places_service

app = FastAPI()
app.include_router(places_router.router)
client = TestClient(app)
REF = "places/ChIJcZY1XAAFdkgRieYQg6UqpzQ/photos/AUc7tXabc_def-123"
PLACE = "ChIJcZY1XAAFdkgRieYQg6UqpzQ"


def _stub(monkeypatch, by_ref, by_place):
    calls = []
    async def ref_fetch(ref, w=600):
        calls.append(("ref", ref, w)); return by_ref
    async def place_fetch(place, w=600):
        calls.append(("place", place, w)); return by_place
    monkeypatch.setattr(places_service, "fetch_photo_bytes", ref_fetch)
    monkeypatch.setattr(places_service, "fetch_place_photo_bytes", place_fetch)
    return calls


def test_photo_name_is_used_first(monkeypatch):
    calls = _stub(monkeypatch, (b"jpg", "image/jpeg"), None)
    r = client.get("/api/places/photo", params={"ref": REF, "place": PLACE, "w": 900})
    assert r.status_code == 200 and r.content == b"jpg" and calls == [("ref", REF, 900)]


def test_expired_name_falls_back_to_the_place(monkeypatch):
    calls = _stub(monkeypatch, None, (b"new", "image/jpeg"))
    r = client.get("/api/places/photo", params={"ref": REF, "place": PLACE})
    assert r.status_code == 200 and r.content == b"new" and [c[0] for c in calls] == ["ref", "place"]


def test_nothing_found_is_404(monkeypatch):
    _stub(monkeypatch, None, None)
    assert client.get("/api/places/photo", params={"ref": REF, "place": PLACE}).status_code == 404


def test_only_googles_shapes_are_accepted(monkeypatch):
    calls = _stub(monkeypatch, (b"x", "image/jpeg"), (b"x", "image/jpeg"))
    for params in ({"ref": "https://evil.example/x"}, {"ref": "places/a/../../x"}, {"place": "a/b"}, {}):
        assert client.get("/api/places/photo", params=params).status_code == 400
    assert calls == []


def test_width_is_clamped(monkeypatch):
    calls = _stub(monkeypatch, (b"x", "image/jpeg"), None)
    client.get("/api/places/photo", params={"ref": REF, "w": 99999})
    assert calls[-1][2] == 1600
