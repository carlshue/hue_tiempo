#!/usr/bin/env python3
"""Mapa cartoony del tiempo en la Región de Murcia (estilo pizarra blanca magnética).

Datos: AEMET OpenData (predicción diaria por municipio + avisos CAP, área 72 = Murcia).
La clave se lee de la variable de entorno AEMET_API_KEY o del fichero .env.

Uso:  python mapa_tiempo_murcia.py [--refresh] [--salida tiempo_murcia.png]
"""
import argparse
import io
import json
import os
import tarfile
import textwrap
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import requests
from matplotlib import font_manager as fm
from matplotlib.patches import Circle, Ellipse, Polygon as MPoly, Rectangle, FancyBboxPatch
from matplotlib.path import Path as MPath
from matplotlib.patches import PathPatch
from PIL import Image, ImageFilter
from scipy.ndimage import gaussian_filter

HERE = Path(__file__).parent
CACHE = HERE / ".cache"
API = "https://opendata.aemet.es/opendata/api"
GEOJSON = "https://raw.githubusercontent.com/codeforgermany/click_that_hood/main/public/data/spain-provinces.geojson"

W, H = 1920, 1200
rng = np.random.default_rng(7)

# Municipios: nombre -> (código INE, lon, lat)
CIUDADES = {
    "Murcia": ("30030", -1.13, 37.98),
    "Cartagena": ("30016", -0.99, 37.60),
    "Lorca": ("30024", -1.70, 37.67),
    "Cieza": ("30019", -1.42, 38.24),
    "Caravaca": ("30015", -1.86, 38.11),
    "Yecla": ("30043", -1.11, 38.61),
    "Jumilla": ("30022", -1.33, 38.47),
    "Águilas": ("30005", -1.58, 37.41),
    "San Javier": ("30035", -0.83, 37.80),
}

# Paleta rotuladores
NEGRO, AZUL, ROJO, VERDE, NARANJA, AMARILLO = "#1d1d24", "#2766c9", "#d63a3a", "#2e9e55", "#f08a1c", "#f3c614"
NIVEL_COL = {"amarillo": AMARILLO, "naranja": NARANJA, "rojo": ROJO}
NIVEL_RANK = {"verde": 0, "amarillo": 1, "naranja": 2, "rojo": 3}
BG = np.array([244, 244, 240])

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DIAS_L = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


# ───────────────────────── datos ─────────────────────────
def api_key():
    k = os.environ.get("AEMET_API_KEY")
    env = HERE / ".env"
    if not k and env.exists():
        for line in env.read_text(encoding="utf8").splitlines():
            if line.startswith("AEMET_API_KEY="):
                k = line.split("=", 1)[1].strip()
    if not k:
        raise SystemExit("Falta AEMET_API_KEY (variable de entorno o .env)")
    return k


def aemet_get(url, key, binary=False, intentos=6):
    for i in range(intentos):
        r = requests.get(url, headers={"api_key": key}, timeout=40)
        if r.status_code == 429:  # límite de peticiones
            time.sleep(4 * (i + 1))
            continue
        r.raise_for_status()
        return r.content if binary else r.json()
    raise RuntimeError(f"AEMET: demasiadas peticiones ({url})")


def cached(name, refresh, fn):
    CACHE.mkdir(exist_ok=True)
    f = CACHE / name
    if f.exists() and not refresh and time.time() - f.stat().st_mtime < 3 * 3600:
        return json.loads(f.read_text(encoding="utf8"))
    data = fn()
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf8")
    return data


def forecast_city(code, key):
    meta = aemet_get(f"{API}/prediccion/especifica/municipio/diaria/{code}", key)
    time.sleep(0.6)
    return requests.get(meta["datos"], timeout=40).json()[0]["prediccion"]["dia"]


def fetch_alerts(key):
    meta = aemet_get(f"{API}/avisos_cap/ultimoelaborado/area/72", key)
    raw = requests.get(meta["datos"], timeout=60).content
    out = []
    ns = {"c": "urn:oasis:names:tc:emergency:cap:1.2"}
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
                fen = info.findtext("c:eventCode/c:value", "", ns).split(";")[-1]
                for a in info.findall("c:area", ns):
                    polys = []
                    for pg in a.findall("c:polygon", ns):
                        polys.append([[float(v) for v in pt.split(",")] for pt in pg.text.split()])
                    out.append(dict(
                        nivel=nivel, fenomeno=fen, zona=a.findtext("c:areaDesc", "", ns),
                        cod=a.findtext("c:geocode/c:value", "", ns), polys=polys,
                        onset=info.findtext("c:onset", "", ns), expires=info.findtext("c:expires", "", ns)))
    return out


def sky_kind(code):
    """Código de estado del cielo AEMET -> tipo de icono."""
    c = (code or "").rstrip("n")
    if not c:
        return "nuboso"
    n = int(c)
    return {11: "sol", 12: "poco", 13: "poco", 14: "nuboso", 15: "nuboso", 16: "nuboso", 17: "poco",
            81: "niebla", 82: "niebla", 83: "calima"}.get(n) or (
        "tormenta" if 51 <= n <= 64 else "nieve" if 33 <= n <= 36 or 71 <= n <= 74
        else "llovizna" if 43 <= n <= 46 else "lluvia" if 23 <= n <= 26 else "nuboso")


def day_summary(dia):
    """Resume un día de predicción: icono, prob. lluvia, tmax, tmin."""
    est = {e.get("periodo", "00-24"): e for e in dia["estadoCielo"] if e["value"]}
    cod = next((est[p]["value"] for p in ("00-24", "06-12", "12-18", "12-24", "00-12") if p in est), "")
    # si hay lluvia/tormenta en algún tramo del día, que mande sobre el resumen
    for e in est.values():
        if sky_kind(e["value"]) in ("tormenta", "lluvia") and "00-24" not in est:
            cod = e["value"]
    prob = max([p["value"] for p in dia["probPrecipitacion"] if p["value"] != ""] or [0])
    t = dia["temperatura"]
    return dict(kind=sky_kind(cod), desc=est.get(next(iter(est), ""), {}).get("descripcion", ""),
                prob=int(prob), tmax=t["maxima"], tmin=t["minima"], fecha=dia["fecha"][:10])


# ───────────────────────── dibujo "rotulador" ─────────────────────────
def wobble(xy, amp=2.2, step=7.0, closed=False, seed=None):
    """Re-muestrea una polilínea y le añade temblor suave de pulso."""
    r = np.random.default_rng(seed) if seed is not None else rng
    xy = np.asarray(xy, float)
    if closed:
        xy = np.vstack([xy, xy[:1]])
    seg = np.hypot(*np.diff(xy, axis=0).T)
    d = np.concatenate([[0], np.cumsum(seg)])
    n = max(int(d[-1] / step), 2)
    t = np.linspace(0, d[-1], n)
    pts = np.c_[np.interp(t, d, xy[:, 0]), np.interp(t, d, xy[:, 1])]
    noise = gaussian_filter(r.normal(0, 1, (n, 2)), (1.5, 0), mode="wrap" if closed else "nearest")
    noise *= amp / max(noise.std(), 1e-6)
    return pts + noise


def stroke(ax, xy, color=NEGRO, lw=3.2, amp=1.8, closed=False, z=5, alpha=0.95, doble=True, ls="-", clip=None):
    for k in range(2 if doble else 1):
        p = wobble(xy, amp=amp * (1 if k == 0 else 1.3), closed=closed)
        ln, = ax.plot(p[:, 0], p[:, 1], color=color, lw=lw * (1 if k == 0 else 0.55), alpha=alpha * (1 if k == 0 else 0.6),
                      solid_capstyle="round", solid_joinstyle="round", zorder=z, ls=ls)
        if clip is not None:
            ln.set_clip_path(clip)


def txt(ax, x, y, s, size=20, color=NEGRO, ha="center", va="center", z=8, rot=0, weight="normal"):
    ax.text(x, y, s, fontsize=size, color=color, ha=ha, va=va, zorder=z, rotation=rot, fontweight=weight,
            fontfamily=FUENTE)


# Iconos: contorno grueso primero (blob negro) y relleno encima -> contorno unificado
def blob(ax, shapes, fill, z=6, edge=NEGRO, lw=3.4):
    """shapes: lista de (cx, cy, rx, ry). Dibuja la unión con contorno de rotulador."""
    for (cx, cy, rx, ry) in shapes:
        ax.add_patch(Ellipse((cx, cy), 2 * rx + lw * 1.6, 2 * ry + lw * 1.6, fc=edge, ec="none", zorder=z))
    for (cx, cy, rx, ry) in shapes:
        ax.add_patch(Ellipse((cx, cy), 2 * rx, 2 * ry, fc=fill, ec="none", zorder=z + 0.1))


def cloud(ax, x, y, s=1.0, fill="#e4ecf5", z=6):
    blob(ax, [(x - 17 * s, y - 4 * s, 15 * s, 11 * s), (x + 3 * s, y + 4 * s, 18 * s, 16 * s),
              (x + 21 * s, y - 3 * s, 14 * s, 11 * s), (x, y - 9 * s, 28 * s, 8 * s)], fill, z)


def sun(ax, x, y, s=1.0, z=6, rays=True):
    if rays:
        for a in np.linspace(0, 2 * np.pi, 9)[:-1] + 0.2:
            r1, r2 = 24 * s, 36 * s
            ax.plot([x + r1 * np.cos(a), x + r2 * np.cos(a)], [y + r1 * np.sin(a), y + r2 * np.sin(a)],
                    color=NARANJA, lw=4.2 * s, solid_capstyle="round", zorder=z)
    blob(ax, [(x, y, 19 * s, 19 * s)], AMARILLO, z, edge=NARANJA, lw=3.0)
    ax.plot([x - 7 * s, x - 3 * s], [y + 4 * s, y + 8 * s], color="#fff3a0", lw=3 * s, solid_capstyle="round", zorder=z + 0.3)


def drops(ax, x, y, s=1.0, n=3, z=7, col=AZUL):
    for i in range(n):
        dx = (i - (n - 1) / 2) * 15 * s
        ax.plot([dx + x + 3 * s, dx + x - 3 * s], [y, y - 13 * s], color=col, lw=3.6 * s, solid_capstyle="round", zorder=z)


def bolt(ax, x, y, s=1.0, z=7):
    pts = np.array([[4, 0], [-8, -14], [-1, -14], [-7, -30], [10, -10], [2, -10], [8, 0]]) * s + [x, y]
    ax.add_patch(MPoly(pts, fc=AMARILLO, ec=NEGRO, lw=2.4, joinstyle="round", zorder=z))


def snow(ax, x, y, s=1.0, z=7):
    for dx in (-14, 0, 14):
        for a in (0, np.pi / 3, 2 * np.pi / 3):
            ax.plot([x + dx * s - 6 * s * np.cos(a), x + dx * s + 6 * s * np.cos(a)],
                    [y - 14 * s - 6 * s * np.sin(a), y - 14 * s + 6 * s * np.sin(a)], color=AZUL, lw=2.4 * s, zorder=z)


def icon(ax, kind, x, y, s=1.0):
    if kind == "sol":
        sun(ax, x, y, s * 1.15)
    elif kind == "poco":
        sun(ax, x - 12 * s, y + 10 * s, s * 0.85)
        cloud(ax, x + 6 * s, y - 6 * s, s * 0.9, z=7)
    elif kind == "nuboso":
        cloud(ax, x - 8 * s, y + 8 * s, s * 0.7, fill="#cdd8e4", z=6)
        cloud(ax, x + 4 * s, y - 2 * s, s * 0.95, z=7)
    elif kind in ("lluvia", "llovizna"):
        cloud(ax, x, y + 8 * s, s, fill="#c3d0e0")
        drops(ax, x, y - 14 * s, s, 3 if kind == "lluvia" else 2)
    elif kind == "tormenta":
        cloud(ax, x, y + 10 * s, s, fill="#aab8cc")
        bolt(ax, x - 2 * s, y - 8 * s, s)
        drops(ax, x + 16 * s, y - 14 * s, s * 0.8, 1)
    elif kind == "nieve":
        cloud(ax, x, y + 8 * s, s, fill="#dbe4ee")
        snow(ax, x, y - 6 * s, s)
    else:  # niebla / calima
        cloud(ax, x, y + 8 * s, s, fill="#e8dcc5" if kind == "calima" else "#e4ecf5")
        for k, dy in enumerate((-12, -22, -32)):
            stroke(ax, [[x - 26 * s + 6 * k, y + dy * s], [x + 26 * s - 4 * k, y + dy * s]], color="#7a8696", lw=3.2 * s,
                   amp=0.8, doble=False, z=7)


def scribble_fill(ax, poly, color, spacing=11, lw=3.0, angle=np.pi / 4, clip=None, alpha=0.8, z=3):
    """Relleno a rayas de rotulador (como pintar con el rotulador de lado a lado)."""
    poly = np.asarray(poly)
    cx, cy = poly.mean(0)
    R = np.hypot(*(poly - [cx, cy]).T).max() + 20
    for off in np.arange(-R, R, spacing):
        a = np.array([-R, off]); b = np.array([R, off])
        rot = np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]])
        a, b = rot @ a + [cx, cy], rot @ b + [cx, cy]
        p = wobble([a, b], amp=1.2, step=14)
        ln, = ax.plot(p[:, 0], p[:, 1], color=color, lw=lw, alpha=alpha, solid_capstyle="round", zorder=z)
        if clip is not None:
            ln.set_clip_path(clip)


def pill(ax, x, y, w, h, color, z=6):
    """Etiqueta redondeada tipo rotulador relleno."""
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0,rounding_size=%d" % (h / 2),
                                fc=color, ec=NEGRO, lw=2.4, zorder=z))


def magnet(ax, x, y, col, r=11):
    ax.add_patch(Circle((x, y), r + 2, fc="#00000030", ec="none", zorder=9.5))
    ax.add_patch(Circle((x, y + 1), r, fc=col, ec="#00000055", lw=1.4, zorder=10))
    ax.add_patch(Circle((x - r * 0.3, y + r * 0.35), r * 0.28, fc="#ffffff99", ec="none", zorder=10.1))


# ───────────────────────── mapa ─────────────────────────
class Proyeccion:
    def __init__(self, bbox_px, lon_rng, lat_rng):
        x0, y0, x1, y1 = bbox_px
        (self.lo0, self.lo1), (self.la0, self.la1) = lon_rng, lat_rng
        k = np.cos(np.radians(38))
        sc = min((x1 - x0) / ((lon_rng[1] - lon_rng[0]) * k), (y1 - y0) / (lat_rng[1] - lat_rng[0]))
        self.sx, self.sy = sc * k, sc
        self.ox = x0 + ((x1 - x0) - (lon_rng[1] - lon_rng[0]) * self.sx) / 2
        self.oy = y0 + ((y1 - y0) - (lat_rng[1] - lat_rng[0]) * self.sy) / 2

    def __call__(self, lon, lat):
        lon, lat = np.asarray(lon, float), np.asarray(lat, float)
        return np.c_[self.ox + (lon - self.lo0) * self.sx, self.oy + (lat - self.la0) * self.sy]


def rings(geom):
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    return [np.array(p[0]) for p in polys]


def path_from(ptsl):
    verts, codes = [], []
    for p in ptsl:
        verts += list(p) + [p[0]]
        codes += [MPath.MOVETO] + [MPath.LINETO] * (len(p) - 1) + [MPath.CLOSEPOLY]
    return MPath(verts, codes)


def dibujar_mapa(ax, proj, geo, ciudades, alertas_activas):
    x0, y0, x1, y1 = 40, 40, 860, 1160
    # mar
    sea = Rectangle((x0, y0), x1 - x0, y1 - y0, fc="none", ec="none", transform=ax.transData)
    ax.add_patch(sea)
    sea_clip = Rectangle((x0, y0), x1 - x0, y1 - y0, transform=ax.transData)
    scribble_fill(ax, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "#7fb4e8", spacing=15, lw=2.4, angle=-0.35, clip=sea_clip, alpha=0.55, z=1)

    provincias = {f["properties"]["name"]: f for f in geo["features"]}
    vecinas = ["Almería", "Granada", "Albacete", "Alacant/Alicante", "Jaén"]
    def grande(p):
        return np.ptp(p[:, 0]) > 40 or np.ptp(p[:, 1]) > 40
    for n in vecinas:
        for r in rings(provincias[n]["geometry"]):
            p = proj(r[:, 0], r[:, 1])
            if not grande(p):
                continue
            pa = MPoly(p, fc="#f4f3ee", ec="none", zorder=1.5)
            ax.add_patch(pa); pa.set_clip_path(sea_clip)
            stroke(ax, p, color="#9aa0a8", lw=2.2, amp=1.5, closed=True, z=2, doble=False, clip=sea_clip)
    murcia = [q for q in (proj(r[:, 0], r[:, 1]) for r in rings(provincias["Murcia"]["geometry"])) if grande(q)]
    for p in murcia:
        ax.add_patch(MPoly(p, fc="#fffef8", ec="none", zorder=2))
    clip_patch = PathPatch(path_from(murcia), transform=ax.transData, fc="none", ec="none")
    ax.add_patch(clip_patch)

    # zonas de aviso (rayadas con el color del nivel más alto)
    for z in alertas_activas.values():
        for poly in z["polys"]:
            pp = proj([c[1] for c in poly], [c[0] for c in poly])
            stroke(ax, pp, color="#555a63", lw=1.8, amp=1.2, closed=True, z=3.5, doble=False, ls=(0, (5, 4)))
            scribble_fill(ax, pp, NIVEL_COL[z["nivel"]], spacing=10, lw=3.4, clip=clip_patch, alpha=0.75, z=3)
    for p in murcia:
        stroke(ax, p, color=NEGRO, lw=4.6, amp=2.4, closed=True, z=6)

    txt(ax, 740, 120, "MAR\nMEDITERRÁNEO", size=21, color=AZUL, rot=14, z=4)
    txt(ax, 110, 130, "ALMERÍA", size=15, color="#7d838c", rot=0)
    txt(ax, 120, 1000, "ALBACETE", size=15, color="#7d838c")
    txt(ax, 790, 960, "ALICANTE", size=15, color="#7d838c")
    txt(ax, 120, 500, "GRANADA", size=15, color="#7d838c", rot=90)

    # ciudades: icono + chapa de imán y etiqueta al lado
    izq = {"San Javier"}
    for nombre, (lon, lat, d) in ciudades.items():
        cx, cy = proj(lon, lat)[0]
        lado = -1 if nombre in izq else 1
        magnet(ax, cx, cy, ROJO if d["prob"] >= 50 else AZUL if d["prob"] >= 20 else VERDE, 7)
        icon(ax, d["kind"], cx + lado * 32, cy + 20, 0.55)
        tx = cx + lado * (100 if nombre == "Cartagena" else 120)
        pill(ax, tx, cy + 14, 112, 24, "#ffffff", z=8)
        txt(ax, tx, cy + 14, nombre, size=14, z=9)
        txt(ax, tx, cy - 14, f"{d['tmin']}°/{d['tmax']}°  {d['prob']}%", size=13, color=AZUL if d["prob"] >= 30 else NEGRO, z=9)


def cargar_logo():
    """Logo (OC) en negro sobre transparente, recortado."""
    im = Image.open(HERE / "logo.png").convert("RGBA")
    px = np.asarray(im, float)
    oscuro = 255 - px[..., :3].mean(axis=2)
    a = np.clip(px[..., 3] * oscuro / 255 * 1.4, 0, 255)
    ys, xs = np.where(a > 40)
    a = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    rgba = np.zeros(a.shape + (4,), np.uint8)
    rgba[..., :3] = (29, 29, 36)
    rgba[..., 3] = a.astype(np.uint8)
    return rgba


def resumen(semana, grupos):
    """Resumen en texto de la previsión (se genera a partir de los datos)."""
    cielo = {"sol": "despejado", "poco": "poco nuboso", "nuboso": "nuboso", "lluvia": "con lluvia", "llovizna": "con lluvia débil",
             "tormenta": "con tormentas", "nieve": "con nieve", "niebla": "con niebla", "calima": "con calima"}
    mojado = lambda s: s["region_prob"] >= 40 or s["kind"] in ("lluvia", "tormenta", "llovizna")
    nombre = lambda s: f"{DIAS_L[datetime.fromisoformat(s['fecha']).weekday()][:3]} {datetime.fromisoformat(s['fecha']).day}"
    h = semana[0]
    partes = [f"Hoy {cielo[h['kind']]}, {h['region_prob']}% de lluvia en la región y máxima de {h['tmax']}°."]
    tramos = []
    for s in semana[1:]:
        if tramos and tramos[-1][0] == mojado(s):
            tramos[-1][1].append(s)
        else:
            tramos.append((mojado(s), [s]))
    frases = []
    for llueve, dias in tramos:
        rango = nombre(dias[0]) if len(dias) == 1 else f"del {nombre(dias[0])} al {nombre(dias[-1])}"
        frases.append(f"lluvia {rango} (hasta {max(d['region_prob'] for d in dias)}%)" if llueve else f"tregua {rango}")
    if frases:
        partes.append("Después: " + "; ".join(frases) + ".")
    tmax = max(semana, key=lambda s: s["tmax"])
    partes.append(f"Máximas entre {min(s['tmax'] for s in semana)}° y {max(s['tmax'] for s in semana)}° (la más alta, el {nombre(tmax)}) "
                  f"y mínimas de {min(s['tmin'] for s in semana)}° a {max(s['tmin'] for s in semana)}°.")
    seguidos = max([len(d) for ll, d in tramos if ll and min(x["region_prob"] for x in d) >= 80] or [0])
    if grupos:
        partes.append("Hay avisos activos: mira los días marcados en rojo.")
    elif seguidos >= 3:
        partes.append("Sin avisos por ahora, pero ojo: varios días seguidos con lluvia muy probable.")
    else:
        partes.append("Sin avisos de nivel amarillo o superior por ahora.")
    return " ".join(partes)


# ───────────────────────── composición ─────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true", help="ignorar la caché de 3 h")
    ap.add_argument("--demo", action="store_true", help="simula un aviso amarillo de lluvias (solo para probar el diseño)")
    ap.add_argument("--salida", default=str(HERE / "tiempo_murcia.png"))
    args = ap.parse_args()
    key = api_key()

    ciudades_pred = {}
    for nombre, (code, lon, lat) in CIUDADES.items():
        ciudades_pred[nombre] = cached(f"pred_{code}.json", args.refresh, lambda c=code: forecast_city(c, key))
    alertas = cached("avisos_72.json", args.refresh, lambda: fetch_alerts(key))
    geo = cached("provincias.json", False, lambda: requests.get(GEOJSON, timeout=60).json())

    # días
    ndias = min(len(v) for v in ciudades_pred.values())
    ndias = min(ndias, 7)
    hoy = {n: day_summary(v[0]) for n, v in ciudades_pred.items()}
    ciudades_hoy = {n: (CIUDADES[n][1], CIUDADES[n][2], hoy[n]) for n in CIUDADES}
    semana = []
    for i in range(ndias):
        por_ciudad = {n: day_summary(v[i]) for n, v in ciudades_pred.items()}
        m = por_ciudad["Murcia"]
        semana.append(dict(m, region_prob=max(d["prob"] for d in por_ciudad.values()),
                           tmax_reg=max(d["tmax"] for d in por_ciudad.values()),
                           lluvia_en=[n for n, d in por_ciudad.items() if d["prob"] >= 40]))

    # avisos activos (no verdes y no caducados)
    ahora = datetime.now(timezone.utc)
    activos = [a for a in alertas if a["nivel"] in NIVEL_COL and datetime.fromisoformat(a["expires"]) > ahora]
    if args.demo:
        activos.append(dict(nivel="amarillo", fenomeno="Lluvias", zona="Campo de Cartagena", cod="720004", polys=[],
                            onset="2026-10-17T06:00:00+02:00", expires="2026-10-18T23:59:00+02:00"))
    por_zona = {}
    for a in activos:
        if a["cod"] not in por_zona or NIVEL_RANK[a["nivel"]] > NIVEL_RANK[por_zona[a["cod"]]["nivel"]]:
            por_zona[a["cod"]] = a
    grupos = {}
    for a in activos:
        g = grupos.setdefault((a["fenomeno"], a["nivel"], a["onset"], a["expires"]), [])
        g.append(a["zona"])

    # ─── figura
    global FUENTE
    for ttf in (HERE / "fonts").glob("*.ttf"):
        fm.fontManager.addfont(str(ttf))
    FUENTE = next((f for f in ("Ink Free", "Patrick Hand", "Segoe Print", "Comic Sans MS") if any(x.name == f for x in fm.fontManager.ttflist)), "sans-serif")
    fig = plt.figure(figsize=(W / 100, H / 100), dpi=100, facecolor="#f4f4f0")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W); ax.set_ylim(0, H); ax.axis("off")

    # restos de borrado en la pizarra (manchones muy tenues)
    for _ in range(26):
        x, y = rng.uniform(0, W), rng.uniform(0, H)
        p = wobble([[x, y], [x + rng.uniform(80, 320), y + rng.uniform(-60, 60)]], amp=6, step=20)
        ax.plot(p[:, 0], p[:, 1], color="#c8ccd2", lw=rng.uniform(10, 26), alpha=0.18, solid_capstyle="round", zorder=0)

    proj = Proyeccion((70, 70, 850, 1130), (-2.40, -0.60), (37.35, 38.82))
    dibujar_mapa(ax, proj, geo, ciudades_hoy, por_zona)
    fecha = datetime.fromisoformat(semana[0]["fecha"])
    txt(ax, 56, 1150, f"{DIAS_L[fecha.weekday()]} {fecha.day} {MESES[fecha.month - 1]}", size=30, ha="left", color=ROJO, z=9)

    # ── presentador (mi OC) + bocadillo con el resumen
    logo = cargar_logo()
    lh, lw_ = 150, 150 * logo.shape[1] / logo.shape[0]
    lx, ly = 905, 975
    ax.imshow(logo, extent=(lx, lx + lw_, ly, ly + lh), zorder=9, interpolation="bilinear", aspect="auto")
    lineas = textwrap.wrap(resumen(semana, grupos), 47)
    bx0, bx1, by1 = 1170, 1875, 1165
    by0 = by1 - 40 - 36 * len(lineas)
    caja = [[bx0, by0], [bx1, by0], [bx1, by1], [bx0, by1]]
    ax.add_patch(MPoly(caja, fc="#ffffff", ec="none", alpha=0.92, zorder=6))
    ax.add_patch(MPoly([[bx0 + 2, ly + 100], [bx0 - 40, ly + 70], [bx0 + 2, ly + 60]], fc="#ffffff", ec="none", alpha=0.92, zorder=6))
    stroke(ax, caja, color=NEGRO, lw=3.4, amp=1.6, closed=True, z=7)
    stroke(ax, [[bx0, ly + 100], [bx0 - 40, ly + 70], [bx0, ly + 60]], color=NEGRO, lw=3.4, amp=0.8, z=7, doble=False)
    y = by1 - 34
    for linea in lineas:
        txt(ax, bx0 + 24, y, linea, size=22, ha="left", color=NEGRO, z=9)
        y -= 36

    # ── avisos (compacto)
    ax_x, top_av = 890, 790
    txt(ax, ax_x, top_av, "AVISOS", size=27, ha="left", color=ROJO)
    stroke(ax, [[ax_x, top_av - 28], [ax_x + 150, top_av - 28]], color=ROJO, lw=4, amp=1.5)
    if not grupos:
        pill(ax, ax_x + 290, top_av, 220, 36, "#bfe8c9")
        txt(ax, ax_x + 290, top_av, "ninguno activo", size=20, color=VERDE)
        txt(ax, ax_x + 430, top_av, "(nivel amarillo o superior)", size=15, ha="left", color="#555a63")
    else:
        filas = sorted(grupos.items(), key=lambda kv: (-NIVEL_RANK[kv[0][1]], kv[0][2]))[:3]
        yy = top_av
        for (fen, nivel, onset, expires), zonas in filas:
            o, e = datetime.fromisoformat(onset), datetime.fromisoformat(expires)
            pill(ax, ax_x + 290, yy, 120, 30, NIVEL_COL[nivel])
            txt(ax, ax_x + 290, yy, nivel.upper(), size=14, weight="bold")
            txt(ax, ax_x + 370, yy, f"{fen} · {DIAS[o.weekday()]} {o.day} {o.hour:02d}h a {DIAS[e.weekday()]} {e.day} {e.hour:02d}h",
                size=17, ha="left", color=NEGRO)
            zs = ", ".join(sorted(set(zonas)))
            txt(ax, ax_x + 370, yy - 22, zs if len(zs) < 70 else zs[:67] + "…", size=12.5, ha="left", color="#555a63")
            yy -= 50
        if len(grupos) > 3:
            txt(ax, ax_x + 150, yy + 8, f"…y {len(grupos) - 3} más (aemet.es)", size=14, ha="left", color="#555a63")

    # ── semana
    sx = 890
    cw = (W - 40 - sx) / len(semana)
    txt(ax, sx, 655, "PRÓXIMOS DÍAS", size=27, ha="left", color=AZUL)
    stroke(ax, [[sx, 627], [sx + 330, 627]], color=AZUL, lw=4, amp=1.5)
    llueve = [s for s in semana if s["region_prob"] >= 40 or s["kind"] in ("lluvia", "tormenta", "llovizna")]
    if llueve:
        dias_ll = ", ".join(DIAS[datetime.fromisoformat(s["fecha"]).weekday()] for s in llueve)
        txt(ax, sx + 360, 655, f"Paraguas: {dias_ll}", size=19, ha="left", color=AZUL)
    else:
        txt(ax, sx + 360, 655, "Sin lluvia prevista esta semana", size=19, ha="left", color=VERDE)
    avisos_dia = {}
    for (fen, nivel, onset, expires) in grupos:
        d0, d1 = datetime.fromisoformat(onset).date(), datetime.fromisoformat(expires).date()
        for s in semana:
            if d0 <= datetime.fromisoformat(s["fecha"]).date() <= d1:
                avisos_dia.setdefault(s["fecha"], []).append((fen, nivel))
    for i, s in enumerate(semana):
        cx = sx + cw * (i + 0.5)
        f = datetime.fromisoformat(s["fecha"])
        lluvia = s in llueve
        av = avisos_dia.get(s["fecha"])
        top, bot = 600, 70
        marco = ROJO if av else AZUL if lluvia else NEGRO
        box = [[cx - cw / 2 + 8, bot], [cx + cw / 2 - 8, bot], [cx + cw / 2 - 8, top], [cx - cw / 2 + 8, top]]
        clip = Rectangle((cx - cw / 2 + 8, bot), cw - 16, top - bot, transform=ax.transData)
        if lluvia:
            scribble_fill(ax, box, "#9cc7f2", spacing=9, lw=3, angle=-1.2, alpha=0.5, z=1.2, clip=clip)
        if av:
            scribble_fill(ax, box, "#f08080", spacing=16, lw=3, angle=0.6, alpha=0.30, z=1.3, clip=clip)
        stroke(ax, box, color=marco, lw=4.2 if av else 3.0, amp=1.4, closed=True, z=4)
        txt(ax, cx, top - 30, DIAS[f.weekday()].upper(), size=24, color=ROJO if f.weekday() >= 5 else NEGRO)
        txt(ax, cx, top - 62, f"{f.day} {MESES[f.month - 1]}", size=16, color="#555a63")
        icon(ax, s["kind"], cx, top - 150, 1.0)
        txt(ax, cx, top - 235, f"{s['tmax']}°", size=34, color=ROJO)
        txt(ax, cx, top - 275, f"{s['tmin']}°", size=24, color=AZUL)
        bw, bh, by = cw - 46, 20, top - 322
        ax.add_patch(Rectangle((cx - bw / 2, by), bw, bh, fc="#ffffff", ec=NEGRO, lw=2.4, zorder=6))
        ax.add_patch(Rectangle((cx - bw / 2, by), bw * s["region_prob"] / 100, bh, fc=AZUL if s["region_prob"] >= 40 else "#8fb3e0",
                               ec="none", zorder=6.1))
        txt(ax, cx, by - 24, f"{s['region_prob']}% lluvia", size=15, color=AZUL if s["region_prob"] >= 40 else NEGRO)
        if s["lluvia_en"]:
            txt(ax, cx, by - 48, f"{len(s['lluvia_en'])}/{len(CIUDADES)} municipios", size=12.5, color="#555a63")
        if av:
            av.sort(key=lambda a: -NIVEL_RANK[a[1]])
            pill(ax, cx, bot + 90, cw - 36, 34, ROJO)
            txt(ax, cx, bot + 90, "¡AVISO!", size=19, color="#ffffff", weight="bold")
            for k, (fen, nivel) in enumerate(av[:2]):
                txt(ax, cx, bot + 52 - k * 24, f"{fen.split()[0][:9]} {nivel[:3]}.", size=14, color=ROJO)

    # bandeja de rotuladores + imanes decorativos
    for k, c in enumerate((NEGRO, AZUL, ROJO, VERDE)):
        ax.add_patch(FancyBboxPatch((1000 + k * 60, 12), 44, 14, boxstyle="round,pad=0,rounding_size=6", fc=c, ec="#00000066", lw=1.2, zorder=9))
    for (mx, my, c) in ((24, 1176, ROJO), (1896, 1176, AZUL), (24, 24, VERDE), (1896, 24, AMARILLO)):
        magnet(ax, mx, my, c, 14)
    txt(ax, W - 40, 20, "Datos: AEMET OpenData", size=12, ha="right", color="#8a8f97")

    # ─── post-proceso: grano, tinta irregular, brillo de pizarra, marco de aluminio
    fig.canvas.draw()
    img = np.asarray(fig.canvas.buffer_rgba())[..., :3].astype(float)
    h, w = img.shape[:2]
    tinta = np.clip((BG - img).max(axis=2) / 140, 0, 1)[..., None]  # 0 = pizarra, 1 = tinta
    # la tinta falla un poco (rotulador seco)
    falla = gaussian_filter(rng.random((h, w)), 1.1)
    falla = (falla - falla.mean()) / falla.std()
    img = BG - (BG - img) * np.clip(0.9 + 0.1 * falla[..., None], 0.55, 1.0)
    # restos de tinta que no salen del todo (ghosting leve)
    ghost = gaussian_filter(rng.random((h, w)), 40)
    ghost = (ghost - ghost.min()) / np.ptp(ghost)
    img = img - 6 * ghost[..., None] * (1 - tinta)
    # grano fino + grano grueso
    grano = rng.normal(0, 1, (h, w))
    img = img + 5.5 * grano[..., None] + 4.0 * gaussian_filter(rng.normal(0, 1, (h, w)), 1.6)[..., None] * 3
    # brillo diagonal de pizarra
    yy, xx = np.mgrid[0:h, 0:w]
    brillo = np.exp(-(((xx - yy * 0.9) - w * 0.45) / (w * 0.16)) ** 2) * 9
    img = img + brillo[..., None] * (1 - tinta)
    # viñeta
    vig = 1 - 0.07 * (((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2)
    img = img * vig[..., None]
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8))
    out = out.filter(ImageFilter.GaussianBlur(0.5))
    # marco de aluminio
    arr = np.asarray(out).astype(float)
    b = 14
    for i in range(b):
        t = i / b
        c = 150 + 90 * t
        arr[i, :] = c; arr[h - 1 - i, :] = c * 0.9; arr[:, i] = c; arr[:, w - 1 - i] = c * 0.9
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(args.salida)
    print("Guardado:", args.salida)
    print(f"Avisos activos: {len(grupos)} | días con lluvia: {[s['fecha'] for s in llueve]}")


if __name__ == "__main__":
    main()
