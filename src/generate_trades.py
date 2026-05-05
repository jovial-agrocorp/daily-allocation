import os
import pandas as pd
from openpyxl.styles import PatternFill, Font, Alignment, Protection
from openpyxl.utils import get_column_letter
from datetime import datetime, timedelta
from neon import Neon

SRC_DIR       = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_FILE = os.path.join(SRC_DIR, f"neon_trades_{datetime.now().strftime('%Y-%m-%d')}.xlsx")
CUTOFF_HOUR   = 8  # Before 8am, treat as previous trading day
SF_ACCOUNTS   = ["TMG30982", "TMG30985"]

FUTURES_INFO_COLS = ["Account Number", "Trade Date", "Contract", "Commodity Name", "Long", "Short", "Price"]
OPTIONS_INFO_COLS = ["Account Number", "Trade Date", "Contract", "Commodity Name", "Long", "Short", "Price", "Strike", "Put/Call"]
EDITABLE_COLS     = ["Book", "Strategy", "New AGP", "New AGP Sub Contract", "New AGS", "New AGS Sub Contract"]
FUTURES_EDITABLE  = EDITABLE_COLS + ["Split Qty"]
OPTIONS_EDITABLE  = EDITABLE_COLS


def get_trading_date():
    now = datetime.now()
    if now.hour < CUTOFF_HOUR:
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return now.strftime("%Y-%m-%d")


def _style_sheet(ws, df, info_cols, all_cols):
    gray_fill   = PatternFill(start_color="D3D3D3", end_color="D3D3D3", fill_type="solid")
    header_font = Font(bold=True)

    # Unlock all cells first (Excel locks all by default)
    for row in ws.iter_rows():
        for cell in row:
            cell.protection = Protection(locked=False)

    for col_idx, col_name in enumerate(all_cols, 1):
        col_letter = get_column_letter(col_idx)
        is_info    = col_name in info_cols

        for row_idx in range(1, len(df) + 2):
            cell = ws[f"{col_letter}{row_idx}"]
            cell.alignment = Alignment(horizontal="left")
            if is_info and row_idx > 1:  # keep header unlocked for filter dropdown
                cell.fill = gray_fill
                cell.protection = Protection(locked=True)

        ws[f"{col_letter}1"].font = header_font

        max_len = max(
            len(str(col_name)),
            max((len(str(df.iloc[r][col_name])) for r in range(len(df))
                 if col_name in df.columns and pd.notna(df.iloc[r][col_name])), default=0)
        )
        ws.column_dimensions[col_letter].width = max_len + 4

    # Enable filter dropdown on header row
    ws.auto_filter.ref = ws.dimensions

    # Protect the sheet — locked cells become read-only, unlocked cells stay editable
    ws.protection.sheet = True
    ws.protection.password = ""
    ws.protection.autoFilter = False  # allow filter interaction while sheet is protected


def generate_trades(trade_date=None, output_path=None):
    neon = Neon()
    neon.connect()
    trade_date = trade_date or get_trading_date()
    print(f"Using trade date: {trade_date}")
    result = neon.get_trades(trade_date)

    if not result["success"]:
        print(f"ERROR: Failed to fetch trades: {result['error']}")
        return

    futures = [t for t in result["futures"] if t["account_number"] in SF_ACCOUNTS]
    options = [t for t in result["options"]  if t["account_number"] in SF_ACCOUNTS]

    futures_rows = []
    for t in futures:
        futures_rows.append({
            "Account Number":       t["account_number"],
            "Trade Date":           t["trade_date"],
            "Contract":             t["contract"],
            "Commodity Name":       t["commodity_name"],
            "Long":                 t["contracts"] if t["contracts"] > 0 else None,
            "Short":                abs(t["contracts"]) if t["contracts"] < 0 else None,
            "Price":                round(t["trade_price"] * 100, 2),
            "Book":                 None,
            "Strategy":             None,
            "New AGP":              None,
            "New AGP Sub Contract": None,
            "New AGS":              None,
            "New AGS Sub Contract": None,
            "Split Qty":            None,
        })

    options_rows = []
    for t in options:
        options_rows.append({
            "Account Number":       t["account_number"],
            "Trade Date":           t["trade_date"],
            "Contract":             t["contract"],
            "Commodity Name":       t["commodity_name"],
            "Long":                 t["contracts"] if t["contracts"] > 0 else None,
            "Short":                abs(t["contracts"]) if t["contracts"] < 0 else None,
            "Price":                round(t["trade_price"] * 100, 2),
            "Strike":               t["strike_price"],
            "Put/Call":             t["call_or_put"],
            "Book":                 None,
            "Strategy":             None,
            "New AGP":              None,
            "New AGP Sub Contract": None,
            "New AGS":              None,
            "New AGS Sub Contract": None,
        })

    def _sum_or_none(s):
        return s.sum() if s.notna().any() else None

    def _aggregate(rows, group_cols, info_cols, editable_cols):
        df = pd.DataFrame(rows)
        df["_qty"] = df["Long"].fillna(0) + df["Short"].fillna(0)
        df["_wt_price"] = df["Price"] * df["_qty"]
        agg = df.groupby(group_cols, as_index=False).agg(
            Long=("Long", _sum_or_none),
            Short=("Short", _sum_or_none),
            _wt_price=("_wt_price", "sum"),
            _qty=("_qty", "sum"),
        )
        agg["Price"] = (agg["_wt_price"] / agg["_qty"]).round(2)
        agg = agg.drop(columns=["_wt_price", "_qty"])
        for col in editable_cols:
            agg[col] = None
        return agg[info_cols + editable_cols].sort_values("Contract").reset_index(drop=True)

    futures_group_cols = ["Account Number", "Trade Date", "Contract", "Commodity Name"]
    options_group_cols = ["Account Number", "Trade Date", "Contract", "Commodity Name", "Strike", "Put/Call"]

    df_futures = _aggregate(futures_rows, futures_group_cols, FUTURES_INFO_COLS, FUTURES_EDITABLE) if futures_rows else pd.DataFrame(columns=FUTURES_INFO_COLS + FUTURES_EDITABLE)
    df_options = _aggregate(options_rows, options_group_cols, OPTIONS_INFO_COLS,  OPTIONS_EDITABLE) if options_rows else pd.DataFrame(columns=OPTIONS_INFO_COLS  + OPTIONS_EDITABLE)

    save_path = output_path or TEMPLATE_FILE
    with pd.ExcelWriter(save_path, engine="openpyxl") as writer:
        df_futures.to_excel(writer, index=False, sheet_name="Futures")
        df_options.to_excel(writer, index=False, sheet_name="Options")
        _style_sheet(writer.sheets["Futures"], df_futures, FUTURES_INFO_COLS, FUTURES_INFO_COLS + FUTURES_EDITABLE)
        _style_sheet(writer.sheets["Options"], df_options, OPTIONS_INFO_COLS,  OPTIONS_INFO_COLS + OPTIONS_EDITABLE)

    print(f"{len(futures)} futures, {len(options)} options fetched for {', '.join(SF_ACCOUNTS)}")
    return save_path


if __name__ == "__main__":
    generate_trades()
