#!/usr/bin/env python3
"""
Fetches NSE bhavcopies (today, 1-week, 1-month) and saves to data/ folder.
Run by GitHub Actions every weekday at 8:30 PM IST.
"""
import os, io, csv, zipfile, datetime, time, requests
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
})

def warm():
    try:
        SESSION.get("https://www.nseindia.com", timeout=15)
        print("Session warmed")
        time.sleep(2)
    except Exception as e:
        print(f"Warm failed: {e}")

def bhav_url(d: datetime.date) -> str:
    return (f"https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")

def trading_day(n: int) -> datetime.date:
    """Return the date n trading days back from today."""
    d = datetime.date.today()
    count = 0
    while True:
        if d.weekday() < 5:
            if count == n:
                return d
            count += 1
        d -= datetime.timedelta(days=1)

def fetch_and_save(date: datetime.date, label: str) -> bool:
    """Try to download bhavcopy for date (and a few days back for holidays)."""
    for back in range(5):
        try_date = date - datetime.timedelta(days=back)
        if try_date.weekday() >= 5:
            continue
        url = bhav_url(try_date)
        print(f"  Trying {url}")
        try:
            r = SESSION.get(url, timeout=30)
            if r.status_code != 200:
                print(f"  HTTP {r.status_code}")
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                csv_name = [n for n in z.namelist() if n.endswith(".csv")][0]
                text = z.read(csv_name).decode("utf-8")
            out = DATA_DIR / f"bhav_{label}.csv"
            out.write_text(text, encoding="utf-8")
            # Also save date reference
            (DATA_DIR / f"bhav_{label}_date.txt").write_text(try_date.isoformat())
            rows = len(text.splitlines()) - 1
            print(f"  Saved {label}: {try_date} ({rows} rows)")
            return True
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(1)
    return False

def main():
    warm()

    dates = {
        "today": trading_day(0),
        "week":  trading_day(5),
        "month": trading_day(22),
    }

    for label, date in dates.items():
        print(f"\nFetching {label} ({date})...")
        ok = fetch_and_save(date, label)
        print(f"  {'OK' if ok else 'FAILED'}")
        time.sleep(2)

    print("\nDone.")

if __name__ == "__main__":
    main()
