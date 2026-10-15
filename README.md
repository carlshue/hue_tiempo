# El tiempo en Murcia (pizarra cartoony)

Genera `tiempo_murcia.png` con datos de AEMET OpenData y lo envía por Telegram.

## Local
    pip install -r requirements.txt
    python mapa_tiempo_murcia.py --refresh          # solo la imagen
    python enviar_telegram.py                       # imagen + envío (usa las variables de abajo)

## Variables de entorno (en local, fichero `.env`; en Railway, pestaña Variables)
- `AEMET_API_KEY`
- `TELEGRAM_BOT_TOKEN` (de @BotFather)
- `TELEGRAM_CHAT_ID` (tu id; escribe al bot y mira https://api.telegram.org/bot<TOKEN>/getUpdates)

## Railway
Servicio desde este repo, Start Command `python enviar_telegram.py`, Cron Schedule `0 6 * * *`
(UTC; 08:00 en horario de verano de Madrid, 07:00 en invierno).
