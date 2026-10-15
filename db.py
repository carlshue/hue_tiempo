"""Base de datos SQLite sencilla: una fila por usuario de Telegram con sus preferencias."""
import os
import sqlite3
import threading
from pathlib import Path

import aemet  # carga .env y define DATA

DB_PATH = Path(os.environ.get("DB_PATH", aemet.DATA / "usuarios.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_lock = threading.Lock()
_con = sqlite3.connect(DB_PATH, check_same_thread=False)
_con.row_factory = sqlite3.Row
_con.execute("""
CREATE TABLE IF NOT EXISTS usuarios (
    telegram_id      INTEGER PRIMARY KEY,   -- id de usuario (= id de chat privado)
    nombre           TEXT,
    ccaa             TEXT,                  -- clave de ccaa.py (murcia, andalucia...)
    modo             TEXT DEFAULT 'diario', -- 'diario' | 'adversos'
    dias             INTEGER DEFAULT 7,     -- horizonte: 3 | 7
    nacional         INTEGER DEFAULT 0,     -- 1 = incluir mapa de España
    hora             INTEGER DEFAULT 8,     -- hora de envío (Europe/Madrid)
    activo           INTEGER DEFAULT 1,
    ultima_comprob   TEXT,                  -- fecha (YYYY-MM-DD) de la última comprobación/envío
    ultima_firma     TEXT,                  -- huella de los avisos ya enviados (modo adversos)
    creado           TEXT DEFAULT CURRENT_TIMESTAMP
)""")
_con.commit()

CAMPOS = {"nombre", "ccaa", "modo", "dias", "nacional", "hora", "activo", "ultima_comprob", "ultima_firma"}


def obtener(uid):
    with _lock:
        r = _con.execute("SELECT * FROM usuarios WHERE telegram_id=?", (uid,)).fetchone()
        return dict(r) if r else None


def asegurar(uid, nombre=""):
    with _lock:
        _con.execute("INSERT OR IGNORE INTO usuarios (telegram_id, nombre) VALUES (?, ?)", (uid, nombre))
        _con.commit()
    return obtener(uid)


def actualizar(uid, **campos):
    campos = {k: v for k, v in campos.items() if k in CAMPOS}
    if not campos:
        return
    with _lock:
        _con.execute(f"UPDATE usuarios SET {', '.join(k + '=?' for k in campos)} WHERE telegram_id=?", (*campos.values(), uid))
        _con.commit()


def activos():
    """Usuarios con configuración completa y envíos activos."""
    with _lock:
        return [dict(r) for r in _con.execute("SELECT * FROM usuarios WHERE activo=1 AND ccaa IS NOT NULL")]


def borrar(uid):
    with _lock:
        _con.execute("DELETE FROM usuarios WHERE telegram_id=?", (uid,))
        _con.commit()
