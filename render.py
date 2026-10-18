#!/usr/bin/env python3
"""Renderer: mapas cartoony del tiempo (estilo pizarra magnética) por comunidad autónoma y de España.

Uso:  python render.py --ccaa murcia --dias 7 [--demo] [--salida x.png]
      python render.py --nacional --dias 3
"""
import argparse
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib import font_manager as fm
from matplotlib.patches import Circle, Ellipse, FancyBboxPatch, PathPatch, Polygon as MPoly, Rectangle
from matplotlib.path import Path as MPath
from PIL import Image, ImageFilter
from scipy.ndimage import gaussian_filter

import aemet
import ccaa as C

HERE = Path(__file__).parent
W, H = 1920, 1200
rng = np.random.default_rng(7)

# Paleta rotuladores
NEGRO, AZUL, ROJO, VERDE, NARANJA, AMARILLO = "#1d1d24", "#2766c9", "#d63a3a", "#2e9e55", "#f08a1c", "#f3c614"
NIVEL_COL = {"amarillo": AMARILLO, "naranja": NARANJA, "rojo": ROJO}
NIVEL_RANK = {"verde": 0, "amarillo": 1, "naranja": 2, "rojo": 3}
BG = np.array([244, 244, 240])

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
DIAS_L = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]

for _ttf in (HERE / "fonts").glob("*.ttf"):
    fm.fontManager.addfont(str(_ttf))
FUENTE = next((f for f in ("Ink Free", "Patrick Hand", "Segoe Print", "Comic Sans MS")
               if any(x.name == f for x in fm.fontManager.ttflist)), "sans-serif")


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




# ───────────────────────── geografía ─────────────────────────
def prov_geo():
    return {f["properties"]["name"]: f for f in aemet.geojson()["features"]}


def tr_canarias(lon, lat):
    """Recoloca Canarias como recuadro junto al suroeste de la península (solo mapa nacional)."""
    lon, lat = np.asarray(lon, float), np.asarray(lat, float)
    m = lon < -11.5
    return np.where(m, lon + 8.0, lon), np.where(m, lat + 6.4, lat)


class ProyeccionNac(Proyeccion):
    def __call__(self, lon, lat):
        lon, lat = tr_canarias(lon, lat)
        return super().__call__(lon, lat)


def limites(gp, provs, nacional=False):
    pts = np.vstack([r for p in provs for r in rings(gp[p]["geometry"])])
    if nacional:
        pts = np.column_stack(tr_canarias(pts[:, 0], pts[:, 1]))
    lo0, la0 = pts.min(0)
    lo1, la1 = pts.max(0)
    mx, my = (lo1 - lo0) * 0.05, (la1 - la0) * 0.05
    return (lo0 - mx, lo1 + mx), (la0 - my, la1 + my)


def grande(p):
    return np.ptp(p[:, 0]) > 25 or np.ptp(p[:, 1]) > 25


def _solapa(a, b):
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def dibujar_mapa(ax, proj, gp, propias, ciudades, zonas, box, mar, nacional=False):
    """propias: nombres de provincias a resaltar. ciudades: nombre -> (lon, lat, resumen_dia)."""
    x0, y0, x1, y1 = box
    sea_clip = Rectangle((x0, y0), x1 - x0, y1 - y0, transform=ax.transData)
    ax.add_patch(Rectangle((x0, y0), x1 - x0, y1 - y0, fc="none", ec="none"))
    scribble_fill(ax, [[x0, y0], [x1, y0], [x1, y1], [x0, y1]], "#7fb4e8" if mar else "#c4c8cf",
                  spacing=15, lw=2.4, angle=-0.35, clip=sea_clip, alpha=0.55 if mar else 0.45, z=1)

    pos = {}
    for nombre, f in gp.items():
        es_propia = nombre in propias
        anillos = [proj(r[:, 0], r[:, 1]) for r in rings(f["geometry"])]
        anillos = [p for p in anillos if grande(p)]
        pos[nombre] = anillos
        if es_propia and not nacional:
            continue
        vis = [p for p in anillos if _solapa((p[:, 0].min(), p[:, 1].min(), p[:, 0].max(), p[:, 1].max()), box)]
        for p in vis:
            pa = MPoly(p, fc="#f4f3ee", ec="none", zorder=1.5)
            ax.add_patch(pa)
            pa.set_clip_path(sea_clip)
            stroke(ax, p, color="#9aa0a8" if not nacional else "#555a63", lw=2.2 if not nacional else 1.6, amp=1.2 if not nacional else 0.8,
                   closed=True, z=2, doble=False, clip=sea_clip)
        if vis and not nacional:
            big = max(vis, key=lambda p: np.ptp(p[:, 0]) * np.ptp(p[:, 1]))
            cx, cy = big.mean(0)
            if x0 + 60 < cx < x1 - 60 and y0 + 25 < cy < y1 - 25:
                txt(ax, cx, cy, nombre.split("/")[0].upper(), size=14, color="#7d838c", z=2.5)

    mias = [p for n in propias for p in pos.get(n, [])]
    for p in mias:
        ax.add_patch(MPoly(p, fc="#fffef8", ec="none", zorder=2))
    clip_patch = PathPatch(path_from(mias), transform=ax.transData, fc="none", ec="none")
    ax.add_patch(clip_patch)

    # zonas de aviso
    for z in zonas.values():
        for poly in z["polys"]:
            pp = proj([c[1] for c in poly], [c[0] for c in poly])
            if nacional:
                pa = MPoly(pp, fc=NIVEL_COL[z["nivel"]], ec=NIVEL_COL[z["nivel"]], alpha=0.55, lw=0.6, zorder=3)
                ax.add_patch(pa)
                pa.set_clip_path(clip_patch)
            else:
                stroke(ax, pp, color="#555a63", lw=1.8, amp=1.2, closed=True, z=3.5, doble=False, ls=(0, (5, 4)), clip=clip_patch)
                scribble_fill(ax, pp, NIVEL_COL[z["nivel"]], spacing=10, lw=3.4, clip=clip_patch, alpha=0.75, z=3)
    for p in mias:
        stroke(ax, p, color=NEGRO, lw=3.4 if nacional else 4.0, amp=1.6, closed=True, z=6)
    if nacional:
        txt(ax, *proj(-14.8 + 8.0, 28.3 + 6.4)[0] + [0, -48], "CANARIAS", size=13, color="#7d838c", z=6)

    # ciudades con colocación sin solapes
    puntos = {n: proj(lon, lat)[0] for n, (lon, lat, d) in ciudades.items()}
    ocupado = [(px - 12, py - 12, px + 12, py + 12) for px, py in puntos.values()]
    for nombre, (lon, lat, d) in ciudades.items():
        cx, cy = puntos[nombre]
        elegido = None
        for lado, dy in ((1, 0), (-1, 0), (1, 52), (-1, 52), (1, -52), (-1, -52), (1, 104), (-1, -104)):
            bx = (cx + 6, cx + 200) if lado == 1 else (cx - 200, cx - 6)
            cand = (bx[0], cy + dy - 30, bx[1], cy + dy + 32)
            dentro = x0 + 4 <= cand[0] and cand[2] <= x1 - 4 and y0 + 4 <= cand[1] and cand[3] <= y1 - 4
            if dentro and not any(_solapa(cand, o) for o in ocupado):
                elegido = (lado, dy, cand)
                break
        if elegido is None:
            lado, dy = 1, 0
            cand = (cx + 6, cy - 30, cx + 178, cy + 32)
        else:
            lado, dy, cand = elegido
        ocupado.append(cand)
        magnet(ax, cx, cy, ROJO if d["prob"] >= 50 else AZUL if d["prob"] >= 20 else VERDE, 7)
        icon(ax, d["kind"], cx + lado * 32, cy + dy + 20, 0.55)
        tx = cx + lado * (120 if lado == 1 or True else 120)
        pill(ax, tx, cy + dy + 14, 112, 24, "#ffffff", z=8)
        txt(ax, tx, cy + dy + 14, nombre, size=14, z=9)
        txt(ax, tx, cy + dy - 14, f"{d['tmin']}°/{d['tmax']}°  {d['prob']}%", size=13, color=AZUL if d["prob"] >= 30 else NEGRO, z=9)


# ───────────────────────── datos → resumen ─────────────────────────
def _iso(s):
    return datetime.fromisoformat(s)


def avisos_activos(alertas, ndias):
    ahora = datetime.now(timezone.utc)
    limite = aemet.hoy_madrid().date() + timedelta(days=ndias - 1)
    return [a for a in alertas if a["nivel"] in NIVEL_COL and _iso(a["expires"]) > ahora and _iso(a["onset"]).date() <= limite]


def agrupar(activos):
    g = {}
    for a in activos:
        g.setdefault((a["fenomeno"], a["nivel"], a["onset"], a["expires"]), []).append(a["zona"])
    return g


def firma_avisos(grupos):
    return "|".join(sorted(f"{k[0]}:{k[1]}:{k[2]}:{k[3]}:{','.join(sorted(set(v)))}" for k, v in grupos.items()))


def resumen_alertas(grupos, maximo=6):
    """Texto corto de los avisos para el pie de foto."""
    filas = sorted(grupos.items(), key=lambda kv: (-NIVEL_RANK[kv[0][1]], kv[0][2]))
    out = []
    for (fen, nivel, onset, expires), zonas in filas[:maximo]:
        o, e = _iso(onset), _iso(expires)
        zs = sorted(set(z.replace("Costa - ", "") for z in zonas))
        out.append(f"• {fen} ({nivel}) {DIAS[o.weekday()]} {o.day} {o.hour:02d}h → {DIAS[e.weekday()]} {e.day} {e.hour:02d}h: "
                   + ", ".join(zs[:4]) + ("…" if len(zs) > 4 else ""))
    if len(filas) > maximo:
        out.append(f"…y {len(filas) - maximo} más")
    return "\n".join(out)


def preparar(clave, ndias, demo=False):
    cc = C.CCAA[clave]
    pred, elab = {}, []
    for nombre in cc["ciudades"]:
        try:
            f = aemet.forecast(cc["ciudades"][nombre][0])
            pred[nombre] = f["dias"]
            elab.append(f.get("elaborado", ""))
        except Exception as e:  # un municipio caído no tumba el mapa
            print("aviso: sin datos de", nombre, e)
    if not pred:
        raise RuntimeError("AEMET no devolvió ninguna predicción")
    activos = avisos_activos(aemet.alertas(cc["area"]), ndias)
    if demo:
        activos.append(dict(nivel="amarillo", fenomeno="Lluvias", zona="Demo", cod="000000", polys=[],
                            onset="2026-10-17T06:00:00+02:00", expires="2026-10-18T23:59:00+02:00"))
    nd = min(ndias, min(len(v) for v in pred.values()))
    semana = []
    for i in range(nd):
        pc = {n: day_summary(v[i]) for n, v in pred.items()}
        ref = pc[next(iter(pc))]
        semana.append(dict(ref, region_prob=max(d["prob"] for d in pc.values()),
                           lluvia_en=[n for n, d in pc.items() if d["prob"] >= 40], n_ciudades=len(pc)))
    hoy = {n: day_summary(v[0]) for n, v in pred.items()}
    return cc, pred, hoy, semana, activos, max(elab or [""])


# ───────────────────────── lluvia de hoy por horas ─────────────────────────
EXTRA_HORAS = 350


def _periodos(lista, minimo):
    """probPrecipitacion/probTormenta horarias: periodo '1420' = de 14 a 20 h."""
    out = []
    for p in lista or []:
        try:
            v = int(p["value"])
        except (ValueError, KeyError):
            continue
        per = p["periodo"]
        if v >= minimo and len(per) == 4:
            out.append((int(per[:2]), int(per[2:]), v))
    return out


def horas_24h(cc, minimo=20):
    """Desglose horario de las próximas 24 h de la ciudad de referencia, solo si hay probabilidad de lluvia (>= minimo %)."""
    try:
        ref = next(iter(cc["ciudades"]))
        dias = aemet.horaria(cc["ciudades"][ref][0])["dias"]
    except Exception as e:
        print("aviso: sin predicción horaria", e)
        return None
    ahora = aemet.hoy_madrid().replace(tzinfo=None, minute=0, second=0, microsecond=0)
    fin = ahora + timedelta(hours=24)
    horas, probs, tormentas = [], [], []
    for dia in dias:
        f0 = datetime.fromisoformat(dia["fecha"][:10])
        cielo = {int(e["periodo"]): sky_kind(e["value"]) for e in dia.get("estadoCielo", []) if e.get("value") and e["periodo"].isdigit()}
        for p in dia.get("precipitacion", []):
            try:
                h = int(p["periodo"])
                dt = f0 + timedelta(hours=h)
                if ahora <= dt < fin:
                    horas.append((dt, float(p["value"] or 0), cielo.get(h, "nuboso")))
            except ValueError:
                pass
        for lista, destino, mn in ((dia.get("probPrecipitacion"), probs, minimo), (dia.get("probTormenta"), tormentas, 30)):
            for h0, h1, v in _periodos(lista, mn):
                ini = f0 + timedelta(hours=h0)
                fin_p = f0 + timedelta(hours=h1 if h1 > h0 else h1 + 24)
                if fin_p > ahora and ini < fin:
                    destino.append((ini, h0, h1, v))
    horas.sort()
    if not horas or not probs:
        return None
    return dict(ciudad=ref, horas=horas, prob=probs, tormenta=tormentas, ahora=ahora)


def _fmt_h(dt, ahora):
    return f"{dt.hour} h" if dt.date() == ahora.date() else f"{DIAS[dt.weekday()]} {dt.hour} h"


def _rangos(dts, ahora):
    out, ini, ant = [], None, None
    for d in dts:
        if ini is None:
            ini = ant = d
        elif d - ant == timedelta(hours=1):
            ant = d
        else:
            out.append((ini, ant)); ini = ant = d
    if ini is not None:
        out.append((ini, ant))
    return [f"a las {a.hour} h" + ("" if a.date() == ahora.date() else f" ({DIAS[a.weekday()]})") if a == b else f"de {_fmt_h(a, ahora)} a {_fmt_h(b + timedelta(hours=1), ahora)}" for a, b in out]


def panel_horas(ax, datos, y0):
    """Franja inferior: barras de lluvia por hora (mm) + iconos de cielo + resumen en texto, de ahora a +24 h."""
    horas, ahora = datos["horas"], datos["ahora"]
    mojadas = [d for d, mm, k in horas if mm >= 0.1 or k in ("lluvia", "tormenta", "llovizna")]
    total = sum(mm for _, mm, _ in horas)
    pico = max(horas, key=lambda x: x[1])
    txt(ax, 56, y0 + 325, f"LLUVIA EN LAS PRÓXIMAS 24 H · {datos['ciudad']}", size=27, ha="left", color=AZUL)
    stroke(ax, [[56, y0 + 297], [56 + 700, y0 + 297]], color=AZUL, lw=4, amp=1.5)
    if mojadas:
        rangos = _rangos(mojadas, ahora)
        res = "Llueve " + " y ".join(rangos[:3]) + (" …" if len(rangos) > 3 else "")
        if pico[1] >= 0.1:
            res += f" · máx {pico[1]:g} mm/h ({_fmt_h(pico[0], ahora)}) · total ~{total:.1f} mm"
    else:
        res = "Sin lluvia apreciable en las próximas 24 h"
    txt(ax, 56, y0 + 262, res, size=21, ha="left", color=NEGRO)

    def tramo(ini, h0, h1, v, pref=""):
        return f"{pref}{v}% ({_fmt_h(ini, ahora).replace(' h', '')}–{h1:02d} h)"
    pr = "  ·  ".join(tramo(i, a, b, v) for i, a, b, v in datos["prob"])
    tor = "  ·  ".join(tramo(i, a, b, v, "tormenta ") for i, a, b, v in datos["tormenta"])
    linea = "Probabilidad: " + pr + (f"   |   {tor}" if tor else "")
    txt(ax, 56, y0 + 230, linea if len(linea) < 150 else linea[:147] + "…", size=15, ha="left", color="#555a63")
    n = len(horas)
    x_ini, x_fin = 110, W - 70
    slot = min(120, (x_fin - x_ini) / n)
    base, hmax = y0 + 70, 70
    mmax = max(2.0, pico[1])
    stroke(ax, [[x_ini - 30, base], [x_ini + slot * n + 10, base]], color=NEGRO, lw=3, amp=1.0, doble=False)
    for i, (dt, mm, kind) in enumerate(horas):
        cx = x_ini + slot * (i + 0.5)
        icon(ax, kind, cx, y0 + 178, min(0.5, slot / 135))
        if mm > 0:
            hb = max(8, mm / mmax * hmax)
            bw = slot * 0.55
            caja = [[cx - bw / 2, base], [cx + bw / 2, base], [cx + bw / 2, base + hb], [cx - bw / 2, base + hb]]
            ax.add_patch(MPoly(caja, fc="#8fb3e0", ec="none", zorder=5))
            stroke(ax, caja, color=AZUL, lw=2.6, amp=0.8, closed=True, z=6, doble=False)
            txt(ax, cx, base + hb + 13, f"{mm:g}", size=11.5 if slot < 90 else 13, color=AZUL)
        txt(ax, cx, base - 20, f"{dt.hour}", size=15, color=NEGRO)
        if i == 0 or dt.hour == 0:
            txt(ax, cx, base - 42, f"{DIAS[dt.weekday()]} {dt.day}", size=13, color=ROJO)
            if dt.hour == 0 and i > 0:
                stroke(ax, [[cx - slot / 2, base - 30], [cx - slot / 2, y0 + 150]], color="#9aa0a8", lw=2, amp=0.5, doble=False, ls=(0, (4, 4)))
    txt(ax, x_ini - 55, base - 20, "hora", size=12, color="#7d838c")
    txt(ax, x_ini - 55, base + hmax / 2, "mm/h", size=12, color="#7d838c")


# ───────────────────────── composición ─────────────────────────
LAYOUT = {
    "tall": dict(box=(40, 40, 860, 1160), pbox=(70, 70, 850, 1130), logo=(905, 975), bub=(1170, 1875), fs=22, pitch=36, wrap=47,
                 av=(890, 815), av_w=1880, sem=(890, 1880, 655, 600, 70), compact=False),
    "wide": dict(box=(40, 560, 1000, 1165), pbox=(60, 575, 985, 1150), logo=(1030, 1000), bub=(1290, 1875), fs=20, pitch=32, wrap=41,
                 av=(1030, 765), av_w=1880, sem=(40, 1880, 520, 460, 60), compact=True),
}


def elegir_layout(lim):
    (lo0, lo1), (la0, la1) = lim
    return "wide" if (lo1 - lo0) * 0.79 / (la1 - la0) > 1.2 else "tall"


def presentador(ax, lay, texto):
    logo = cargar_logo()
    lh = 150
    lw_ = lh * logo.shape[1] / logo.shape[0]
    lx, ly = lay["logo"]
    ax.imshow(logo, extent=(lx, lx + lw_, ly, ly + lh), zorder=9, interpolation="bilinear", aspect="auto")
    bx0, bx1 = lay["bub"]
    by1 = 1165
    lineas = textwrap.wrap(texto, lay["wrap"])
    by0 = by1 - 40 - lay["pitch"] * len(lineas)
    caja = [[bx0, by0], [bx1, by0], [bx1, by1], [bx0, by1]]
    ty = ly + 75
    ax.add_patch(MPoly(caja, fc="#ffffff", ec="none", alpha=0.92, zorder=6))
    ax.add_patch(MPoly([[bx0 + 2, ty + 25], [bx0 - 40, ty - 5], [bx0 + 2, ty - 15]], fc="#ffffff", ec="none", alpha=0.92, zorder=6))
    stroke(ax, caja, color=NEGRO, lw=3.4, amp=1.6, closed=True, z=7)
    stroke(ax, [[bx0, ty + 25], [bx0 - 40, ty - 5], [bx0, ty - 15]], color=NEGRO, lw=3.4, amp=0.8, z=7, doble=False)
    y = by1 - 34
    for linea in lineas:
        txt(ax, bx0 + 24, y, linea, size=lay["fs"], ha="left", color=NEGRO, z=9)
        y -= lay["pitch"]
    return by0


def bloque_avisos(ax, lay, grupos):
    ax_x, top = lay["av"]
    ancho_zonas = 70 if lay["av_w"] - ax_x > 900 else 55
    txt(ax, ax_x, top, "AVISOS", size=27, ha="left", color=ROJO)
    stroke(ax, [[ax_x, top - 28], [ax_x + 150, top - 28]], color=ROJO, lw=4, amp=1.5)
    if not grupos:
        pill(ax, ax_x + 290, top, 220, 36, "#bfe8c9")
        txt(ax, ax_x + 290, top, "ninguno activo", size=20, color=VERDE)
        txt(ax, ax_x + 430, top, "(nivel amarillo o superior)", size=15, ha="left", color="#555a63")
        return
    filas = sorted(grupos.items(), key=lambda kv: (-NIVEL_RANK[kv[0][1]], kv[0][2]))[:3]
    yy = top
    for (fen, nivel, onset, expires), zonas in filas:
        o, e = _iso(onset), _iso(expires)
        pill(ax, ax_x + 290, yy, 120, 30, NIVEL_COL[nivel])
        txt(ax, ax_x + 290, yy, nivel.upper(), size=14, weight="bold")
        txt(ax, ax_x + 370, yy, f"{fen} · {DIAS[o.weekday()]} {o.day} {o.hour:02d}h a {DIAS[e.weekday()]} {e.day} {e.hour:02d}h",
            size=17, ha="left", color=NEGRO)
        zs = ", ".join(sorted(set(z.replace("Costa - ", "") for z in zonas)))
        txt(ax, ax_x + 370, yy - 22, zs if len(zs) < ancho_zonas else zs[:ancho_zonas - 3] + "…", size=12.5, ha="left", color="#555a63")
        yy -= 46
    if len(grupos) > 3:
        txt(ax, ax_x + 150, yy + 8, f"…y {len(grupos) - 3} más (aemet.es)", size=14, ha="left", color="#555a63")


def bloque_semana(ax, lay, semana, grupos):
    sx, ex, ty, top, bot = lay["sem"]
    compact = lay["compact"]
    n = len(semana)
    cw = (ex - 40 - sx) / n if ex - sx > 1000 else (ex - sx) / n
    cw = (ex - sx - (0 if ex - sx < 1000 else 0)) / n
    txt(ax, sx + 10 if compact else sx, ty, "PRÓXIMOS DÍAS", size=27, ha="left", color=AZUL)
    stroke(ax, [[sx + (10 if compact else 0), ty - 28], [sx + (10 if compact else 0) + 330, ty - 28]], color=AZUL, lw=4, amp=1.5)
    llueve = [s for s in semana if s["region_prob"] >= 40 or s["kind"] in ("lluvia", "tormenta", "llovizna")]
    dd = lambda s: DIAS[_iso(s["fecha"]).weekday()]
    msg = f"Paraguas: {', '.join(dd(s) for s in llueve)}" if llueve else "Sin lluvia prevista"
    txt(ax, sx + (370 if compact else 360), ty, msg, size=19, ha="left", color=AZUL if llueve else VERDE)
    avisos_dia = {}
    for (fen, nivel, onset, expires) in grupos:
        d0, d1 = _iso(onset).date(), _iso(expires).date()
        for s in semana:
            if d0 <= _iso(s["fecha"]).date() <= d1:
                avisos_dia.setdefault(s["fecha"], []).append((fen, nivel))
    off = dict(dn=26, dt=54, ic=112, tmax=172, tmin=208, by=246, pct=270) if compact else \
        dict(dn=30, dt=62, ic=150, tmax=235, tmin=275, by=322, pct=346)
    for i, s in enumerate(semana):
        cx = sx + cw * (i + 0.5)
        f = _iso(s["fecha"])
        lluvia = s in llueve
        av = avisos_dia.get(s["fecha"])
        box = [[cx - cw / 2 + 8, bot], [cx + cw / 2 - 8, bot], [cx + cw / 2 - 8, top], [cx - cw / 2 + 8, top]]
        clip = Rectangle((cx - cw / 2 + 8, bot), cw - 16, top - bot, transform=ax.transData)
        if lluvia:
            scribble_fill(ax, box, "#9cc7f2", spacing=9, lw=3, angle=-1.2, alpha=0.5, z=1.2, clip=clip)
        if av:
            scribble_fill(ax, box, "#f08080", spacing=16, lw=3, angle=0.6, alpha=0.30, z=1.3, clip=clip)
        stroke(ax, box, color=ROJO if av else AZUL if lluvia else NEGRO, lw=4.2 if av else 3.0, amp=1.4, closed=True, z=4)
        txt(ax, cx, top - off["dn"], DIAS[f.weekday()].upper(), size=24, color=ROJO if f.weekday() >= 5 else NEGRO)
        txt(ax, cx, top - off["dt"], f"{f.day} {MESES[f.month - 1]}", size=16, color="#555a63")
        icon(ax, s["kind"], cx, top - off["ic"], 0.85 if compact else 1.0)
        txt(ax, cx, top - off["tmax"], f"{s['tmax']}°", size=32 if compact else 34, color=ROJO)
        txt(ax, cx, top - off["tmin"], f"{s['tmin']}°", size=22 if compact else 24, color=AZUL)
        bw, bh, by = min(cw - 46, 190), 20, top - off["by"]
        ax.add_patch(Rectangle((cx - bw / 2, by), bw, bh, fc="#ffffff", ec=NEGRO, lw=2.4, zorder=6))
        ax.add_patch(Rectangle((cx - bw / 2, by), bw * s["region_prob"] / 100, bh, fc=AZUL if s["region_prob"] >= 40 else "#8fb3e0",
                               ec="none", zorder=6.1))
        txt(ax, cx, top - off["pct"], f"{s['region_prob']}% lluvia", size=15, color=AZUL if s["region_prob"] >= 40 else NEGRO)
        if s["lluvia_en"] and not compact:
            txt(ax, cx, top - off["pct"] - 24, f"{len(s['lluvia_en'])}/{s['n_ciudades']} municipios", size=12.5, color="#555a63")
        if av:
            av.sort(key=lambda a: -NIVEL_RANK[a[1]])
            vistos = set()
            av = [a for a in av if not (a[0] in vistos or vistos.add(a[0]))]
            pb = bot + (70 if compact else 90)
            pill(ax, cx, pb, min(cw - 36, 200), 34, ROJO)
            txt(ax, cx, pb, "¡AVISO!", size=19, color="#ffffff", weight="bold")
            for k, (fen, nivel) in enumerate(av[:2]):
                txt(ax, cx, pb - 32 - k * 22, fen.split()[0][:10], size=14, color=ROJO)


def postprocesar(fig, salida):
    """Grano, tinta irregular, brillo de pizarra y marco de aluminio."""
    fig.canvas.draw()
    img = np.asarray(fig.canvas.buffer_rgba())[..., :3].astype(float)
    h, w = img.shape[:2]
    tinta = np.clip((BG - img).max(axis=2) / 140, 0, 1)[..., None]
    falla = gaussian_filter(rng.random((h, w)), 1.1)
    falla = (falla - falla.mean()) / falla.std()
    img = BG - (BG - img) * np.clip(0.9 + 0.1 * falla[..., None], 0.55, 1.0)
    ghost = gaussian_filter(rng.random((h, w)), 40)
    ghost = (ghost - ghost.min()) / np.ptp(ghost)
    img = img - 6 * ghost[..., None] * (1 - tinta)
    img = img + 5.5 * rng.normal(0, 1, (h, w))[..., None] + 12.0 * gaussian_filter(rng.normal(0, 1, (h, w)), 1.6)[..., None]
    yy, xx = np.mgrid[0:h, 0:w]
    img = img + (np.exp(-(((xx - yy * 0.9) - w * 0.45) / (w * 0.16)) ** 2) * 9)[..., None] * (1 - tinta)
    img = img * (1 - 0.07 * (((xx - w / 2) / (w / 2)) ** 2 + ((yy - h / 2) / (h / 2)) ** 2))[..., None]
    out = Image.fromarray(np.clip(img, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.5))
    arr = np.asarray(out).astype(float)
    b = 14
    for i in range(b):
        c = 150 + 90 * i / b
        arr[i, :] = c; arr[h - 1 - i, :] = c * 0.9; arr[:, i] = c; arr[:, w - 1 - i] = c * 0.9
    Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8)).save(salida)


def lienzo(extra=0):
    """Lienzo de 1920x1200; con extra > 0 añade una franja inferior (coordenadas y negativas)."""
    fig = plt.figure(figsize=(W / 100, (H + extra) / 100), dpi=100, facecolor="#f4f4f0")
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, W); ax.set_ylim(-extra, H); ax.axis("off")
    for _ in range(26 + extra // 50):
        x, y = rng.uniform(0, W), rng.uniform(-extra, H)
        p = wobble([[x, y], [x + rng.uniform(80, 320), y + rng.uniform(-60, 60)]], amp=6, step=20)
        ax.plot(p[:, 0], p[:, 1], color="#c8ccd2", lw=rng.uniform(10, 26), alpha=0.18, solid_capstyle="round", zorder=0)
    return fig, ax


def decoraciones(ax, nota="Datos: AEMET OpenData", y0=0):
    for k, c in enumerate((NEGRO, AZUL, ROJO, VERDE)):
        ax.add_patch(FancyBboxPatch((1000 + k * 60, y0 + 12), 44, 14, boxstyle="round,pad=0,rounding_size=6", fc=c, ec="#00000066", lw=1.2, zorder=9))
    for (mx, my, c) in ((24, 1176, ROJO), (1896, 1176, AZUL), (24, y0 + 24, VERDE), (1896, y0 + 24, AMARILLO)):
        magnet(ax, mx, my, c, 14)
    txt(ax, W - 40, y0 + 20, nota, size=12, ha="right", color="#8a8f97")


def generar_ccaa(clave, ndias, salida, demo=False):
    """Genera la imagen de una comunidad. Devuelve (grupos de avisos, firma)."""
    cc, pred, hoy, semana, activos, elab = preparar(clave, ndias, demo)
    grupos = agrupar(activos)
    por_zona = {}
    for a in activos:
        if a["cod"] not in por_zona or NIVEL_RANK[a["nivel"]] > NIVEL_RANK[por_zona[a["cod"]]["nivel"]]:
            por_zona[a["cod"]] = a
    gp = prov_geo()
    lim = limites(gp, cc["provincias"])
    lay = LAYOUT[elegir_layout(lim)]
    horas = horas_24h(cc)
    extra = EXTRA_HORAS if horas else 0
    fig, ax = lienzo(extra)
    proj = Proyeccion(lay["pbox"], *lim)
    ciudades = {n: (cc["ciudades"][n][1], cc["ciudades"][n][2], hoy[n]) for n in hoy}
    dibujar_mapa(ax, proj, gp, cc["provincias"], ciudades, por_zona, lay["box"], cc["mar"])
    fecha = _iso(semana[0]["fecha"])
    txt(ax, 56, 1150, f"{cc['nombre']} · {DIAS_L[fecha.weekday()]} {fecha.day} {MESES[fecha.month - 1]}", size=28, ha="left", color=ROJO, z=9)
    presentador(ax, lay, resumen(semana, grupos))
    bloque_avisos(ax, lay, grupos)
    bloque_semana(ax, lay, semana, grupos)
    try:
        ed = datetime.fromisoformat(elab)
        nota = f"Datos: AEMET · predicción de {ed.day}/{ed.month} {ed:%H:%M}"
    except ValueError:
        nota = "Datos: AEMET OpenData"
    if horas:
        panel_horas(ax, horas, -extra)
    decoraciones(ax, nota, y0=-extra)
    postprocesar(fig, salida)
    plt.close(fig)
    return grupos, firma_avisos(grupos)


def generar_nacional(ndias, salida):
    """Mapa de España con las zonas de aviso y el texto general de AEMET."""
    activos = avisos_activos(aemet.alertas("esp"), ndias)
    por_zona = {}
    for a in activos:
        k = (a["cod"], )
        if k not in por_zona or NIVEL_RANK[a["nivel"]] > NIVEL_RANK[por_zona[k]["nivel"]]:
            por_zona[k] = a
    por_ccaa = {}
    for a in activos:
        nom = C.AREAS.get(a["cod"][:2], "Otras")
        d = por_ccaa.setdefault(nom, dict(nivel="amarillo", fen=set()))
        if NIVEL_RANK[a["nivel"]] > NIVEL_RANK[d["nivel"]]:
            d["nivel"] = a["nivel"]
        d["fen"].add(a["fenomeno"])
    gp = prov_geo()
    todas = list(gp)
    lim = limites(gp, todas, nacional=True)
    fig, ax = lienzo()
    proj = ProyeccionNac((60, 80, 850, 1120), *lim)
    dibujar_mapa(ax, proj, gp, todas, {}, por_zona, (40, 40, 860, 1160), True, nacional=True)
    hoy = aemet.hoy_madrid()
    txt(ax, 56, 1150, f"España · {DIAS_L[hoy.weekday()]} {hoy.day} {MESES[hoy.month - 1]}", size=28, ha="left", color=ROJO, z=9)
    texto = aemet.texto_nacional()
    if len(texto) > 560:
        texto = texto[:560].rsplit(".", 1)[0] + "."
    lay = dict(LAYOUT["tall"], wrap=47, pitch=33, fs=20)
    by0 = presentador(ax, lay, texto or "AEMET no ha publicado texto general.")
    y0 = by0 - 50
    txt(ax, 890, y0, f"AVISOS (próx. {ndias} días)", size=27, ha="left", color=ROJO)
    stroke(ax, [[890, y0 - 28], [890 + 330, y0 - 28]], color=ROJO, lw=4, amp=1.5)
    if not por_ccaa:
        pill(ax, 1180, y0 - 70, 220, 36, "#bfe8c9")
        txt(ax, 1180, y0 - 70, "ninguno activo", size=20, color=VERDE)
    filas = sorted(por_ccaa.items(), key=lambda kv: (-NIVEL_RANK[kv[1]["nivel"]], kv[0]))
    for i, (nom, d) in enumerate(filas[:16]):
        col, fila = divmod(i, 8)
        x = 890 + col * 500
        y = y0 - 75 - fila * 52
        pill(ax, x + 22, y, 44, 28, NIVEL_COL[d["nivel"]])
        txt(ax, x + 60, y + 6, nom, size=18, ha="left")
        fen = ", ".join(sorted(d["fen"]))
        txt(ax, x + 60, y - 14, fen if len(fen) < 52 else fen[:49] + "…", size=12.5, ha="left", color="#555a63")
    decoraciones(ax)
    postprocesar(fig, salida)
    plt.close(fig)
    return por_ccaa


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ccaa", default="murcia", choices=sorted(C.CCAA))
    ap.add_argument("--nacional", action="store_true")
    ap.add_argument("--dias", type=int, default=7, choices=(3, 7))
    ap.add_argument("--demo", action="store_true", help="simula un aviso amarillo (solo para probar el diseño)")
    ap.add_argument("--salida", default=str(HERE / "tiempo.png"))
    a = ap.parse_args()
    if a.nacional:
        generar_nacional(a.dias, a.salida)
    else:
        g, _ = generar_ccaa(a.ccaa, a.dias, a.salida, a.demo)
        print(resumen_alertas(g) or "Sin avisos")
    print("Guardado:", a.salida)
