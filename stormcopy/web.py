"""Small localhost upload UI. Scans the existing index; never downloads on upload."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile

from .fingerprint import MAX_XML_BYTES
from .index import connect, scan

HTML = """<!doctype html><html lang="en"><meta charset="utf-8"><title>Stormworks Copy Detector</title>
<style>body{font:16px system-ui;max-width:850px;margin:3rem auto;padding:0 1rem;color:#17212b}
button{padding:.6rem 1rem}section{border:1px solid #ccd5df;border-radius:8px;padding:1rem;margin:1rem 0}
.sub{color:#52606d}progress{width:100%}</style>
<h1>Stormworks Copy Detector</h1><p class="sub">Compare a vehicle XML with your local Workshop index.</p>
<input id="file" type="file" accept=".xml,text/xml"><button id="scan">Scan</button>
<p id="message"></p><div id="results"></div>
<script>
const $=id=>document.getElementById(id), out=$('results');
function el(tag,text,parent){let n=document.createElement(tag);n.textContent=text;parent.appendChild(n);return n}
function pos(value){return Array.isArray(value)?value.join(','):'unknown'}
$('scan').onclick=async()=>{let f=$('file').files[0];if(!f){$('message').textContent='Choose an XML file.';return}
out.replaceChildren();$('message').textContent='Scanning…';
try{let r=await fetch('/scan',{method:'POST',headers:{'Content-Type':'application/xml'},body:f});let d=await r.json();
if(!r.ok)throw Error(d.error||'Scan failed');$('message').textContent=d.status;
let c=el('p',`Indexed ${d.coverage.indexed_items} items (${d.coverage.indexed_files} vehicle files); known ${d.coverage.known_items}. Discovery complete: ${d.coverage.discovery_complete?'yes':'no'}.`,out);
if(d.note)el('p',d.note,out);for(let m of d.matches){let s=el('section','',out);
el('h2',`${m.title||m.item_id} — ${m.similarity_percent}% query coverage`,s);
el('p',`${m.confidence} heuristic confidence · ${m.workshop_coverage_percent}% of indexed vehicle covered · ${m.shared_neighborhoods} shared neighborhoods across ${m.distinct_shared_neighborhoods} distinct patterns`,s);
el('p',`${m.combined_similarity_percent}% multi-signal overlap · MinHash Jaccard estimate: ${m.minhash_jaccard_estimate_percent===null?'unavailable':m.minhash_jaccard_estimate_percent+'%'}`,s);
if(m.partial_copy_evidence)el('p','Nearby matching structural anchors suggest a copied section.',s);
if(m.semantic_copy_evidence)el('p','Shared controller or logic content is present.',s);
if(m.url){let a=el('a','Open Workshop item',s);a.href=m.url;a.target='_blank';a.rel='noopener noreferrer'}
el('p',`Evidence: ${m.rare_shared_neighborhoods} rare shared neighborhoods; sample matching positions: `+m.evidence.map(e=>e.channel+': '+pos(e.query_position)+' ↔ '+pos(e.workshop_position)).join('; '),s)}
}catch(e){$('message').textContent=e.message}}
</script></html>"""


def serve(db_path, port=8765):
    db_path = Path(db_path)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != "/":
                self.send_error(404)
                return
            data = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            if self.path != "/scan":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > MAX_XML_BYTES:
                    raise ValueError("Upload must be 1 byte to 64 MiB")
                payload = self.rfile.read(size)
                with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
                    tmp.write(payload)
                    temp_path = Path(tmp.name)
                try:
                    db = connect(db_path)
                    try:
                        result = scan(db, temp_path)
                    finally:
                        db.close()
                finally:
                    temp_path.unlink(missing_ok=True)
                status = 200
            except (ValueError, OSError) as exc:
                result, status = {"error": str(exc)}, 400
            data = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Open http://127.0.0.1:{port}/")
    server.serve_forever()
