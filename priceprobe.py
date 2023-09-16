#!/usr/bin/env python3

import prometheus_client

import logging
import time
import typing
import datetime
import requests
import dateutil.parser
import time
import json
import dbm

Database: typing.TypeAlias = "dbm._Database"


class Price(typing.TypedDict):
    value: float
    timestamp: datetime.datetime


class Metrics(typing.TypedDict):
    spotprice: prometheus_client.metrics.Gauge
    spotprice_sek: prometheus_client.metrics.Gauge
    total_cost: prometheus_client.metrics.Gauge
    sellprice: prometheus_client.metrics.Gauge
    selltotal: prometheus_client.metrics.Gauge


logger = logging.getLogger()


class Meters:
    def get_rate(self, force: bool = False) -> float:
        today = time.strftime("%Y-%m-%d")
        key = f"rates{time.strftime('%Y%m%d')}"
        db = self.database
        if key in db and not force:
            return float(db[key])

        logger.debug("Fetching rates")
        r = requests.get(f"https://api-eu.exchangerate.host/latest?symbols=SEK")
        if r.status_code != 200:
            raise SystemError("could not fetch electricity info")

        rates = r.json()
        if (
            not rates["success"]
            or rates["date"] != today
            or not "SEK" in rates["rates"]
        ):
            raise SystemError("no suitable rate")

        db[key] = str(rates["rates"]["SEK"])

        return float(db[key])

    def get_prices(self, force: bool = False) -> list[Price]:
        db = self.database
        key = f"prices{time.strftime('%Y%m%d')}"
        if key in db and not force:
            data = db[key]
        else:
            logger.debug("Fetching spot prices")
            r = requests.get(f"https://spot.utilitarian.io/electricity/SE3/latest")
            if r.status_code != 200:
                raise SystemError("could not fetch electricity info")

            db[key] = r.text
            data = r.text.encode("ascii")

        def fix_entry(x: typing.Dict[str, str]) -> Price:
            r = Price(
                value=float(x["value"]),
                timestamp=dateutil.parser.parse(x["timestamp"]).astimezone(),
            )
            return r

        def price_apply(x: Price) -> bool:
            today = datetime.datetime.now()
            if x["timestamp"].day == today.day:
                return True
            return False

        fixed = list(map(fix_entry, json.loads(data)))
        filtered = list(filter(price_apply, fixed))

        if not force and not len(filtered):
            # No entries, try with force if this isn't forced
            return self.get_prices(True)

        return filtered

    metrics: Metrics = Metrics(
        spotprice=prometheus_client.metrics.Gauge("spotprice", "Spotprice in EUR/Mwh"),
        spotprice_sek=prometheus_client.metrics.Gauge(
            "spotprice_sek_kwh", "Spotprice in SEK/kwh per ECB course"
        ),
        total_cost=prometheus_client.metrics.Gauge(
            "total_cost", "Total cost of power SEK/kwh per ECB course"
        ),
        sellprice=prometheus_client.metrics.Gauge("sellprice", "Sellprice in SEK/kwh"),
        selltotal=prometheus_client.metrics.Gauge(
            "selltotal", "Total effect of selling in SEK/kwh"
        ),
    )

    def __init__(self):
        self.database = dbm.open("priceprobe.db", "c")

    def refresh_all_meters(self):
        t = time.localtime().tm_hour

        prices = self.get_prices()
        rate = self.get_rate()

        for p in prices:
            if t == p["timestamp"].hour:
                self.metrics["spotprice"].set(p["value"])
                self.metrics["spotprice_sek"].set(p["value"] * rate / 1000)

                # Sälj: spot+10-2,32 öre från telge
                # 20,4 Vattenfall
                # 60 öre skatt
                self.metrics["sellprice"].set(
                    0.2040 - 0.0232 + p["value"] * rate / 1000 + 0.10
                )
                self.metrics["selltotal"].set(
                    0.2040 - 0.0232 + p["value"] * rate / 1000 + 0.10 + 0.6
                )

                # Köp: Energiskatt+elöverföring (Vattenfall) 39,2+24,4
                # Påslag Telge 5 öre/kwh
                # Moms
                other_charges = 0.244 + 0.3920
                self.metrics["total_cost"].set(
                    (p["value"] * rate / 1000 + other_charges) * 1.25
                )


def serve():
    m = Meters()
    prometheus_client.start_http_server(8016)

    while True:
        m.refresh_all_meters()
        time.sleep(10)


if __name__ == "__main__":
    serve()
