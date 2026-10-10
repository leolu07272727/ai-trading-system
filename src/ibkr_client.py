"""IBKR paper-trading client wrapping ib_async's synchronous API."""

from __future__ import annotations

import os
import time
from zoneinfo import ZoneInfo

import pandas as pd
from ib_async import IB, LimitOrder, Stock, StopOrder, Trade

_US_EASTERN = ZoneInfo("America/New_York")

_RECONNECT_RETRIES = 3
_RECONNECT_WAIT_SECONDS = 5
_ORDER_POLL_ATTEMPTS = 10
_ACTIVE_STATUSES = ("Filled", "PreSubmitted", "Submitted")


class IBKRClient:
    """Thin wrapper over ib_async.IB for short-lived paper-trading scripts."""

    def __init__(self, host: str = "127.0.0.1", port: int = 4002, client_id: int = 1) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        # 1=live, 2=frozen, 3=delayed, 4=delayed-frozen. Demo/unsubscribed accounts
        # only have delayed data, so default to 3; set IBKR_MARKET_DATA_TYPE=1 once a
        # live market-data subscription is active.
        self.market_data_type = int(os.environ.get("IBKR_MARKET_DATA_TYPE", "3"))
        self.ib = IB()
        self._connect_with_retry()

    def _connect_with_retry(self) -> None:
        last_error: Exception | None = None
        for attempt in range(1, _RECONNECT_RETRIES + 1):
            try:
                self.ib.connect(self.host, self.port, clientId=self.client_id)
                self.ib.reqMarketDataType(self.market_data_type)
                return
            except Exception as exc:  # noqa: BLE001 - retry on any connect failure
                last_error = exc
                if attempt < _RECONNECT_RETRIES:
                    time.sleep(_RECONNECT_WAIT_SECONDS)
        raise ConnectionError(
            f"Failed to connect to IBKR at {self.host}:{self.port} "
            f"after {_RECONNECT_RETRIES} attempts"
        ) from last_error

    def _qualify_stock(self, symbol: str) -> Stock:
        contract = Stock(symbol, "SMART", "USD")
        self.ib.qualifyContracts(contract)
        return contract

    def get_snapshot_price(self, symbol: str, attempts: int = 2) -> float:
        """Return a real-time snapshot price (last, falling back to close).

        Delayed/demo market data occasionally returns nothing on the first
        snapshot, so the whole request is retried before giving up. A successful
        snapshot self-terminates server-side, so we only cancel on timeout — this
        avoids the benign "Error 300: Can't find EId" noise that cancelling an
        already-finished snapshot would emit.
        """
        contract = self._qualify_stock(symbol)
        price = float("nan")
        for _ in range(max(1, attempts)):
            ticker = self.ib.reqMktData(contract, snapshot=True)
            for _ in range(_ORDER_POLL_ATTEMPTS):
                self.ib.sleep(1)
                market = ticker.marketPrice()
                if market is not None and market == market and market > 0:
                    price = float(market)
                    break
                for candidate in (ticker.last, ticker.close):
                    if candidate is not None and candidate == candidate and candidate > 0:
                        price = float(candidate)
                        break
                if price == price:
                    break
            if price == price:
                break
            # Only reached on timeout: the snapshot never delivered, so cancel
            # the still-open request before retrying.
            try:
                self.ib.cancelMktData(contract)
            except Exception:  # noqa: BLE001 - cancel is best-effort cleanup
                pass
        if price != price:
            raise ValueError(f"No snapshot price available for {symbol}")
        return price

    def get_5m_bars(self, symbol: str, lookback: int) -> pd.DataFrame:
        """Return 5-minute bars as a DataFrame: date, open, high, low, close, volume."""
        contract = self._qualify_stock(symbol)
        duration_days = max(1, (lookback * 5) // (60 * 7) + 1)
        duration_str = f"{duration_days} D"
        bars = self.ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration_str,
            barSizeSetting="5 mins",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
        )
        rows = []
        for bar in bars:
            rows.append(
                {
                    "date": pd.Timestamp(bar.date),
                    "open": float(bar.open),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                }
            )
        df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
        if lookback > 0 and len(df) > lookback:
            df = df.tail(lookback).reset_index(drop=True)
        return df

    def place_limit_order(
        self, symbol: str, side: str, quantity: int, limit_price: float
    ) -> Trade:
        """§Fix2 - place a limit order (outsideRth) and wait for it to go active."""
        contract = self._qualify_stock(symbol)
        order = LimitOrder(
            side.upper(),
            int(quantity),
            float(limit_price),
            outsideRth=True,
            tif="DAY",
        )
        trade = self.ib.placeOrder(contract, order)
        for _ in range(_ORDER_POLL_ATTEMPTS):
            self.ib.sleep(1)
            if trade.orderStatus.status in _ACTIVE_STATUSES:
                break
        return trade

    def place_stop_order(
        self, symbol: str, side: str, quantity: int, stop_price: float
    ) -> Trade:
        """Place a protective stop order; Trade.order.orderId is accessible."""
        contract = self._qualify_stock(symbol)
        # No outsideRth: a stop order rejects that attribute (Warning 2109) and the
        # order ends in ValidationError, leaving the position unprotected.
        order = StopOrder(
            side.upper(),
            int(quantity),
            float(stop_price),
            tif="GTC",
        )
        trade = self.ib.placeOrder(contract, order)
        # Wait for the broker to assign a global permId. orderId is per-connection
        # and collides across clients; stop-out detection matches on permId, so it
        # must be populated before we return.
        for _ in range(_ORDER_POLL_ATTEMPTS):
            self.ib.sleep(1)
            if trade.order.permId:
                break
        return trade

    # (place_backup_close_order removed 2026-07: a SELL DAY limit at 98% of the
    # current price is a marketable order that fills IMMEDIATELY, flattening the
    # position seconds after entry and orphaning the GTC protective stop. EOD
    # flattening already has two independent layers — cycle.py force_close_all
    # and force_close.py — so this "third net" was pure downside.)

    def disconnect(self) -> None:
        """Cleanly disconnect from IBKR."""
        if self.ib.isConnected():
            self.ib.disconnect()
