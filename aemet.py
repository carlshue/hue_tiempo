"""Acceso a AEMET OpenData con caché en disco, reintentos y espaciado entre peticiones."""
import io
import json
import os
import re
import tarfile
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
API = "https://opendata.aemet.es/opendata/api"
GEOJSON = "https://raw.githubusercontent.com/codeforgermany/click_that_hood/main/public/data/spain-provinces.geojson"


def cargar_env():
    env = HERE / ".env"
    if env.exists():
        for linea in env.read_text(encoding="utf8").splitlines():
            if "=" in linea and not linea.startswith("#"):
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


cargar_env()
DATA = Path(os.environ.get("DATA_DIR", HERE / ".cache"))
DATA.mkdir(parents=True, exist_ok=True)

_lock = threading.Lock()
_ultimo = [0.0]


def hoy_madrid():
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:  # sin base de zonas horarias (Windows sin tzdata)
        return datetime.now(timezone(timedelta(hours=2)))


def _espera():
    with _lock:
        d = 0.7 - (time.time() - _ultimo[0])
        if d > 0:
            time.sleep(d)
        _ultimo[0] = time.time()


def _decodificar(b):
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def _get(url, con_clave=True, binario=False, intentos=8):
    headers = {"api_key": os.environ["AEMET_API_KEY"].strip().strip("\"'")} if con_clave else {}
    for i in range(intentos):
        _espera()
        r = requests.get(url, headers=headers, timeout=60)
        if r.status_code in (429, 500, 502, 503):
            time.sleep(5 * (i + 1))
            continue
        r.raise_for_status()
        return r.content if binario else json.loads(_decodificar(r.content))
    raise RuntimeError(f"AEMET no responde: {url}")


def _cached(nombre, ttl, fn):
    f = DATA / nombre
    if f.exists() and time.time() - f.stat().st_mtime < ttl:
        return json.loads(f.read_text(encoding="utf8"))
    datos = fn()
    f.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf8")
    return datos


def forecast(ine, ttl=3 * 3600):
    """Predicción diaria de un municipio: {'nombre', 'dias': [...]}"""
    def pedir():
        meta = _get(f"{API}/prediccion/especifica/municipio/diaria/{ine}")
        d = _get(meta["datos"], con_clave=False)[0]
        return {"nombre": d.get("nombre", ""), "dias": d["prediccion"]["dia"]}
    return _cached(f"pred_{ine}.json", ttl, pedir)


def alertas(area, ttl=3600):
    """Avisos CAP vigentes de una comunidad (área '61'..'79') o de toda España ('esp')."""
    def pedir():
        meta = _get(f"{API}/avisos_cap/ultimoelaborado/area/{area}")
        raw = _get(meta["datos"], con_clave=False, binario=True)
        out, ns = [], {"c": "urn:oasis:names:tc:emergency:cap:1.2"}
        with tarfile.open(fileobj=io.BytesIO(raw)) as t:
            for m in t.getmembers():
                if not m.isfile():
                    continue
                root = ET.fromstring(t.extractfile(m).read())
                for info in root.findall("c:info", ns):
                    if info.findtext("c:language", "", ns) != "es-ES":
                        continue
                    nivel = ""
                    for p in info.findall("c:parameter", ns):
                        if p.findtext("c:valueName", "", ns) == "AEMET-Meteoalerta nivel":
                            nivel = p.findtext("c:value", "", ns)
                    if nivel == "verde":  # no aportan nada y pesan mucho
                        continue
                    fen = info.findtext("c:eventCode/c:value", "", ns).split(";")[-1]
                    for a in info.findall("c:area", ns):
                        polys = [[[float(v) for v in pt.split(",")] for pt in pg.text.split()]
                                 for pg in a.findall("c:polygon", ns)]
                        out.append(dict(nivel=nivel, fenomeno=fen, zona=a.findtext("c:areaDesc", "", ns),
                                        cod=a.findtext("c:geocode/c:value", "", ns), polys=polys,
                                        onset=info.findtext("c:onset", "", ns), expires=info.findtext("c:expires", "", ns)))
        return out
    return _cached(f"avisos_{area}.json", ttl, pedir)


def texto_nacional(ttl=3 * 3600):
    """'Fenómenos significativos' de la predicción general de AEMET para hoy y mañana."""
    def pedir():
        partes = []
        for ep, etiqueta in (("hoy", "Hoy"), ("manana", "Mañana")):
            meta = _get(f"{API}/prediccion/nacional/{ep}")
            raw = _get(meta["datos"], con_clave=False, binario=True).decode("latin-1")
            m = re.search(r"FENÓMENOS SIGNIFICATIVOS\s*(.*?)\s*B\.-", raw, re.S)
            if m:
                partes.append(f"{etiqueta}: " + " ".join(m.group(1).split()))
        return {"texto": " ".join(partes)}
    return _cached("nacional.json", ttl, pedir)["texto"]


def geojson():
    return _cached("provincias.json", 365 * 86400, lambda: requests.get(GEOJSON, timeout=60).json())
