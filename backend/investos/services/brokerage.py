from __future__ import annotations

import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.investments_holdings_get_request import InvestmentsHoldingsGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.products import Products

from investos.services.runtime_settings import RuntimeSettingsStore


class PlaidServiceError(Exception):
    pass


class BrokerageService:
    @classmethod
    def _get_client(cls) -> plaid_api.PlaidApi:
        runtime = RuntimeSettingsStore.load().plaid
        client_id = runtime.client_id
        secret = runtime.secret
        if not client_id or not secret:
            raise PlaidServiceError("Plaid credentials are not configured in settings.")
        env = runtime.environment.lower()
        if env == "production":
            host = plaid.Environment.Production
        elif env == "development":
            host = plaid.Environment.Development
        else:
            host = plaid.Environment.Sandbox

        configuration = plaid.Configuration(
            host=host,
            api_key={
                "clientId": client_id,
                "secret": secret,
            },
        )
        api_client = plaid.ApiClient(configuration)
        return plaid_api.PlaidApi(api_client)

    @classmethod
    def create_link_token(cls) -> str:
        client = cls._get_client()
        request = LinkTokenCreateRequest(
            products=[Products("investments")],
            client_name="Prophet",
            country_codes=[CountryCode("US")],
            language="en",
            user=LinkTokenCreateRequestUser(client_user_id="investos_local_user"),
        )
        try:
            response = client.link_token_create(request)
            return response["link_token"]
        except plaid.ApiException as e:
            raise PlaidServiceError(
                f"Failed to create Plaid link token: {e.reason}"
            ) from e

    @classmethod
    def exchange_public_token(cls, public_token: str) -> dict:
        client = cls._get_client()
        request = ItemPublicTokenExchangeRequest(public_token=public_token)
        try:
            response = client.item_public_token_exchange(request)
            access_token = response["access_token"]
            item_id = response["item_id"]

            # Save the tokens to runtime settings
            runtime = RuntimeSettingsStore.load()
            runtime.plaid.enabled = True
            runtime.plaid.access_token = access_token
            runtime.plaid.item_id = item_id
            RuntimeSettingsStore.save(runtime)

            return {"ok": True, "item_id": item_id}
        except plaid.ApiException as e:
            raise PlaidServiceError(
                f"Failed to exchange public token: {e.reason}"
            ) from e

    @classmethod
    def fetch_holdings_snapshot(cls) -> dict:
        runtime = RuntimeSettingsStore.load().plaid
        if not runtime.enabled:
            raise PlaidServiceError("Plaid broker sync is disabled.")
        if not runtime.access_token:
            raise PlaidServiceError(
                "Connect a brokerage account with Plaid Link first."
            )

        client = cls._get_client()
        try:
            response = client.investments_holdings_get(
                InvestmentsHoldingsGetRequest(access_token=runtime.access_token)
            )
        except plaid.ApiException as exc:
            raise PlaidServiceError(
                f"Failed to fetch Plaid investment holdings: {exc.reason}"
            ) from exc

        payload = response.to_dict() if hasattr(response, "to_dict") else dict(response)
        return cls._normalize_holdings_response(payload, item_id=runtime.item_id)

    @staticmethod
    def _normalize_holdings_response(
        payload: dict, *, item_id: str | None = None
    ) -> dict:
        securities = {
            str(item.get("security_id")): item
            for item in payload.get("securities", [])
            if item.get("security_id")
        }
        quantities: dict[str, float] = {}
        cash = 0.0
        ignored: list[dict] = []
        for holding in payload.get("holdings", []):
            security = securities.get(str(holding.get("security_id"))) or {}
            if security.get("is_cash_equivalent"):
                cash += float(holding.get("institution_value") or 0.0)
                continue
            ticker = str(security.get("ticker_symbol") or "").strip().upper()
            quantity = holding.get("quantity")
            if not ticker or quantity is None:
                ignored.append(
                    {
                        "security_id": holding.get("security_id"),
                        "name": security.get("name"),
                        "reason": "missing_ticker_or_quantity",
                    }
                )
                continue
            quantities[ticker] = quantities.get(ticker, 0.0) + float(quantity)

        return {
            "holdings": [
                {"ticker": ticker, "quantity": quantity}
                for ticker, quantity in sorted(quantities.items())
            ],
            "cash": cash if cash else None,
            "account_count": len(payload.get("accounts", [])),
            "ignored": ignored,
            "item_id": item_id,
        }
