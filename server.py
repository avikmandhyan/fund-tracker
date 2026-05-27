#!/usr/bin/env python3
"""
NSE Fund Tracker — All-in-one server
Serves the HTML tool AND fetches bhavcopy data.
Run: python3 server.py
Open: http://localhost:7001
"""

import os, io, csv, zipfile, datetime, time, json, logging, threading, requests
from pathlib import Path
from flask import Flask, jsonify, request, Response

PORT = int(os.environ.get("PORT", 7001))
CACHE_DIR = Path(__file__).parent / "cache"
CACHE_DIR.mkdir(exist_ok=True)
HTML_FILE = Path(__file__).parent / "fund-tracker.html"
LOG_FILE  = Path(__file__).parent / "server.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)]
)
log = logging.getLogger("nse")

app = Flask(__name__)

# ── CORS — allow all origins ─────────────────────────────────────────────
@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET,OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "*"
    return r

@app.route("/", methods=["OPTIONS"])
@app.route("/<path:p>", methods=["OPTIONS"])
def options(p=""):
    return "", 204

# ── Serve HTML ────────────────────────────────────────────────────────────
@app.route("/")
def index():
    if HTML_FILE.exists():
        return Response(HTML_FILE.read_text("utf-8"), mimetype="text/html")
    return "<h2>fund-tracker.html not found next to server.py</h2>", 404

@app.route("/ping")
def ping():
    return "pong"

# ── Price DB ──────────────────────────────────────────────────────────────
price_db_today: dict = {}   # today's prices
price_db_week:  dict = {}   # 1 week ago prices  
price_db_month: dict = {}   # 1 month ago prices
db_dates: dict = {}
db_lock  = threading.Lock()

# Keep price_db as alias for today (backwards compat)
price_db = price_db_today
EQUITY   = {"EQ","BE","BZ","SM","ST","N1","N2","N3","N4","N5","N6","N7","N8"}

# ── NSE fetch helpers ────────────────────────────────────────────────────
NSE_HDR = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
SESSION = requests.Session()
SESSION.headers.update(NSE_HDR)

def warm():
    try:
        SESSION.get("https://www.nseindia.com", timeout=10)
        log.info("NSE session warmed")
    except Exception as e:
        log.warning(f"Warm failed: {e}")

def trading_day(n: int) -> datetime.date:
    """n trading days back from today (IST)."""
    ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    d   = ist.date()
    if ist.hour < 20:          # bhavcopy not out yet
        d -= datetime.timedelta(days=1)
    count = 0
    while True:
        if d.weekday() < 5:
            if count == n: return d
            count += 1
        d -= datetime.timedelta(days=1)

def bhav_url(d: datetime.date) -> str:
    return (f"https://nsearchives.nseindia.com/content/cm/"
            f"BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip")

def cache_path(d: datetime.date) -> Path:
    return CACHE_DIR / f"bhav_{d:%Y%m%d}.csv"

def fetch_bhav(d: datetime.date, label: str) -> bool:
    cp = cache_path(d)
    # Try cache first
    for back in range(5):
        td = d - datetime.timedelta(days=back)
        tcp = cache_path(td)
        if tcp.exists():
            log.info(f"Cache hit: {tcp.name}")
            _load_csv(tcp.read_text("utf-8"), label, td)
            return True
        # Download
        if td.weekday() >= 5: continue
        url = bhav_url(td)
        log.info(f"Fetching {url}")
        try:
            r = SESSION.get(url, timeout=25)
            if r.status_code != 200:
                log.warning(f"HTTP {r.status_code}")
                continue
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                fn = [n for n in z.namelist() if n.endswith(".csv")][0]
                text = z.read(fn).decode("utf-8")
            tcp.write_text(text, "utf-8")
            log.info(f"Saved {len(text)//1024}KB → {tcp.name}")
            _load_csv(text, label, td)
            return True
        except Exception as e:
            log.warning(f"Attempt back={back}: {e}")
    return False

def _load_csv(text: str, label: str, d: datetime.date):
    # Select the right DB based on label
    db = price_db_today if label == "today" else price_db_week if label == "week" else price_db_month
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    with db_lock:
        db.clear()
        for row in reader:
            if (row.get("SctySrs") or row.get("SERIES","")).strip() not in EQUITY:
                continue
            sym  = (row.get("TckrSymb") or row.get("SYMBOL","")).strip().upper()
            isin = (row.get("ISIN","")).strip()
            try:
                close = float(row.get("ClsPric")     or row.get("CLOSE")     or 0)
                prev  = float(row.get("PrvsClsgPric") or row.get("PREVCLOSE") or 0)
            except: continue
            if not sym or close <= 0: continue
            entry = {"close": close, "prevClose": prev, "date": d.isoformat(), "isin": isin}
            db[sym]  = entry
            if isin: db[isin] = entry
            n += 1
        db_dates[label] = d.isoformat()
    log.info(f"Loaded {n} rows for {label} ({d})")

# ── API endpoints ─────────────────────────────────────────────────────────
@app.route("/status")
def status():
    with db_lock:
        syms = len([k for k in price_db_today if not k.startswith("INE")])
    return jsonify({"status":"ok","stocks_loaded":syms,"dates":db_dates})

@app.route("/price")
def get_price():
    sym = request.args.get("symbol","").upper().replace(".NS","").replace(".BO","")
    with db_lock: e = price_db.get(sym)
    if not e: return jsonify({"error":"not found"}), 404
    return jsonify({"symbol":sym,**e})

@app.route("/prices")
def get_prices():
    raw  = request.args.get("symbols","")
    syms = [s.strip().upper().replace(".NS","").replace(".BO","") for s in raw.split(",") if s.strip()]
    if not syms: return jsonify({"error":"symbols required"}), 400

    # Build per-symbol response with today, week, month prices
    result = {}
    missing = []
    with db_lock:
        # Load week and month DBs from cache if not in memory
        pass

    # Return all price data — client will use today/prevClose for 1D
    # and we serve week/month from separate cached bhavcopies
    found, missing = {}, []
    with db_lock:
        for s in syms:
            e = price_db_today.get(s)
            if e:
                found[s] = e
            else:
                missing.append(s)
    return jsonify({"found":found,"missing":missing,
                    "dates":db_dates,
                    "total_requested":len(syms),"total_found":len(found)})


def _prices_from_cache(label: str, syms: list) -> dict:
    """Read prices from the in-memory db for the given label."""
    db = price_db_today if label == "today" else price_db_week if label == "week" else price_db_month
    syms_set = set(syms)
    date_str = db_dates.get(label, "")
    found = {}
    with db_lock:
        for sym in syms_set:
            e = db.get(sym)
            if e:
                found[sym] = {"close": e["close"], "date": date_str}
    return found

@app.route("/prices/week")
def get_prices_week():
    raw  = request.args.get("symbols","")
    syms = [s.strip().upper().replace(".NS","").replace(".BO","") for s in raw.split(",") if s.strip()]
    if not syms: return jsonify({"error":"symbols required"}), 400
    if "week" not in db_dates: return jsonify({"error":"week bhavcopy not loaded yet"}), 404
    found = _prices_from_cache("week", syms)
    return jsonify({"found": found, "date": db_dates.get("week","")})

@app.route("/prices/month")
def get_prices_month():
    raw  = request.args.get("symbols","")
    syms = [s.strip().upper().replace(".NS","").replace(".BO","") for s in raw.split(",") if s.strip()]
    if not syms: return jsonify({"error":"symbols required"}), 400
    if "month" not in db_dates: return jsonify({"error":"month bhavcopy not loaded yet"}), 404
    found = _prices_from_cache("month", syms)
    return jsonify({"found": found, "date": db_dates.get("month","")})

@app.route("/refresh")
def refresh():
    for n in [0,5,22]:
        cp = cache_path(trading_day(n))
        if cp.exists(): cp.unlink()
    threading.Thread(target=load_all, daemon=True).start()
    return jsonify({"status":"refreshing"})

@app.route("/cache")
def list_cache():
    files = sorted(CACHE_DIR.glob("bhav_*.csv"), reverse=True)
    return jsonify({"files":[f.stem for f in files],"count":len(files)})

# ── Background loader ────────────────────────────────────────────────────
def load_all():
    warm()
    for label, n in [("today",0),("week",5),("month",22)]:
        log.info(f"Loading {label}…")
        ok = fetch_bhav(trading_day(n), label)
        log.info(f"  {label}: {'OK' if ok else 'FAILED'}")
        time.sleep(1)

# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════╗
║   NSE Fund Tracker  — port {PORT}     ║
╠══════════════════════════════════════╣
║  Open: http://localhost:{PORT}         ║
║  /status  /prices  /refresh /cache  ║
╚══════════════════════════════════════╝
Loading bhavcopies in background...
""")
    threading.Thread(target=load_all, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
