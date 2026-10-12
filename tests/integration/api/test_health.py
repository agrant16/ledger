import pytest
from fastapi.testclient import TestClient

from ledger.main import app

pytestmark = pytest.mark.usefixtures("migrated_database")


def test_health_reaches_the_database() -> None:
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
