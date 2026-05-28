import os
import threading
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

SGT = ZoneInfo("Asia/Singapore")
POLL_INTERVAL = 60  # seconds
MARKET_OPEN = dtime(8, 0)
MARKET_CLOSE = dtime(3, 30)  # next calendar day


def get_active_trade_date():
    """Return (is_active, trade_date_str) for the current SGT time.

    Each weekday session runs Mon–Fri 08:00 SGT → next day 03:30 SGT.
    """
    now = datetime.now(SGT)
    weekday = now.weekday()  # 0=Mon … 6=Sun
    t = now.time()

    if t >= MARKET_OPEN:
        if weekday <= 4:  # Mon–Fri
            return True, now.date().isoformat()
    else:
        if t < MARKET_CLOSE and 1 <= weekday <= 5:  # Tue–Sat before 03:30
            trade_date = (now.date() - timedelta(days=1)).isoformat()
            return True, trade_date

    return False, None


def _fmt(value):
    return f"{value:g}"


def _format_new_trades(new_futures, new_options, trade_date):
    lines = []

    futures_agg = {}
    for t in new_futures:
        action = "bought" if t["contracts"] > 0 else "sold"
        price = round(t["trade_price"] * _price_mult(t["contract"]), 2)
        key = (t["contract"], action, price)
        futures_agg[key] = futures_agg.get(key, 0) + abs(int(t["contracts"]))

    for (contract, action, price), qty in futures_agg.items():
        lines.append(f"{action} {qty} lots {contract} at {_fmt(price)}")

    options_agg = {}
    for t in new_options:
        action = "bought" if t["contracts"] > 0 else "sold"
        price = round(t["trade_price"] * _price_mult(t["contract"]), 2)
        cp = "C" if t["call_or_put"].lower() == "call" else "P"
        key = (t["contract"], action, t["strike_price"], cp, price)
        options_agg[key] = options_agg.get(key, 0) + abs(int(t["contracts"]))

    for (contract, action, strike, cp, price), qty in options_agg.items():
        lines.append(f"{action} {qty} lots {contract} {_fmt(strike)}{cp} at {_fmt(price)} cents")

    return "\n".join(lines).strip()



class TradeListener:
    def __init__(self, bot):
        self._bot = bot
        self._chat_id = os.getenv("TELEGRAM_LISTENER_CHAT_ID")
        self._stop_event = threading.Event()
        self._thread = None
        self._seen_ids: set = set()
        self._current_trade_date: str = None

    def start(self):
        if not self._chat_id:
            print("TradeListener: TELEGRAM_LISTENER_CHAT_ID not set — listener disabled")
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="TradeListener")
        self._thread.start()
        print(f"TradeListener started, sending to chat {self._chat_id}")

    def stop(self):
        self._stop_event.set()

    def _loop(self):
        from neon import Neon
from generate_trades import SF_ACCOUNTS, _price_mult
        neon = Neon()
        neon.connect()

        while not self._stop_event.is_set():
            try:
                self._poll(neon)
            except Exception as e:
                print(f"TradeListener poll error: {e}")
            self._stop_event.wait(POLL_INTERVAL)

    def _poll(self, neon):
        is_active, trade_date = get_active_trade_date()
        if not is_active:
            return

        if trade_date != self._current_trade_date:
            self._seen_ids = set()
            self._current_trade_date = trade_date
            print(f"TradeListener: new trading day {trade_date}")

        result = neon.get_trades(trade_date)
        if not result["success"]:
            print(f"TradeListener: get_trades failed: {result.get('error')}")
            return

        futures = [t for t in result["futures"] if t["account_number"] in SF_ACCOUNTS]
        options = [t for t in result["options"]  if t["account_number"] in SF_ACCOUNTS]

        new_futures = [t for t in futures if t["unique_trade_id"] not in self._seen_ids]
        new_options = [t for t in options  if t["unique_trade_id"] not in self._seen_ids]

        if new_futures or new_options:
            msg = _format_new_trades(new_futures, new_options, trade_date)
            print(f"TradeListener: {len(new_futures)} new futures, {len(new_options)} new options")
            self._bot.send_message(self._chat_id, msg)
            for t in new_futures + new_options:
                self._seen_ids.add(t["unique_trade_id"])
