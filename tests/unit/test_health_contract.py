import json
import urllib.error
import urllib.request

from app.infrastructure.worker_health import start_health_server


def test_worker_liveness_is_independent_from_dependency_readiness() -> None:
    server = start_health_server(0, {"dependency": ("127.0.0.1", 1)})
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert json.load(urllib.request.urlopen(f"{base}/health/live"))["status"] == "live"
        try:
            urllib.request.urlopen(f"{base}/health/ready")
        except urllib.error.HTTPError as error:
            assert error.code == 503
            assert json.load(error)["dependencies"] == {"dependency": False}
        else:
            raise AssertionError("readiness must fail when a required dependency is unavailable")
    finally:
        server.shutdown()
