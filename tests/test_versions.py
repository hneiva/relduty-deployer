"""The Dockerflow probe, served by an in-process transport rather than the network."""

import httpx
import pytest

from relduty_deployer.versions import HttpxVersionProbe, ProbeError

URL = "https://stage.balrog.nonprod.webservices.mozgcp.net/__version__"

# The exact shape balrog serves.
REAL_PAYLOAD = {
    "commit": "ed0fad325977783b2ac2132aaf7c40662d15aa84",
    "version": "3.120",
    "source": "https://github.com/mozilla-releng/balrog",
    "build": "https://firefox-ci-tc.services.mozilla.com/tasks/DT6zEm3uRTqcKcMxbBGY0w",
}


def probe_serving(handler):
    """A real HttpxVersionProbe wired to an in-process transport."""
    return HttpxVersionProbe(transport=httpx.MockTransport(handler))


def serving(payload, status=200):
    def handler(request):
        return httpx.Response(status, json=payload)

    return probe_serving(handler)


async def test_a_real_payload_is_parsed():
    probe = serving(REAL_PAYLOAD)

    deployed = await probe.probe(URL)

    assert deployed.version == "3.120"
    assert deployed.commit == "ed0fad325977783b2ac2132aaf7c40662d15aa84"
    assert deployed.url == URL


async def test_a_payload_without_a_commit_still_reports_its_version():
    probe = serving({"version": "3.120"})

    deployed = await probe.probe(URL)

    assert deployed.version == "3.120"
    assert deployed.commit == ""


@pytest.mark.parametrize("payload", [{}, {"version": ""}, {"version": None}, {"version": 3.12}])
async def test_a_payload_without_a_usable_version_is_an_error(payload):
    probe = serving(payload)

    with pytest.raises(ProbeError, match="reported no version"):
        await probe.probe(URL)


async def test_a_json_array_is_rejected():
    probe = serving(["3.120"])

    with pytest.raises(ProbeError, match="expected an object"):
        await probe.probe(URL)


async def test_an_http_error_is_reported_not_raised_as_httpx():
    probe = serving({"error": "boom"}, status=503)

    with pytest.raises(ProbeError, match="could not read"):
        await probe.probe(URL)


async def test_a_connection_failure_is_reported():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    probe = probe_serving(handler)

    with pytest.raises(ProbeError, match="could not read"):
        await probe.probe(URL)


async def test_html_instead_of_json_is_reported():
    def handler(request):
        return httpx.Response(200, text="<!doctype html><html>404</html>")

    probe = probe_serving(handler)

    with pytest.raises(ProbeError, match="did not return JSON"):
        await probe.probe(URL)
