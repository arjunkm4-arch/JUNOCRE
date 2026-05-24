import os, json, re, time, uuid, threading
from flask import Flask, request, jsonify, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024
DATA_FILE = "data/properties.json"
os.makedirs("uploads", exist_ok=True)
os.makedirs("data", exist_ok=True)

def load_props():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f: return json.load(f)
    return []

def save_props(props):
    with open(DATA_FILE, "w") as f: json.dump(props, f, indent=2)

def extract_text_from_pdf(filepath):
    try:
        from pdfminer.high_level import extract_text
        return extract_text(filepath, maxpages=4)
    except: return ""

def parse_deal(text, filename):
    d = {"name": filename.replace(".pdf","").replace("_"," ").strip()}
    for line in text[:600].split("\n"):
        line = line.strip()
        if 4 < len(line) < 70 and not any(x in line.lower() for x in ["confidential","offering memorandum","disclaimer","www.","@","table of"]):
            d["name"] = line; break
    addr_re = re.compile(r"(\d{3,6}\s+[\w\s\.\-]+?(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Pkwy|Parkway|Highway|Hwy|Freeway|Fwy|FM\s*\d+|Grand\s+Pkwy)[^,\n]{0,40},\s*(?:Houston|Humble|Katy|Spring|Tomball|Cypress|Pasadena|Pearland|Richmond|Sugar Land|Missouri City|Baytown|Kingwood|The Woodlands|La Porte|Fulshear|Bryan|Stafford)[^,\n]{0,30},?\s*(?:Texas|TX)[\s,]+\d{5})", re.IGNORECASE)
    m = addr_re.search(text)
    if m: d["address"] = re.sub(r"\s+"," ",m.group(1)).strip()
    for pat in [r"(?:sale\s*price|asking\s*price|price)[:\s\|]*\$\s*([\d,]+(?:\.\d+)?)"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",",""))
                d["price"] = int(val); d["price_display"] = f"${val/1e6:.2f}M" if val >= 1e6 else f"${val:,.0f}"
            except: pass
            break
    for pat in [r"cap\s*rate[:\s\|~]*(\d+\.?\d*)\s*%", r"(\d+\.?\d*)\s*%\s*cap"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try: d["cap_rate"] = float(m.group(1))
            except: pass
            break
    m = re.search(r"(?:NOI|net\s*operating\s*income)[:\s\|]*\$\s*([\d,]+)", text, re.IGNORECASE)
    if m:
        try: d["noi"] = int(m.group(1).replace(",","")); d["noi_display"] = f"${d['noi']:,}"
        except: pass
    for pat in [r"(?:GLA|gross\s*leasable\s*area|building\s*size)[:\s\|]*([0-9,]+)\s*(?:SF|sq)"]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                sf = int(m.group(1).replace(",",""))
                if 1000 < sf < 2000000: d["sf"]=sf; d["sf_display"]=f"{sf:,} SF"; break
            except: pass
    m = re.search(r"(?:occupancy)[:\s\|]*(\d+)\s*%|(\d+)\s*%\s*(?:occupied|leased)", text, re.IGNORECASE)
    if m:
        try: d["occupancy"] = int(m.group(1) or m.group(2))
        except: pass
    m = re.search(r"(?:year\s*built)[:\s\|]*(\d{4})", text, re.IGNORECASE)
    if m:
        try:
            yr = int(m.group(1))
            if 1950 < yr < 2030: d["year_built"] = yr
        except: pass
    return d

def geocode(address):
    import requests as rq
    try:
        r = rq.get("https://nominatim.openstreetmap.org/search", params={"q":address,"format":"json","limit":1}, headers={"User-Agent":"OM-Platform/1.0"}, timeout=8)
        res = r.json()
        if res: return float(res[0]["lat"]), float(res[0]["lon"])
    except: pass
    return None, None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

@app.route("/")
def index():
    html_path = os.path.join(os.getcwd(), "index.html")
    with open(html_path) as f:
        return f.read()

@app.route("/api/properties")
def get_properties():
    return jsonify(load_props())

@app.route("/api/properties/<pid>", methods=["DELETE"])
def delete_property(pid):
    save_props([p for p in load_props() if p.get("id") != pid])
    return jsonify({"ok": True})

@app.route("/api/properties/<pid>", methods=["PATCH"])
def update_property(pid):
    props = load_props()
    for p in props:
        if p.get("id") == pid: p.update(request.json or {}); break
    save_props(props)
    return jsonify({"ok": True})

@app.route("/api/upload", methods=["POST"])
def upload():
    if "file" not in request.files: return jsonify({"error":"No file"}), 400
    file = request.files["file"]
    if not file.filename.lower().endswith(".pdf"): return jsonify({"error":"PDFs only"}), 400
    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    file.save(filepath)
    text = extract_text_from_pdf(filepath)
    if not text.strip(): return jsonify({"error":"Cannot read PDF text"}), 422
    deal = parse_deal(text, filename)
    deal.update({"id": str(uuid.uuid4()), "filename": filename, "added": time.strftime("%Y-%m-%d"), "source": "upload"})
    def geo_save(d):
        if d.get("address"):
            lat, lon = geocode(d["address"])
            if lat: d["lat"]=lat; d["lon"]=lon
        props = load_props(); props.append(d); save_props(props)
    threading.Thread(target=geo_save, args=(deal,), daemon=True).start()
    return jsonify({**deal, "geocoding":"in_progress"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
