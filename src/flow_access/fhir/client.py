import json
import time
from collections.abc import Iterator
from typing import Any, Protocol

import httpx

Resource = dict[str, Any]


class TokenProvider(Protocol):
    def access_token(self) -> str: ...
    def invalidate(self) -> None: ...


class FhirError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class FhirClient:
    """Minimal FHIR R4 client for Epic: reads, paged searches, and Bulk Data $export."""

    def __init__(self, base_url: str, auth: TokenProvider | None, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self._http = http or httpx.Client(timeout=60)

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {"Accept": "application/fhir+json"}
        if self.auth:
            headers["Authorization"] = f"Bearer {self.auth.access_token()}"
        headers.update(extra or {})
        return headers

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        if not url.startswith("http"):
            url = f"{self.base_url}/{url.lstrip('/')}"
        extra_headers = kwargs.pop("headers", None)
        resp = self._http.request(method, url, headers=self._headers(extra_headers), **kwargs)
        if resp.status_code == 401 and self.auth:
            self.auth.invalidate()
            resp = self._http.request(method, url, headers=self._headers(extra_headers), **kwargs)
        return resp

    def read(self, resource_type: str, resource_id: str) -> Resource:
        resp = self._request("GET", f"{resource_type}/{resource_id}")
        if resp.status_code != 200:
            raise FhirError(f"GET {resource_type}/{resource_id} returned HTTP {resp.status_code}", resp.status_code)
        return resp.json()

    def search(self, resource_type: str, params: dict[str, str]) -> Iterator[Resource]:
        """Yield matching resources across all pages, skipping OperationOutcome entries Epic adds."""
        url: str | None = resource_type
        query: dict[str, str] | None = params
        while url:
            resp = self._request("GET", url, params=query)
            if resp.status_code != 200:
                raise FhirError(f"Search {resource_type} returned HTTP {resp.status_code}", resp.status_code)
            bundle = resp.json()
            for entry in bundle.get("entry", []):
                resource = entry.get("resource", {})
                if resource.get("resourceType") == resource_type:
                    yield resource
            url = next((link["url"] for link in bundle.get("link", []) if link.get("relation") == "next"), None)
            query = None

    def create(self, resource: Resource) -> Resource:
        resp = self._request(
            "POST", resource["resourceType"], json=resource, headers={"Content-Type": "application/fhir+json"}
        )
        if resp.status_code not in (200, 201):
            raise FhirError(f"Create {resource['resourceType']} returned HTTP {resp.status_code}", resp.status_code)
        return resp.json() if resp.content else resource

    def bulk_export(
        self,
        group_id: str,
        types: list[str],
        since: str | None = None,
        poll_interval: float = 10.0,
        timeout: float = 3600.0,
        sleep=time.sleep,
    ) -> dict:
        """Run a Group-level Bulk Data $export to completion and return the manifest."""
        params = {"_type": ",".join(types)}
        if since:
            params["_since"] = since
        kickoff = self._request("GET", f"Group/{group_id}/$export", params=params, headers={"Prefer": "respond-async"})
        if kickoff.status_code != 202:
            raise FhirError(f"$export kickoff returned HTTP {kickoff.status_code}", kickoff.status_code)
        status_url = kickoff.headers["Content-Location"]

        deadline = time.monotonic() + timeout
        while True:
            status = self._request("GET", status_url)
            if status.status_code == 200:
                return status.json()
            if status.status_code != 202:
                raise FhirError(f"$export status returned HTTP {status.status_code}", status.status_code)
            if time.monotonic() > deadline:
                raise FhirError("$export did not complete before timeout")
            retry_after = status.headers.get("Retry-After", "")
            sleep(float(retry_after) if retry_after.isdigit() else poll_interval)

    def iter_bulk_output(self, manifest: dict) -> Iterator[Resource]:
        for output in manifest.get("output", []):
            resp = self._request("GET", output["url"], headers={"Accept": "application/fhir+ndjson"})
            if resp.status_code != 200:
                raise FhirError(f"$export file download returned HTTP {resp.status_code}", resp.status_code)
            for line in resp.text.splitlines():
                if line.strip():
                    yield json.loads(line)
