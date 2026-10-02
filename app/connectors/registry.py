import httpx

from app.connectors.ashby import AshbyConnector
from app.connectors.base import ConnectorError, JobConnector
from app.connectors.greenhouse import GreenhouseConnector
from app.connectors.jsonld import JsonLdConnector
from app.connectors.lever import LeverConnector
from app.connectors.workable import WorkableConnector


def build_connector(
    source_type: str,
    client: httpx.AsyncClient | None = None,
    company_name: str | None = None,
) -> JobConnector:
    if source_type == "greenhouse":
        return GreenhouseConnector(client)
    if source_type == "lever":
        return LeverConnector(client, company_name=company_name)
    if source_type == "ashby":
        return AshbyConnector(client, company_name=company_name)
    if source_type == "workable":
        return WorkableConnector(client, company_name=company_name)
    if source_type == "generic-jsonld":
        return JsonLdConnector(client, company_name=company_name)
    raise ConnectorError(f"No connector for source type {source_type}")
