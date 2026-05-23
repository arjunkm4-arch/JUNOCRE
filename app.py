import os, json, re, time, uuid, threading
from flask import Flask, request, jsonify, render_template, send_from_directory
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

DATA_FILE = 'data/properties.json'
os.makedirs('uploads', exist_ok=True)
os.makedirs('data', exist_ok=True)

# ── Data helpers ──────────────────────────────────────────────────────────────

def load_props():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return []

def save_props(props):
    with open(DATA_FILE, 'w') as f:
        json.dump(props, f, indent=2)

# ── PDF extraction ────────────────────────────────────────────────────────────

def extract_text_from_pdf(filepath):
    try:
        from pdfminer.high_level import extract_text
        return extract_text(filepath, maxpages=4)
    except Exception as e:
        return ''

def parse_deal(text, filename):
    d = {'name': filename.replace('.pdf', '').replace('_', ' ').strip()}

    # Name: first short meaningful line in first 600 chars
    for line in text[:600].split('\n'):
        line = line.strip()
        if 4 < len(line) < 70 and not any(x in line.lower() for x in
                ['confidential', 'offering memorandum', 'disclaimer', 'www.', '@', 'table of']):
            d['name'] = line
            break

    # Address
    addr_re = re.compile(
        r'(\d{3,6}\s+[\w\s\.\-]+?'
        r'(?:Road|Rd|Street|St|Avenue|Ave|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|'
        r'Pkwy|Parkway|Highway|Hwy|Freeway|Fwy|FM\s*\d+|Farm\s+to\s+Market\s+\w+|'
        r'Grand\s+Pkwy|Southwest\s+Fwy|South\s+Freeway|East\s+Freeway|Northwest\s+Freeway|Gulf\s+Freeway)'
        r'[^,\n]{0,40},\s*'
        r'(?:Houston|Humble|Katy|Spring|Tomball|Cypress|Pasadena|Pearland|Richmond|'
        r'Sugar Land|Missouri City|Baytown|Kingwood|The Woodlands|Woodlands|La Porte|'
        r'Fulshear|Bryan|Stafford|Texas City|Spring)[^,\n]{0,30},?\s*(?:Texas|TX)[\s,]+\d{5})',
        re.IGNORECASE)
    m = addr_re.search(text)
    if m:
        d['address'] = re.sub(r'\s+', ' ', m.group(1)).strip()

    # Price
    for pat in [
        r'(?:sale\s*price|asking\s*price|price)[:\s\|]*\$\s*([\d,]+(?:\.\d+)?)',
        r'\$\s*([\d,]+(?:\.\d+)?)\s*(?:million|MM)',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(',', '')
            try:
                val = float(raw)
                if 'million' in m.group(0).lower() or 'MM' in m.group(0):
                    val *= 1_000_000
                d['price'] = int(val)
                d['price_display'] = f"${val:,.0f}" if val < 1_000_000 else f"${val/1_000_000:.2f}M"
            except:
                pass
            break

    # Cap rate
    for pat in [r'cap\s*rate[:\s\|~]*(\d+\.?\d*)\s*%', r'(\d+\.?\d*)\s*%\s*cap']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                d['cap_rate'] = float(m.group(1))
            except:
                pass
            break

    # NOI
    for pat in [
        r'(?:NOI|net\s*operating\s*income)[:\s\|]*\$\s*([\d,]+)',
        r'\$\s*([\d,]+)\s*(?:NOI|net\s*operating)',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                d['noi'] = int(m.group(1).replace(',', ''))
                d['noi_display'] = f"${d['noi']:,}"
            except:
                pass
            break

    # SF / GLA
    for pat in [
        r'(?:GLA|gross\s*leasable\s*area|net\s*rentable\s*area|building\s*size|rentable\s*area)[:\s\|±]*([0-9,]+)\s*(?:SF|sq)',
        r'([0-9,]+)\s*SF\b',
    ]:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                sf = int(m.group(1).replace(',', ''))
                if 1_000 < sf < 2_000_000:
                    d['sf'] = sf
                    d['sf_display'] = f"{sf:,} SF"
                    break
            except:
                pass

    # Occupancy
    for pat in [r'(?:occupancy|in-place\s*occupancy)[:\s\|]*(\d+)\s*%', r'(\d+)\s*%\s*(?:occupied|leased)']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                d['occupancy'] = int(m.group(1))
            except:
                pass
            break

    # Year built
    for pat in [r'(?:year\s*built|built)[:\s\|]*(\d{4})', r'(?:constructed|delivered)[:\s\|]*(\d{4})']:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                yr = int(m.group(1))
                if 1950 < yr < 2030:
                    d['year_built'] = yr
                    break
            except:
                pass

    return d

def geocode(address):
    import requests as rq
    if not address:
        return None, None
    try:
        r = rq.get('https://nominatim.openstreetmap.org/search',
                   params={'q': address, 'format': 'json', 'limit': 1},
                   headers={'User-Agent': 'OM-Platform/1.0'},
                   timeout=8)
        res = r.json()
        if res:
            return float(res[0]['lat']), float(res[0]['lon'])
    except Exception as e:
        print(f'Geocode error: {e}')
    return None, None

# ── Routes ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/properties', methods=['GET'])
def get_properties():
    return jsonify(load_props())

@app.route('/api/properties/<pid>', methods=['DELETE'])
def delete_property(pid):
    props = [p for p in load_props() if p.get('id') != pid]
    save_props(props)
    return jsonify({'ok': True})

@app.route('/api/properties/<pid>', methods=['PATCH'])
def update_property(pid):
    props = load_props()
    updates = request.json or {}
    for p in props:
        if p.get('id') == pid:
            p.update(updates)
            break
    save_props(props)
    return jsonify({'ok': True})

@app.route('/api/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    file = request.files['file']
    if not file.filename.lower().endswith('.pdf'):
        return jsonify({'error': 'Only PDF files are supported'}), 400

    filename = secure_filename(file.filename)
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(filepath)

    text = extract_text_from_pdf(filepath)
    if not text.strip():
        return jsonify({'error': 'Could not extract text from PDF (may be scanned image)'}), 422

    deal = parse_deal(text, filename)
    deal['id'] = str(uuid.uuid4())
    deal['filename'] = filename
    deal['added'] = time.strftime('%Y-%m-%d')
    deal['source'] = 'upload'

    # Geocode in background so upload feels instant
    def geo_and_save(d):
        addr = d.get('address', '')
        if addr:
            lat, lon = geocode(addr)
            if lat:
                d['lat'] = lat
                d['lon'] = lon
        props = load_props()
        props.append(d)
        save_props(props)

    t = threading.Thread(target=geo_and_save, args=(deal,))
    t.daemon = True
    t.start()

    # Return immediately with what we have
    return jsonify({**deal, 'geocoding': 'in_progress'})

@app.route('/uploads/<filename>')
def serve_upload(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
