#!/usr/bin/env python3
"""
NSE Fund Tracker — Railway server
Reads bhavcopies from data/ folder (committed by GitHub Actions daily).
Serves the HTML tool and price API.
"""

import os, io, csv, datetime, logging, threading
from pathlib import Path
from flask import Flask, jsonify, request, Response

PORT     = int(os.environ.get("PORT", 7001))
DATA_DIR = Path(__file__).parent 
HTML_FILE = Path(__file__).parent / "fund-tracker.html"
LOG_FILE  = Path(__file__).parent / "server.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("nse")

app = Flask(__name__)

@app.after_request
def cors(r):
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
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
    return "<h2>fund-tracker.html not found</h2>", 404

@app.route("/ping")
def ping():
    return "pong"

# ── Price databases ───────────────────────────────────────────────────────
price_db_today: dict = {}
price_db_week:  dict = {}
price_db_month: dict = {}
db_dates: dict = {}
db_lock = threading.Lock()

EQUITY = {"EQ","BE","BZ","SM","ST","N1","N2","N3","N4","N5","N6","N7","N8"}

def load_csv(text: str, label: str, date_str: str):
    db = price_db_today if label=="today" else price_db_week if label=="week" else price_db_month
    reader = csv.DictReader(io.StringIO(text))
    n = 0
    with db_lock:
        db.clear()
        for row in reader:
            series = (row.get("SctySrs") or row.get("SERIES","")).strip()
            if series not in EQUITY: continue
            sym  = (row.get("TckrSymb") or row.get("SYMBOL","")).strip().upper()
            isin = (row.get("ISIN","")).strip()
            try:
                close = float(row.get("ClsPric") or row.get("CLOSE") or 0)
                prev  = float(row.get("PrvsClsgPric") or row.get("PREVCLOSE") or 0)
            except: continue
            if not sym or close <= 0: continue
            entry = {"close": close, "prevClose": prev, "date": date_str, "isin": isin}
            db[sym] = entry
            if isin: db[isin] = entry
            n += 1
        db_dates[label] = date_str
    log.info(f"Loaded {n} rows for {label} ({date_str})")

def load_all_from_data():
    """Load bhavcopies from the data/ folder (committed by GitHub Actions)."""
    DATA_DIR.mkdir(exist_ok=True)
    for label in ["today", "week", "month"]:
        csv_file  = DATA_DIR / f"bhav_{label}.csv"
        date_file = DATA_DIR / f"bhav_{label}_date.txt"
        if csv_file.exists():
            date_str = date_file.read_text().strip() if date_file.exists() else "unknown"
            log.info(f"Loading {label} from data/ ({date_str})")
            load_csv(csv_file.read_text("utf-8"), label, date_str)
        else:
            log.warning(f"No data file for {label}: {csv_file}")

# ── API endpoints ─────────────────────────────────────────────────────────
@app.route("/status")
def status():
    with db_lock:
        syms = len([k for k in price_db_today if not k.startswith("INE")])
    return jsonify({"status":"ok","stocks_loaded":syms,"dates":db_dates})

@app.route("/prices")
def get_prices():
    raw  = request.args.get("symbols","")
    syms = [s.strip().upper().replace(".NS","").replace(".BO","") for s in raw.split(",") if s.strip()]
    if not syms: return jsonify({"error":"symbols required"}), 400
    found, missing = {}, []
    with db_lock:
        for s in syms:
            e = price_db_today.get(s)
            if e: found[s] = e
            else: missing.append(s)
    return jsonify({"found":found,"missing":missing,"dates":db_dates,
                    "total_requested":len(syms),"total_found":len(found)})

@app.route("/prices/week")
def get_prices_week():
    raw  = request.args.get("symbols","")
    syms = [s.strip().upper().replace(".NS","").replace(".BO","") for s in raw.split(",") if s.strip()]
    if not syms: return jsonify({"error":"symbols required"}), 400
    found = {}
    with db_lock:
        for s in syms:
            e = price_db_week.get(s)
            if e: found[s] = {"close": e["close"], "date": e["date"]}
    return jsonify({"found":found,"date":db_dates.get("week","")})

@app.route("/prices/month")
def get_prices_month():
    raw  = request.args.get("symbols","")
    syms = [s.strip().upper().replace(".NS","").replace(".BO","") for s in raw.split(",") if s.strip()]
    if not syms: return jsonify({"error":"symbols required"}), 400
    found = {}
    with db_lock:
        for s in syms:
            e = price_db_month.get(s)
            if e: found[s] = {"close": e["close"], "date": e["date"]}
    return jsonify({"found":found,"date":db_dates.get("month","")})

@app.route("/upload-bhav", methods=["POST"])
def upload_bhav():
    """Upload a bhavcopy CSV directly (for manual sync)."""
    label = request.args.get("label","today")
    if label not in ("today","week","month"):
        return jsonify({"error":"label must be today/week/month"}), 400
    text = request.get_data(as_text=True)
    if not text or len(text) < 100:
        return jsonify({"error":"empty body"}), 400
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows: return jsonify({"error":"no rows"}), 400
    date_str = (rows[0].get("TradDt") or rows[0].get("BizDt") or "").strip()[:10]
    try:
        datetime.date.fromisoformat(date_str)
    except:
        date_str = datetime.date.today().isoformat()
    # Save to data/ and load into memory
    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / f"bhav_{label}.csv").write_text(text, "utf-8")
    (DATA_DIR / f"bhav_{label}_date.txt").write_text(date_str)
    load_csv(text, label, date_str)
    with db_lock:
        db = price_db_today if label=="today" else price_db_week if label=="week" else price_db_month
        count = len([k for k in db if not k.startswith("INE")])
    return jsonify({"status":"ok","label":label,"date":date_str,"stocks":count})

# ── Startup ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"""
╔══════════════════════════════════════╗
║   NSE Fund Tracker  — port {PORT}     ║
╠══════════════════════════════════════╣
║  Open: http://localhost:{PORT}         ║
╚══════════════════════════════════════╝
""")
    threading.Thread(target=load_all_from_data, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)

# For gunicorn
load_all_from_data()
