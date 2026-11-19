import pandas as pd
from io import BytesIO

def safe_parse_date(value):
    try:
        dt = pd.to_datetime(value, errors="coerce")
        if pd.isna(dt):
            return None
        if dt.year < 1970:
            return None
        return dt
    except:
        return None

async def parse_excel(file_bytes):

    excel_file = pd.ExcelFile(BytesIO(file_bytes))
    sheets_data = {}
    for sheet in excel_file.sheet_names:
        df = excel_file.parse(sheet, header=None)
        header_row = None

        # SEARCH FOR HEADER ROW (FIRST 10 ROWS)
        search_limit = min(10, len(df))

        for i in range(search_limit):
            row = [str(x).lower() for x in df.iloc[i].values]

            if any("date" in x for x in row) or any("memo" in x for x in row) or any("description" in x for x in row):
                header_row = i
                print("Header row detected at:", i)
                break

        # HEADER-BASED STRUCTURE
        if header_row is not None:

            df.columns = df.iloc[header_row]
            df = df[header_row + 1:]

            df.columns = (
                df.columns.astype(str)
                .str.strip()
                .str.lower()
                .str.replace(r"\s+", " ", regex=True)
            )
            column_map = {}

            for col in df.columns:
                col_clean = str(col).lower().strip()
                if "date" in col_clean:
                    column_map[col] = "date"
                elif "staff" in col_clean  or "initial" in col_clean:
                    column_map[col] = "staff"
                elif "memo" in col_clean or "task" in col_clean or "description" in col_clean or "work" in col_clean:
                    column_map[col] = "task"
                elif "hour" in col_clean or "time" in col_clean or "period" in col_clean or "billable" in col_clean:
                    column_map[col] = "hours"
            df = df.rename(columns=column_map)

        # NO HEADER, USE STATIC STRUCTURE
        else:
            df = df.copy()
            df.columns = ["value_code", "date", "staff", "task", "hours"]

        if {"date", "staff", "task"}.issubset(df.columns):

            if "hours" not in df.columns:
                df["hours"] = None

            df = df[["date", "staff", "task", "hours"]]
            df["sheet_name"] = sheet
            df["date"] = df["date"].apply(safe_parse_date)
            df = df.dropna(subset=["task", "date", "staff"])
            sheets_data[sheet] = df
    return sheets_data