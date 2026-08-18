from fastapi.testclient import TestClient

import app.main as main
from app.config import MAX_FIELDS_PER_REQUEST
from app.mireye_client import MireyeClient


def _local_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(main, "mireye_client", MireyeClient(mode="local"))
    return TestClient(main.app)


def test_ask_rejects_latitude_without_longitude(monkeypatch):
    client = _local_client(monkeypatch)

    response = client.post("/v1/ask", json={"lat": 32.5})

    assert response.status_code == 400
    assert "dossier" not in response.json()


def test_ask_rejects_longitude_without_latitude(monkeypatch):
    client = _local_client(monkeypatch)

    response = client.post("/v1/ask", json={"lng": -97.0})

    assert response.status_code == 400
    assert "dossier" not in response.json()


def test_ask_rejects_missing_location_without_first_parcel_fallback(monkeypatch):
    client = _local_client(monkeypatch)

    response = client.post("/v1/ask", json={})

    assert response.status_code == 400
    assert "dossier" not in response.json()


def test_ask_ambiguous_address_returns_choices_not_first_match(monkeypatch):
    client = _local_client(monkeypatch)

    response = client.post("/v1/ask", json={"address": "PCL-0000"})
    data = response.json()

    assert response.status_code in {300, 400, 409, 422}
    detail = data.get("detail", data)
    assert "ambig" in str(detail).lower()
    candidates = detail.get("candidates") if isinstance(detail, dict) else data.get("candidates")
    assert isinstance(candidates, list)
    assert len(candidates) >= 2


def test_ask_more_than_max_fields_does_not_silently_omit_fields(monkeypatch):
    client = _local_client(monkeypatch)
    fields = [f"contract_field_{i}" for i in range(MAX_FIELDS_PER_REQUEST + 3)]

    response = client.post(
        "/v1/ask",
        json={"lat": 32.5, "lng": -97.0, "fields": fields},
    )
    data = response.json()

    if response.status_code != 200:
        assert response.status_code in {400, 413, 422}
        assert "field" in str(data.get("detail", data)).lower()
        return

    returned = set(data["dossier"].get("fields", {}))
    omitted = [field for field in fields if field not in returned]
    if not omitted:
        return

    detail = data.get("field_pagination") or data.get("fields_omitted") or data["dossier"].get("fields_omitted")
    assert detail, f"Fields omitted without an explicit contract signal: {omitted}"
