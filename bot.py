#!/usr/bin/env python3
"""Bot de Telegram del tiempo (long polling + planificador de envíos).

Cada usuario elige: comunidad autónoma, modo (diario / solo adversos), horizonte (3 o 7 días),
si quiere también el mapa de España y la hora de envío. Se guarda en SQLite (db.py).

Variables: TELEGRAM_BOT_TOKEN, AEMET_API_KEY, DATA_DIR (carpeta persistente), DB_PATH (opcional)
Comandos: /start /ajustes /ahora /parar /reanudar /borrar /ayuda
"""
import os
import threading
import time
import traceback

import requests

import aemet
import ccaa as C
import db
import render

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
URL = f"https://api.telegram.org/bot{TOKEN}"
SALIDAS = aemet.DATA / "salidas"
SALIDAS.mkdir(parents=True, exist_ok=True)
LOCK_RENDER = threading.Lock()  # matplotlib no es thread-safe
HORAS = (7, 8, 9, 13, 20)


# ───────────────────────── Telegram ─────────────────────────
def tg(metodo, **params):
    try:
        r = requests.post(f"{URL}/{metodo}", json=params, timeout=70)
        return r.json()
    except Exception as e:
        print("tg error", metodo, e)
        return {}


def enviar_foto(chat, ruta, caption=""):
    with open(ruta, "rb") as f:
        r = requests.post(f"{URL}/sendPhoto", data={"chat_id": chat, "caption": caption[:1000]}, files={"photo": f}, timeout=90)
    if not r.ok:
        print("sendPhoto falló", r.status_code, r.text[:200])
    return r.ok


# ───────────────────────── pantallas de ajustes ─────────────────────────
def _fila(botones, n=2):
    return [botones[i:i + n] for i in range(0, len(botones), n)]


def teclado(pantalla, u, wiz):
    w = "1" if wiz else "0"
    if pantalla == "ccaa":
        b = [{"text": c["nombre"], "callback_data": f"s:ccaa:{k}:{w}"} for k, c in sorted(C.CCAA.items(), key=lambda kv: kv[1]["nombre"])]
        return _fila(b, 2)
    if pantalla == "modo":
        return [[{"text": "Cada día", "callback_data": f"s:modo:diario:{w}"}],
                [{"text": "Solo si hay avisos (tiempo adverso)", "callback_data": f"s:modo:adversos:{w}"}]]
    if pantalla == "dias":
        return [[{"text": "Próximos 3 días", "callback_data": f"s:dias:3:{w}"}, {"text": "Próximos 7 días", "callback_data": f"s:dias:7:{w}"}]]
    if pantalla == "nac":
        return [[{"text": "Solo mi comunidad", "callback_data": f"s:nacional:0:{w}"}],
                [{"text": "Mi comunidad + España", "callback_data": f"s:nacional:1:{w}"}]]
    if pantalla == "hora":
        return [[{"text": f"{h:02d}:00", "callback_data": f"s:hora:{h}:{w}"} for h in HORAS]]
    # menú
    return [[{"text": "Comunidad", "callback_data": "p:ccaa"}, {"text": "Modo", "callback_data": "p:modo"}],
            [{"text": "Días", "callback_data": "p:dias"}, {"text": "España", "callback_data": "p:nac"}],
            [{"text": "Hora", "callback_data": "p:hora"},
             {"text": "▶ Reanudar" if not u.get("activo") else "⏸ Pausar", "callback_data": "s:activo:" + ("1" if not u.get("activo") else "0") + ":0"}],
            [{"text": "Enviar ahora", "callback_data": "ahora"}]]


TEXTOS = {
    "ccaa": "¿De qué comunidad autónoma quieres el tiempo?",
    "modo": "¿Cuándo quieres que te escriba?",
    "dias": "¿Cuántos días de previsión (y de vigilancia de avisos)?",
    "nac": "¿Quieres también el mapa de España?",
    "hora": "¿A qué hora (hora de Madrid)?",
}
SIGUIENTE = {"ccaa": "modo", "modo": "dias", "dias": "nac", "nac": "hora", "hora": None}


def resumen_ajustes(u):
    cc = C.CCAA.get(u.get("ccaa"), {}).get("nombre", "sin elegir")
    modo = "cada día" if u["modo"] == "diario" else "solo si hay avisos"
    estado = "activo" if u["activo"] else "en pausa"
    return (f"Tus ajustes ({estado}):\n• Comunidad: {cc}\n• Envío: {modo} a las {u['hora']:02d}:00\n"
            f"• Horizonte: {u['dias']} días\n• Mapa de España: {'sí' if u['nacional'] else 'no'}")


def mostrar(chat, pantalla, u, wiz=False, msg=None):
    if pantalla == "menu":
        texto = resumen_ajustes(u) + "\n\nCambia lo que quieras:"
    else:
        texto = TEXTOS[pantalla]
    kb = {"inline_keyboard": teclado(pantalla, u, wiz)}
    if msg:
        r = tg("editMessageText", chat_id=chat, message_id=msg, text=texto, reply_markup=kb)
        if r.get("ok") or "not modified" in str(r):
            return
    tg("sendMessage", chat_id=chat, text=texto, reply_markup=kb)


# ───────────────────────── envío ─────────────────────────
def _imagen(clave, dias, nacional=False):
    """Imagen en caché por hora (la comparten todos los usuarios con los mismos ajustes)."""
    marca = aemet.hoy_madrid().strftime("%Y%m%d%H")
    nombre = f"{'esp' if nacional else clave}_{dias}_{marca}.png"
    ruta = SALIDAS / nombre
    if not ruta.exists():
        with LOCK_RENDER:
            if not ruta.exists():
                for viejo in SALIDAS.glob(f"{'esp' if nacional else clave}_{dias}_*.png"):
                    viejo.unlink(missing_ok=True)
                if nacional:
                    render.generar_nacional(dias, str(ruta))
                else:
                    render.generar_ccaa(clave, dias, str(ruta))
    return ruta


def evaluar(u):
    """Avisos activos de su comunidad en su horizonte: (grupos, firma)."""
    cc = C.CCAA[u["ccaa"]]
    grupos = render.agrupar(render.avisos_activos(aemet.alertas(cc["area"]), u["dias"]))
    return grupos, render.firma_avisos(grupos)


def enviar_a_usuario(u, forzar=False):
    """Devuelve True si ha enviado algo."""
    grupos, firma = evaluar(u)
    hoy = aemet.hoy_madrid().strftime("%Y-%m-%d")
    if u["modo"] == "adversos" and not forzar:
        db.actualizar(u["telegram_id"], ultima_comprob=hoy)
        if not grupos or firma == u.get("ultima_firma"):
            return False  # nada nuevo que contar
    nombre = C.CCAA[u["ccaa"]]["nombre"]
    if grupos:
        cap = f"⚠ Avisos en {nombre} (próx. {u['dias']} días):\n" + render.resumen_alertas(grupos)
    else:
        cap = f"{nombre}: sin avisos de nivel amarillo o superior en los próximos {u['dias']} días."
    ok = enviar_foto(u["telegram_id"], _imagen(u["ccaa"], u["dias"]), cap)
    if u["nacional"]:
        enviar_foto(u["telegram_id"], _imagen(u["ccaa"], u["dias"], nacional=True), "España")
    if ok:
        db.actualizar(u["telegram_id"], ultima_comprob=hoy, ultima_firma=firma)
    return ok


def planificador():
    """Cada 30 s mira quién toca: hora de envío alcanzada y no comprobado hoy."""
    while True:
        try:
            ahora = aemet.hoy_madrid()
            hoy = ahora.strftime("%Y-%m-%d")
            for u in db.activos():
                if ahora.hour >= u["hora"] and u.get("ultima_comprob") != hoy:
                    try:
                        enviar_a_usuario(u)
                    except Exception:
                        traceback.print_exc()
                        db.actualizar(u["telegram_id"], ultima_comprob=hoy)  # no reintentar en bucle
        except Exception:
            traceback.print_exc()
        time.sleep(30)


# ───────────────────────── mensajes ─────────────────────────
AYUDA = ("Soy el bot del tiempo cartoony con datos de AEMET.\n\n"
         "/start – configurar (comunidad, modo, días, España, hora)\n/ajustes – ver o cambiar ajustes\n"
         "/ahora – recibir el mapa ya\n/parar – pausar envíos\n/reanudar – reanudar\n/borrar – borrar mis datos")


def enviar_ahora(chat, u):
    if not u.get("ccaa"):
        tg("sendMessage", chat_id=chat, text="Primero elige tu comunidad con /start")
        return
    tg("sendMessage", chat_id=chat, text="Dibujando el mapa… (unos segundos)")
    try:
        enviar_a_usuario(u, forzar=True)
    except Exception:
        traceback.print_exc()
        tg("sendMessage", chat_id=chat, text="No he podido obtener los datos de AEMET ahora mismo. Inténtalo en unos minutos.")


def on_mensaje(m):
    chat = m["chat"]["id"]
    uid = m["from"]["id"]
    nombre = m["from"].get("first_name", "")
    texto = (m.get("text") or "").split("@")[0].strip().lower()
    u = db.asegurar(uid, nombre)
    if texto in ("/start", "/configurar"):
        tg("sendMessage", chat_id=chat, text="¡Hola! Vamos a configurarlo en unos toques.")
        mostrar(chat, "ccaa", u, wiz=True)
    elif texto == "/ajustes":
        mostrar(chat, "menu", u)
    elif texto == "/ahora":
        enviar_ahora(chat, u)
    elif texto == "/parar":
        db.actualizar(uid, activo=0)
        tg("sendMessage", chat_id=chat, text="Envíos en pausa. /reanudar para volver.")
    elif texto == "/reanudar":
        db.actualizar(uid, activo=1)
        tg("sendMessage", chat_id=chat, text="Envíos reanudados.")
    elif texto == "/borrar":
        db.borrar(uid)
        tg("sendMessage", chat_id=chat, text="He borrado tus datos. Escribe /start si quieres volver.")
    else:
        tg("sendMessage", chat_id=chat, text=AYUDA)


def on_callback(q):
    chat = q["message"]["chat"]["id"]
    msg = q["message"]["message_id"]
    uid = q["from"]["id"]
    tg("answerCallbackQuery", callback_query_id=q["id"])
    u = db.asegurar(uid, q["from"].get("first_name", ""))
    d = q["data"].split(":")
    if d[0] == "p":                                  # abrir pantalla
        mostrar(chat, d[1], u, msg=msg)
    elif d[0] == "s":                                # guardar un valor
        campo, valor, wiz = d[1], d[2], d[3] == "1"
        if campo == "ccaa" and valor not in C.CCAA:
            return
        if campo in ("dias", "nacional", "hora", "activo"):
            valor = int(valor)
        db.actualizar(uid, **{campo: valor})
        if campo in ("ccaa", "modo", "dias", "nacional", "hora"):
            db.actualizar(uid, ultima_firma=None, ultima_comprob=None)  # nuevo ajuste = comprobar de nuevo
        u = db.obtener(uid)
        pant = {"ccaa": "ccaa", "modo": "modo", "dias": "dias", "nacional": "nac", "hora": "hora"}.get(campo)
        sig = SIGUIENTE.get(pant) if wiz else None
        if sig:
            mostrar(chat, sig, u, wiz=True, msg=msg)
        else:
            mostrar(chat, "menu", u, msg=msg)
            if wiz:
                tg("sendMessage", chat_id=chat, text="¡Listo! Te escribiré a la hora elegida. Con /ahora lo recibes ya.")
    elif d[0] == "ahora":
        threading.Thread(target=enviar_ahora, args=(chat, u), daemon=True).start()


def main():
    threading.Thread(target=planificador, daemon=True).start()
    tg("setMyCommands", commands=[{"command": c, "description": t} for c, t in (
        ("start", "Configurar el bot"), ("ajustes", "Ver o cambiar ajustes"), ("ahora", "Recibir el mapa ya"),
        ("parar", "Pausar envíos"), ("reanudar", "Reanudar envíos"), ("borrar", "Borrar mis datos"))])
    print("Bot en marcha")
    offset = None
    while True:
        r = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
        for up in r.get("result", []):
            offset = up["update_id"] + 1
            try:
                if "message" in up and up["message"].get("chat", {}).get("type") == "private":
                    threading.Thread(target=on_mensaje, args=(up["message"],), daemon=True).start()
                elif "callback_query" in up:
                    threading.Thread(target=on_callback, args=(up["callback_query"],), daemon=True).start()
            except Exception:
                traceback.print_exc()
        if not r.get("ok", True):
            time.sleep(5)


if __name__ == "__main__":
    main()
