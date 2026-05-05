import os
import requests
import pandas as pd
import time
from datetime import datetime, date
from datetime import time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
import pandas_market_calendars as mcal

load_dotenv()

class Neon:
    def __init__(self):
        self.client_id = os.getenv("NEON_CLIENT_ID")
        self.client_secret = os.getenv("NEON_CLIENT_SECRET")
        self.token = None
        self.token_expires_at = 0
        self.account_lst = []
        self.neon_sf_mapping = {
            "COFFEE C": "KC",
            "COTTON": "CT",
            "NO.5 WHITE SUGAR": "SW",
            "SUGAR NO.11": "SB",
            "CORN": "C",
            "WHEAT": "W",
            "EURO FX": "6E",
            "KC WHEAT": "KE",
            "MILLING WHEAT": "CA",
            "SOYBEAN MEAL": "ZM",
            "SOYBEAN OIL": "ZL",
            "SOYBEANS": "S",
            "CANOLA": "RS",
            "HRS WHEAT": "MW",
            "SOUTH AMERICAN SOYBEANS": "SAS",
            "EUA FUTURES": "ECP",
            "GOLD": "GC",
            "ROBUSTA COFFEE (10)": "RC",
            "EMINI NSDQ": "NQ",
            "EMINI S&P 500": "ES"
        }

    def neon_to_sf_account(self, neon_account):
        if neon_account.startswith("111"):
            return "TMG" + neon_account[3:]
        return neon_account

    def token_is_valid(self):
        return self.token and time.time() < self.token_expires_at

    def get_token(self):
        url = "https://login.neon.markets/oauth/token"
        headers = {"Content-Type": "application/json"}
        body = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "audience": "https://app.neon.markets/api",
            "grant_type": "client_credentials"
        }
        response = requests.post(url=url, headers=headers, json=body)
        try:
            response.raise_for_status()
            data = response.json()
            self.token = data["access_token"]
            self.token_expires_at = time.time() + data["expires_in"]
            return {"success": True, "token": self.token}
        except requests.HTTPError:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text
        return {"success": False, "status": response.status_code, "error": error_data}

    def get_accounts(self):
        if not self.token_is_valid():
            self.get_token()
        url = "https://neonapi.neon.markets/rest/portfolio/v1/accounts"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        response = requests.get(url=url, headers=headers)
        try:
            response.raise_for_status()
            data = response.json()
            self.account_lst = [account["accountNumber"]["value"] for account in data["accounts"]]
            return {"success": True, "account_lst": self.account_lst}
        except requests.HTTPError:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text
        return {"success": False, "status": response.status_code, "error": error_data}

    def connect(self):
        self.get_token()
        self.get_accounts()

    def get_us_trading_day(self):
        et = ZoneInfo("America/New_York")
        now = datetime.now(et)
        nyse = mcal.get_calendar("NYSE")
        market_open = dtime(9, 30)
        if now.time() < market_open:
            reference_date = now.date() - pd.Timedelta(days=1)
        else:
            reference_date = now.date()
        schedule = nyse.valid_days(
            start_date=reference_date - pd.Timedelta(days=7),
            end_date=reference_date
        )
        return schedule[-1].date().isoformat()

    def get_trades(self, trade_date=None):
        if not self.token_is_valid():
            self.get_token()
        if not self.account_lst:
            self.get_accounts()

        trading_day = trade_date or date.today().strftime("%Y-%m-%d")
        print(f"Fetching trades for {trading_day}")

        url = "https://neonapi.neon.markets/rest/portfolio/v1/trades"
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {self.token}"}
        params = {"accounts": ",".join(self.account_lst), "date": trading_day}

        response = requests.get(url=url, headers=headers, params=params)
        try:
            response.raise_for_status()
            data = response.json()
            raw_futures, raw_options = [], []
            for trade in data["trades"]:
                taxonomy = trade["product"]["taxonomy"][0]["productQualifier"]
                if taxonomy == "ExchangeTradedOption":
                    raw_options.append(trade)
                else:
                    raw_futures.append(trade)

            futures_trades = [t for t in [self.preprocess_futures_trade(t, trading_day) for t in raw_futures] if t]
            options_trades = [t for t in [self.preprocess_options_trade(t, trading_day) for t in raw_options] if t]

            return {"success": True, "futures": futures_trades, "options": options_trades}
        except requests.HTTPError:
            try:
                error_data = response.json()
            except ValueError:
                error_data = response.text
        return {"success": False, "status": response.status_code, "error": error_data}

    def preprocess_futures_trade(self, trade, trading_day):
        futures_month_codes = {
            "Jan": "F", "Feb": "G", "Mar": "H", "Apr": "J",
            "May": "K", "Jun": "M", "Jul": "N", "Aug": "Q",
            "Sep": "U", "Oct": "V", "Nov": "X", "Dec": "Z"
        }
        try:
            unique_trade_id = trade["tradeIdentifier"][0]["assignedIdentifier"][0]["identifier"]["value"]

            # Extract account from trade ID prefix and map to SF account format
            neon_account = unique_trade_id.split("-")[0]
            sf_account = self.neon_to_sf_account(neon_account)

            quantity = trade["tradeLot"][0]["priceQuantity"][0]["quantity"][0]["value"]["value"]

            contract_identifier = trade["product"]["economicTerms"]["payout"][0]["SettlementPayout"]["underlier"]["Product"]\
                ["TransferableProduct"]["Instrument"]["ListedDerivative"]["identifier"][2]["identifier"]["value"]
            contract_code = self.neon_sf_mapping.get(contract_identifier, contract_identifier)

            contract_month = trade["product"]["economicTerms"]["payout"][0]["SettlementPayout"]["deliveryTerm"]
            contract = f"{contract_code}{futures_month_codes[contract_month[:3]]}{contract_month[-2:]}"

            price = trade["tradeLot"][0]["priceQuantity"][0]["price"][0]["value"]["value"]

            return {
                "unique_trade_id": unique_trade_id,
                "trade_date": trading_day,
                "contract": contract,
                "commodity_name": contract_identifier,
                "contracts": quantity,
                "trade_price": price,
                "account_number": sf_account,
            }
        except Exception as e:
            print(f"Warning: failed to preprocess futures trade: {e}")
            return None

    def preprocess_options_trade(self, trade, trading_day):
        futures_month_codes = {
            "01": "F", "02": "G", "03": "H", "04": "J",
            "05": "K", "06": "M", "07": "N", "08": "Q",
            "09": "U", "10": "V", "11": "X", "12": "Z"
        }
        try:
            unique_trade_id = trade["tradeIdentifier"][0]["assignedIdentifier"][0]["identifier"]["value"]
            neon_account = trade["account"][0]["accountNumber"]["value"]
            sf_account = self.neon_to_sf_account(neon_account)

            payout = trade["product"]["economicTerms"]["payout"][0]["OptionPayout"]
            call_or_put = payout["optionType"]
            strike_price = payout["strike"]["strikePrice"]["value"]
            option_expiry = payout["exerciseTerms"]["expirationDate"][0]["adjustableDate"]["unadjustedDate"]
            month = option_expiry.split("-")[1]
            year = option_expiry.split("-")[0][-2:]

            contract_identifier = payout["underlier"]["Product"]["TransferableProduct"]\
                ["Instrument"]["ListedDerivative"]["identifier"][2]["identifier"]["value"]
            commodity_code = self.neon_sf_mapping.get(contract_identifier, contract_identifier)
            contract = f"{commodity_code}{futures_month_codes[month]}{year}"

            quantity = trade["tradeLot"][0]["priceQuantity"][0]["quantity"][0]["value"]["value"]
            price = trade["tradeLot"][0]["priceQuantity"][0]["price"][0]["value"]["value"]

            return {
                "unique_trade_id": unique_trade_id,
                "trade_date": trading_day,
                "contract": contract,
                "commodity_name": contract_identifier,
                "call_or_put": call_or_put,
                "strike_price": strike_price,
                "contracts": quantity,
                "trade_price": price,
                "account_number": sf_account,
            }
        except Exception as e:
            print(f"Warning: failed to preprocess options trade: {e}")
            return None
