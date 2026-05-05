import os
import pandas as pd
from dotenv import load_dotenv
from simple_salesforce import Salesforce

load_dotenv()

SRC_DIR       = os.path.dirname(os.path.abspath(__file__))
SF_ACCOUNTS   = ["TMG30982", "TMG30985"]

EDITABLE_COLS    = ["Book", "Strategy", "New AGP", "New AGP Sub Contract", "New AGS", "New AGS Sub Contract"]
FUTURES_EDITABLE = EDITABLE_COLS + ["Split Qty"]
OPTIONS_EDITABLE = EDITABLE_COLS

FIELD_MAP = {
    "Book":                 "Book__c",
    "Strategy":             "Strategy__c",
    "New AGP":              "New_AGP__c",
    "New AGP Sub Contract": "New_AGP_Sub_Contract__c",
    "New AGS":              "New_AGS__c",
    "New AGS Sub Contract": "New_AGS_Sub_Contract__c",
}

MASTER_CONTRACT_FIELDS = {"New AGP", "New AGS"}
SUB_CONTRACT_FIELDS    = {"New AGP Sub Contract", "New AGS Sub Contract"}

SYSTEM_FIELDS = {
    "Id", "attributes", "CreatedDate", "CreatedById",
    "LastModifiedDate", "LastModifiedById", "SystemModstamp",
    "IsDeleted", "LastActivityDate", "LastViewedDate", "LastReferencedDate",
    "MTM__c", "Equity_MTM__c", "Conv__c"
}

sf = Salesforce(
    username=os.getenv("SF_USERNAME"),
    password=os.getenv("SF_PASSWORD"),
    security_token=os.getenv("SF_SECURITY_TOKEN"),
)


def resolve_ids(df_futures, df_options):
    master_names, sub_names = set(), set()
    for df in [df_futures, df_options]:
        for col in MASTER_CONTRACT_FIELDS:
            master_names.update(df[col].dropna().astype(str).unique())
        for col in SUB_CONTRACT_FIELDS:
            sub_names.update(df[col].dropna().astype(str).unique())

    master_map = {}
    if master_names:
        names_str = "', '".join(master_names)
        res = sf.query_all(f"SELECT Id, Name FROM Master_Contract__c WHERE Name IN ('{names_str}')")
        master_map = {r["Name"]: r["Id"] for r in res["records"]}

    sub_map = {}
    if sub_names:
        names_str = "', '".join(sub_names)
        res = sf.query_all(f"SELECT Id, Name FROM Sub_Contract__c WHERE Name IN ('{names_str}')")
        sub_map = {r["Name"]: r["Id"] for r in res["records"]}

    return master_map, sub_map


def build_payload(row, master_map, sub_map, warnings, label):
    payload = {}
    for k in FIELD_MAP:
        if hasattr(row, "get"):
            val = row.get(k)
        else:
            val = getattr(row, k.replace(" ", "_"), None)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            continue
        sf_field = FIELD_MAP[k]
        if k in MASTER_CONTRACT_FIELDS:
            resolved = master_map.get(str(val))
            if not resolved:
                warnings.append(f"{label} Could not resolve Master Contract '{val}' — skipping field {sf_field}")
                continue
            payload[sf_field] = resolved
        elif k in SUB_CONTRACT_FIELDS:
            resolved = sub_map.get(str(val))
            if not resolved:
                warnings.append(f"{label} Could not resolve Sub Contract '{val}' — skipping field {sf_field}")
                continue
            payload[sf_field] = resolved
        else:
            payload[sf_field] = val
    return payload


def update_salesforce(excel_path=None):
    if excel_path is None:
        neon_files = [f for f in os.listdir(SRC_DIR) if f.lower().startswith("neon") and f.lower().endswith(".xlsx")]
        if not neon_files:
            print("ERROR: No Excel file starting with 'neon' found in the directory.")
            return
        if len(neon_files) > 1:
            print(f"ERROR: Multiple 'neon' Excel files found: {neon_files}. Please keep only one.")
            return
        excel_path = os.path.join(SRC_DIR, neon_files[0])
    print(f"Using file: {os.path.basename(excel_path)}")
    try:
        df_futures = pd.read_excel(excel_path, sheet_name="Futures")
        df_options = pd.read_excel(excel_path, sheet_name="Options")
    except FileNotFoundError:
        print(f"ERROR: File not found: {excel_path}")
        return

    master_map, sub_map = resolve_ids(df_futures, df_options)

    # Derive trade dates from the Excel
    all_dates  = pd.concat([
        df_futures["Trade Date"].dropna().astype(str),
        df_options["Trade Date"].dropna().astype(str)
    ]).unique()
    trade_dates = [pd.Timestamp(d).strftime("%Y-%m-%d") for d in all_dates]
    dates_str   = ", ".join(trade_dates)

    accounts_str = "', '".join(SF_ACCOUNTS)
    sf_result = sf.query_all(f"""
        SELECT Id, Contract__c, Long__c, Short__c, Price__c,
               Strike__c, Put_Call_2__c, Contract_type__c, Account_No__c,
               Commodity_Name__c
        FROM Futur__c
        WHERE Account_No__c IN ('{accounts_str}')
        AND Trade_Date__c IN ({dates_str})
        AND Status__c = 'Open'
    """)
    sf_records = sf_result["records"]

    warnings = []
    updates  = 0
    splits   = 0

    def find_sf_match(account, contract, long_qty, short_qty, price, contract_type="Futures"):
        is_long = long_qty is not None
        qty     = long_qty if is_long else short_qty
        matches = [
            r for r in sf_records
            if r["Account_No__c"] == account
            and r["Contract__c"] == contract
            and r["Contract_type__c"] == contract_type
            and abs((r["Price__c"] or 0) - price) < 0.0001
            and (
                (is_long     and (r["Long__c"]  or 0) == qty)
                or
                (not is_long and abs(r["Short__c"] or 0) == qty)
            )
        ]
        return matches, is_long, qty

    # --- Futures ---
    filled_futures = df_futures[df_futures[FUTURES_EDITABLE].notna().any(axis=1)].copy()
    split_mask     = filled_futures["Split Qty"].notna()

    # Aggregate non-split rows: sum Long/Short per Trade Date + Account + Contract + Price
    agg_spec = {"Long": lambda x: x.sum(min_count=1), "Short": lambda x: x.sum(min_count=1)}
    agg_spec.update({col: "first" for col in EDITABLE_COLS})
    nonsplit_agg = (
        filled_futures[~split_mask]
        .groupby(["Trade Date", "Account Number", "Contract", "Price"], dropna=False)
        .agg(agg_spec)
        .reset_index()
    )

    for _, row in nonsplit_agg.iterrows():
        account   = row["Account Number"]
        contract  = row["Contract"]
        long_qty  = None if pd.isna(row["Long"])  else row["Long"]
        short_qty = None if pd.isna(row["Short"]) else row["Short"]
        price     = row["Price"]

        matches, is_long, qty = find_sf_match(account, contract, long_qty, short_qty, price)

        if len(matches) == 0:
            warnings.append(f"[Futures] No match: {contract}  long={long_qty}  short={short_qty}  price={price}")
            continue
        if len(matches) > 1:
            warnings.append(f"[Futures] Ambiguous match: {contract}  long={long_qty}  short={short_qty}  price={price} — skipping")
            continue

        record_id = matches[0]["Id"]
        payload   = build_payload(row, master_map, sub_map, warnings, f"[Futures] {contract}")
        try:
            sf.Futur__c.update(record_id, payload)
            updates += 1
        except Exception as e:
            warnings.append(f"[Futures] Update failed for {contract} ({record_id}): {e}")

    # Process split rows — Long/Short here is the SF record's total qty, not summed
    for key, group in filled_futures[split_mask].groupby(["Account Number", "Contract", "Long", "Short", "Price"], dropna=False):
        account, contract, long_qty, short_qty, price = key
        long_qty  = None if pd.isna(long_qty)  else long_qty
        short_qty = None if pd.isna(short_qty) else short_qty

        matches, is_long, qty = find_sf_match(account, contract, long_qty, short_qty, price)

        if len(matches) == 0:
            warnings.append(f"[Futures] No match: {contract}  long={long_qty}  short={short_qty}  price={price}")
            continue
        if len(matches) > 1:
            warnings.append(f"[Futures] Ambiguous match: {contract}  long={long_qty}  short={short_qty}  price={price} — skipping")
            continue

        record_id = matches[0]["Id"]

        if group["Split Qty"].isna().any():
            warnings.append(f"[Futures] Split error for {contract}: some rows missing Split Qty — skipping")
            continue

        split_qtys = group["Split Qty"].astype(int).tolist()
        if sum(split_qtys) != qty:
            warnings.append(f"[Futures] Split qty mismatch for {contract}: {split_qtys} sum to {sum(split_qtys)}, expected {qty}")
            continue

        try:
            full_record = sf.Futur__c.get(record_id)
            base_clone  = {k: v for k, v in full_record.items() if k not in SYSTEM_FIELDS and v is not None}

            first_row     = group.iloc[0]
            first_payload = build_payload(first_row, master_map, sub_map, warnings, f"[Futures split] {contract}")
            first_payload["Long__c" if is_long else "Short__c"] = split_qtys[0] if is_long else -split_qtys[0]
            sf.Futur__c.update(record_id, first_payload)

            for i, row in enumerate(group.iloc[1:].itertuples(), 1):
                clone_payload = dict(base_clone)
                clone_payload.update(build_payload(row, master_map, sub_map, warnings, f"[Futures split clone] {contract}"))
                clone_payload["Long__c" if is_long else "Short__c"] = split_qtys[i] if is_long else -split_qtys[i]
                clone_payload["Short__c" if is_long else "Long__c"] = None
                sf.Futur__c.create(clone_payload)

            splits += 1
        except Exception as e:
            warnings.append(f"[Futures] Split failed for {contract} ({record_id}): {e}")

    # --- Options ---
    filled_options = df_options[df_options[OPTIONS_EDITABLE].notna().any(axis=1)].copy()

    # Aggregate by Trade Date, Account, Contract, Price, Strike, Put/Call
    agg_spec_opt = {"Long": lambda x: x.sum(min_count=1), "Short": lambda x: x.sum(min_count=1)}
    agg_spec_opt.update({col: "first" for col in OPTIONS_EDITABLE})
    filled_options = (
        filled_options
        .groupby(["Trade Date", "Account Number", "Contract", "Price", "Strike", "Put/Call"], dropna=False)
        .agg(agg_spec_opt)
        .reset_index()
    )

    for _, row in filled_options.iterrows():
        contract  = row["Contract"]
        long_qty  = row["Long"]  if pd.notna(row["Long"])  else None
        short_qty = row["Short"] if pd.notna(row["Short"]) else None
        price     = row["Price"]
        strike    = row["Strike"]
        put_call  = row["Put/Call"]
        is_long   = long_qty is not None
        qty       = long_qty if is_long else short_qty
        account   = row["Account Number"]

        matches = [
            r for r in sf_records
            if r["Account_No__c"] == account
            and r["Contract__c"] == contract
            and r["Contract_type__c"] == "Option"
            and abs((r["Price__c"]  or 0) - price)  < 0.0001
            and abs((r["Strike__c"] or 0) - strike) < 0.0001
            and (r["Put_Call_2__c"] or "").upper() == str(put_call).upper()
            and (
                (is_long     and (r["Long__c"]  or 0) == qty)
                or
                (not is_long and abs(r["Short__c"] or 0) == qty)
            )
        ]

        if len(matches) == 0:
            warnings.append(f"[Options] No match: {contract}  long={long_qty}  short={short_qty}  price={price}  strike={strike}  {put_call}")
            continue
        if len(matches) > 1:
            warnings.append(f"[Options] Ambiguous match: {contract}  long={long_qty}  short={short_qty}  price={price}  strike={strike}  {put_call} — skipping")
            continue

        record_id = matches[0]["Id"]
        payload   = build_payload(row, master_map, sub_map, warnings, f"[Options] {contract}")
        try:
            sf.Futur__c.update(record_id, payload)
            updates += 1
        except Exception as e:
            warnings.append(f"[Options] Update failed for {contract} ({record_id}): {e}")

    print(f"\nDone -- {updates} updated, {splits} split")
    if warnings:
        print(f"\nWarnings ({len(warnings)}):")
        for w in warnings:
            print(f"  - {w}")


if __name__ == "__main__":
    update_salesforce()
