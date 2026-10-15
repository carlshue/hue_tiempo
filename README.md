# Bot del tiempo cartoony (AEMET → Telegram)

Mapas dibujados "en pizarra magnética" por comunidad autónoma y de España, con avisos de AEMET.
Cada usuario de Telegram configura su comunidad, si quiere aviso diario o solo con tiempo adverso,
horizonte de 3 o 7 días, si quiere además el mapa de España y la hora. Se guarda en SQLite.

## Archivos
- `bot.py` – bot (long polling) + planificador de envíos. **Start command: `python bot.py`**
- `render.py` – dibuja las imágenes (`python render.py --ccaa murcia --dias 7`, `--nacional`, `--demo`)
- `aemet.py` – cliente AEMET con caché; `ccaa.py` – comunidades y municipios; `db.py` – SQLite

## Variables de entorno
- `AEMET_API_KEY`, `TELEGRAM_BOT_TOKEN`
- `DATA_DIR` – carpeta persistente (en Railway: `/data`, con un Volume montado ahí). Guarda `usuarios.db` y la caché.

## Railway
1. Servicio desde este repo, Start Command `python bot.py` (sin cron: es un proceso siempre activo).
2. Añade un Volume montado en `/data` y la variable `DATA_DIR=/data` (sin volumen se pierden los usuarios en cada despliegue).
3. Variables `AEMET_API_KEY` y `TELEGRAM_BOT_TOKEN`.
4. Una sola instancia: dos bots haciendo polling con el mismo token se pisan.

## Modo "solo adversos"
Mira cada día, a la hora elegida, los avisos amarillos o superiores de tu comunidad en tu horizonte y
solo escribe si hay alguno **nuevo** (no repite el mismo aviso cada día).
