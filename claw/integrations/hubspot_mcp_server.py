"""HubSpot MCP server for the built-in HubSpot connector preset.

Auth is a HubSpot Private App access token (Settings -> Integrations ->
Private Apps), not OAuth — one Bearer token per connection, no token
refresh/expiry to manage, matching the GitHub/Notion/Tavily connectors.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

HUBSPOT_API_BASE_DEFAULT = "https://api.hubapi.com"
HUBSPOT_USER_AGENT = "nanobot-hubspot-connector/1.0"

# Properties returned by default for each CRM object type — HubSpot only
# returns id/createdAt/updatedAt/archived unless properties are requested
# explicitly. These are HubSpot's own out-of-the-box property names.
_DEFAULT_PROPERTIES: dict[str, tuple[str, ...]] = {
    "contacts": ("email", "firstname", "lastname", "phone", "company", "lifecyclestage"),
    "companies": ("name", "domain", "phone", "city", "industry"),
    "deals": ("dealname", "amount", "dealstage", "pipeline", "closedate"),
}


def _extract_hubspot_error_detail(response: httpx.Response) -> str:
    """HubSpot error bodies are {"message": ..., "category": ..., "errors": [...]}
    — surface that instead of a bare 'Client error 400' with no explanation."""
    try:
        data = response.json()
    except Exception:
        return response.text.strip()
    if not isinstance(data, dict):
        return response.text.strip()
    parts: list[str] = []
    message = str(data.get("message") or "").strip()
    if message:
        parts.append(message)
    errors = data.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, dict):
                sub = str(item.get("message") or "").strip()
                if sub and sub not in parts:
                    parts.append(sub)
    return "; ".join(parts) if parts else response.text.strip()


@dataclass(frozen=True)
class HubSpotClient:
    """Small HubSpot CRM v3/v4 REST API client used by the MCP server."""

    token: str
    api_base: str = HUBSPOT_API_BASE_DEFAULT
    transport: httpx.BaseTransport | None = None

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.api_base.rstrip("/"),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": HUBSPOT_USER_AGENT,
            },
            timeout=20.0,
            transport=self.transport,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> Any:
        if not self.token:
            raise ValueError("HubSpot access token is required")
        with self._client() as client:
            response = client.request(method, path, params=params, json=json_data)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = _extract_hubspot_error_detail(response)
                if detail:
                    raise httpx.HTTPStatusError(
                        f"{exc}: {detail}", request=exc.request, response=exc.response
                    ) from exc
                raise
            if not response.content:
                return {}
            return response.json()

    def get_account_info(self) -> dict[str, Any]:
        return self._request("GET", "/account-info/v3/details")

    def list_objects(
        self, object_type: str, *, limit: int = 10, after: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": int(limit),
            "properties": ",".join(_DEFAULT_PROPERTIES.get(object_type, ())),
        }
        if after:
            params["after"] = str(after)
        return self._request("GET", f"/crm/v3/objects/{object_type}", params=params)

    def get_object(self, object_type: str, object_id: str) -> dict[str, Any]:
        params = {"properties": ",".join(_DEFAULT_PROPERTIES.get(object_type, ()))}
        return self._request("GET", f"/crm/v3/objects/{object_type}/{object_id}", params=params)

    def create_object(self, object_type: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", f"/crm/v3/objects/{object_type}", json_data={"properties": properties})

    def update_object(self, object_type: str, object_id: str, properties: dict[str, Any]) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/crm/v3/objects/{object_type}/{object_id}", json_data={"properties": properties}
        )

    def search_objects(self, object_type: str, query: str = "", *, limit: int = 10) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "limit": int(limit),
            "properties": list(_DEFAULT_PROPERTIES.get(object_type, ())),
        }
        if query:
            payload["query"] = str(query)
        return self._request("POST", f"/crm/v3/objects/{object_type}/search", json_data=payload)

    def associate_default(self, from_type: str, from_id: str, to_type: str, to_id: str) -> dict[str, Any]:
        """Associate two CRM records using HubSpot's default association type
        for the pair, so the caller never needs a numeric association-type ID."""
        return self._request(
            "PUT", f"/crm/v4/objects/{from_type}/{from_id}/associations/default/{to_type}/{to_id}"
        )

    def create_note(
        self,
        body: str,
        *,
        contact_id: str | None = None,
        company_id: str | None = None,
        deal_id: str | None = None,
    ) -> dict[str, Any]:
        note = self.create_object(
            "notes", {"hs_note_body": body, "hs_timestamp": int(time.time() * 1000)}
        )
        note_id = note.get("id")
        associated_with: list[dict[str, str]] = []
        for to_type, to_id in (("contacts", contact_id), ("companies", company_id), ("deals", deal_id)):
            if to_id and note_id:
                self.associate_default("notes", note_id, to_type, to_id)
                associated_with.append({"type": to_type, "id": str(to_id)})
        note["associated_with"] = associated_with
        return note


def _client_from_env() -> HubSpotClient:
    return HubSpotClient(
        token=str(os.environ.get("HUBSPOT_TOKEN") or "").strip(),
        api_base=str(os.environ.get("HUBSPOT_API_BASE") or HUBSPOT_API_BASE_DEFAULT).strip()
        or HUBSPOT_API_BASE_DEFAULT,
    )


def _connector_context() -> dict[str, Any]:
    return {
        "api_base": str(os.environ.get("HUBSPOT_API_BASE") or HUBSPOT_API_BASE_DEFAULT).strip()
        or HUBSPOT_API_BASE_DEFAULT,
        "has_token": bool(str(os.environ.get("HUBSPOT_TOKEN") or "").strip()),
    }


mcp = FastMCP(
    "hubspot-connector",
    instructions=(
        "HubSpot CRM connector for contacts, companies, deals, and notes. "
        "Use the search_* tools to find a record by keyword before reading or updating it by ID — "
        "list_* tools return whatever HubSpot returns first, not a relevance-ranked match."
    ),
)


@mcp.tool(description="Return HubSpot account/portal details for token validation.")
def get_account_info() -> dict[str, Any]:
    return _client_from_env().get_account_info()


@mcp.tool(description="List HubSpot contacts (most recently created/updated first, per HubSpot's default order).")
def list_contacts(limit: int = 10, after: str | None = None) -> dict[str, Any]:
    return _client_from_env().list_objects("contacts", limit=limit, after=after)


@mcp.tool(description="Get a HubSpot contact by ID.")
def get_contact(contact_id: str) -> dict[str, Any]:
    return _client_from_env().get_object("contacts", contact_id)


@mcp.tool(description="Search HubSpot contacts by free-text query (matches name, email, phone, company).")
def search_contacts(query: str, limit: int = 10) -> dict[str, Any]:
    return _client_from_env().search_objects("contacts", query, limit=limit)


@mcp.tool(
    description=(
        "Create a HubSpot contact. properties keys are HubSpot internal property names, "
        "e.g. email, firstname, lastname, phone, company."
    )
)
def create_contact(properties: dict[str, Any]) -> dict[str, Any]:
    return _client_from_env().create_object("contacts", properties)


@mcp.tool(description="Update a HubSpot contact by ID. properties keys are HubSpot internal property names.")
def update_contact(contact_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    return _client_from_env().update_object("contacts", contact_id, properties)


@mcp.tool(description="List HubSpot companies.")
def list_companies(limit: int = 10, after: str | None = None) -> dict[str, Any]:
    return _client_from_env().list_objects("companies", limit=limit, after=after)


@mcp.tool(description="Get a HubSpot company by ID.")
def get_company(company_id: str) -> dict[str, Any]:
    return _client_from_env().get_object("companies", company_id)


@mcp.tool(description="Search HubSpot companies by free-text query (matches name, domain).")
def search_companies(query: str, limit: int = 10) -> dict[str, Any]:
    return _client_from_env().search_objects("companies", query, limit=limit)


@mcp.tool(
    description=(
        "Create a HubSpot company. properties keys are HubSpot internal property names, "
        "e.g. name, domain, phone, city, industry."
    )
)
def create_company(properties: dict[str, Any]) -> dict[str, Any]:
    return _client_from_env().create_object("companies", properties)


@mcp.tool(description="Update a HubSpot company by ID. properties keys are HubSpot internal property names.")
def update_company(company_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    return _client_from_env().update_object("companies", company_id, properties)


@mcp.tool(description="List HubSpot deals.")
def list_deals(limit: int = 10, after: str | None = None) -> dict[str, Any]:
    return _client_from_env().list_objects("deals", limit=limit, after=after)


@mcp.tool(description="Get a HubSpot deal by ID.")
def get_deal(deal_id: str) -> dict[str, Any]:
    return _client_from_env().get_object("deals", deal_id)


@mcp.tool(description="Search HubSpot deals by free-text query (matches deal name).")
def search_deals(query: str, limit: int = 10) -> dict[str, Any]:
    return _client_from_env().search_objects("deals", query, limit=limit)


@mcp.tool(
    description=(
        "Create a HubSpot deal. properties keys are HubSpot internal property names, "
        "e.g. dealname, amount, dealstage, pipeline, closedate."
    )
)
def create_deal(properties: dict[str, Any]) -> dict[str, Any]:
    return _client_from_env().create_object("deals", properties)


@mcp.tool(description="Update a HubSpot deal by ID. properties keys are HubSpot internal property names.")
def update_deal(deal_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    return _client_from_env().update_object("deals", deal_id, properties)


@mcp.tool(
    description=(
        "Create a HubSpot note, optionally associated with a contact, company, and/or deal by ID."
    )
)
def create_note(
    body: str,
    contact_id: str | None = None,
    company_id: str | None = None,
    deal_id: str | None = None,
) -> dict[str, Any]:
    return _client_from_env().create_note(body, contact_id=contact_id, company_id=company_id, deal_id=deal_id)


@mcp.tool(description="Return the HubSpot connector runtime context, including whether a token is configured.")
def get_connector_context() -> dict[str, Any]:
    return _connector_context()


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
