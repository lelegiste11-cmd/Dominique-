import os
import asyncio
import re
import logging
import sys
from datetime import datetime
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY, SUIT_NAMES,
    PREDICTION_OFFSET
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

if not API_ID or API_ID == 0:
    logger.error("API_ID manquant")
    exit(1)
if not API_HASH:
    logger.error("API_HASH manquant")
    exit(1)
if not BOT_TOKEN:
    logger.error("BOT_TOKEN manquant")
    exit(1)

logger.info(f"Configuration: SOURCE_CHANNEL={SOURCE_CHANNEL_ID}, PREDICTION_CHANNEL={PREDICTION_CHANNEL_ID}")

session_string = os.getenv('TELEGRAM_SESSION', '')
client = TelegramClient(StringSession(session_string), API_ID, API_HASH)

pending_predictions = {}
recent_games = {}
processed_messages = set()
last_transferred_game = None
current_game_number = 0
transfer_enabled = True

source_channel_ok = False
prediction_channel_ok = False


def extract_game_number(message: str):
    match = re.search(r"#N\s*(\d+)\.?", message, re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def extract_parentheses_groups(message: str):
    return re.findall(r"\(([^)]*)\)", message)


def normalize_suits(group_str: str) -> str:
    normalized = group_str.replace('❤️', '♥').replace('❤', '♥').replace('♥️', '♥')
    normalized = normalized.replace('♠️', '♠').replace('♦️', '♦').replace('♣️', '♣')
    return normalized


def get_first_suit(group_str: str):
    normalized = normalize_suits(group_str)
    for char in normalized:
        if char in ALL_SUITS:
            return SUIT_DISPLAY.get(char, char)
    return None


def get_suit_name(suit: str) -> str:
    return SUIT_NAMES.get(suit, suit)


def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False


def is_message_finalized(message: str) -> bool:
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message


async def send_prediction_to_channel(target_game: int, suit: str, base_game: int):
    try:
        suit_name = get_suit_name(suit)
        prediction_msg = f"""📡 PRÉDICTION #{target_game}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: ⏳ EN COURS"""

        msg_id = 0
        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and prediction_channel_ok:
            try:
                pred_msg = await client.send_message(PREDICTION_CHANNEL_ID, prediction_msg)
                msg_id = pred_msg.id
                logger.info(f"✅ Prédiction envoyée au canal {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction: {e}")

        pending_predictions[target_game] = {
            'message_id': msg_id,
            'suit': suit,
            'suit_name': suit_name,
            'base_game': base_game,
            'status': '⏳ EN COURS',
            'check_count': 0,
            'created_at': datetime.now().isoformat()
        }
        logger.info(f"Prédiction active: Jeu #{target_game} - {suit} (basé sur #{base_game})")
        return msg_id
    except Exception as e:
        logger.error(f"Erreur envoi prédiction: {e}")
        return None


async def update_prediction_status(game_number: int, new_status: str):
    try:
        if game_number not in pending_predictions:
            return False

        pred = pending_predictions[game_number]
        message_id = pred['message_id']
        suit = pred['suit']
        suit_name = pred['suit_name']

        updated_msg = f"""📡 PRÉDICTION #{game_number}
🎯 Couleur: {suit} {suit_name}
🌪️ Statut: {new_status}"""

        if PREDICTION_CHANNEL_ID and PREDICTION_CHANNEL_ID != 0 and message_id > 0 and prediction_channel_ok:
            try:
                await client.edit_message(PREDICTION_CHANNEL_ID, message_id, updated_msg)
                logger.info(f"✅ Prédiction #{game_number} mise à jour: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour: {e}")

        pred['status'] = new_status

        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            del pending_predictions[game_number]
            logger.info(f"Prédiction #{game_number} terminée")

        return True
    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False


async def check_prediction_result(game_number: int, first_group: str):
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        target_suit = pred['suit']
        if has_suit_in_group(first_group, target_suit):
            await update_prediction_status(game_number, '✅0️⃣')
            logger.info(f"Prédiction #{game_number} réussie immédiatement!")
            return True
        else:
            pred['check_count'] = 1
            logger.info(f"Prédiction #{game_number}: couleur non trouvée, attente +1")
            return False

    prev_game = game_number - 1
    if prev_game in pending_predictions:
        pred = pending_predictions[prev_game]
        if pred.get('check_count', 0) == 1:
            target_suit = pred['suit']
            if has_suit_in_group(first_group, target_suit):
                await update_prediction_status(prev_game, '✅1️⃣')
                logger.info(f"Prédiction #{prev_game} réussie au jeu +1!")
                return True
            else:
                pred['check_count'] = 2
                logger.info(f"Prédiction #{prev_game}: non trouvée au +1, attente +2")
                return False
        elif pred.get('check_count', 0) == 2:
            target_suit = pred['suit']
            if has_suit_in_group(first_group, target_suit):
                await update_prediction_status(prev_game, '✅2️⃣')
                logger.info(f"Prédiction #{prev_game} réussie au jeu +2!")
                return True
            else:
                await update_prediction_status(prev_game, '❌')
                logger.info(f"Prédiction #{prev_game} échouée après 3 tentatives")
                return False
    return None


async def process_message_for_prediction(message_text: str, chat_id: int, is_finalized: bool):
    global last_transferred_game, current_game_number
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number
        message_hash = f"{game_number}_{message_text[:50]}_{is_finalized}"
        if message_hash in processed_messages:
            return
        processed_messages.add(message_hash)

        if len(processed_messages) > 200:
            processed_messages.clear()

        groups = extract_parentheses_groups(message_text)
        if len(groups) < 2:
            return

        first_group = groups[0]
        second_group = groups[1]

        logger.info(f"Jeu #{game_number} (finalisé={is_finalized}) - G2: {second_group}")

        if is_finalized and transfer_enabled and ADMIN_ID and ADMIN_ID != 0 and last_transferred_game != game_number:
            try:
                transfer_msg = f"📨 Message finalisé #{game_number}:\n\n{message_text}"
                await client.send_message(ADMIN_ID, transfer_msg)
                last_transferred_game = game_number
                logger.info(f"✅ Message #{game_number} transféré à l'admin")
            except Exception as e:
                logger.error(f"❌ Erreur transfert: {e}")

        if is_finalized:
            await check_prediction_result(game_number, first_group)

        first_suit_second_group = get_first_suit(second_group)
        if first_suit_second_group:
            target_game = game_number + PREDICTION_OFFSET
            if target_game not in pending_predictions:
                logger.info(f"🎯 Prédiction: #{game_number} -> #{target_game}, couleur: {first_suit_second_group}")
                await send_prediction_to_channel(target_game, first_suit_second_group, game_number)

    except Exception as e:
        logger.error(f"Erreur traitement message: {e}")
        import traceback
        logger.error(traceback.format_exc())


@client.on(events.NewMessage())
async def handle_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            is_finalized = is_message_finalized(message_text)
            await process_message_for_prediction(message_text, chat_id, is_finalized)
    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")


@client.on(events.MessageEdited())
async def handle_edited_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id
        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            is_finalized = is_message_finalized(message_text)
            await process_message_for_prediction(message_text, chat_id, is_finalized)
    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")


@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return
    await event.respond("""🤖 Bot de Prédiction Baccarat

Commandes:
• /status - Voir les prédictions
• /setoffset <n> - Changer décalage (défaut: 2)
• /checkchannels - Vérifier accès canaux
• /debug - Informations système
• /help - Aide complète""")


@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'admin")
        return

    status_msg = f"📊 État:\n🎮 Jeu actuel: #{current_game_number}\n📐 Décalage: +{PREDICTION_OFFSET}\n\n"
    if pending_predictions:
        status_msg += f"🔮 Prédictions actives ({len(pending_predictions)}):\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            status_msg += f"• #{game_num}: {pred['suit']} - {pred['status']} (dans {distance})\n"
    else:
        status_msg += "🔮 Aucune prédiction active"
    await event.respond(status_msg)


@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    global PREDICTION_OFFSET
    if event.is_group or event.is_channel:
        return
    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'admin")
        return

    try:
        parts = event.message.message.split()
        if len(parts) < 2:
            await event.respond(f"Usage: /setoffset <nombre>\nActuel: {PREDICTION_OFFSET}")
            return
        new_offset = int(parts[1])
        if new_offset < 1 or new_offset > 10:
            await event.respond("Le décalage doit être entre 1 et 10")
            return
        PREDICTION_OFFSET = new_offset
        logger.info(f"Décalage changé à {PREDICTION_OFFSET}")
        await event.respond(f"✅ Décalage défini à: {PREDICTION_OFFSET}")
    except ValueError:
        await event.respond("Veuillez entrer un nombre valide")


@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return
    debug_msg = f"""🔍 Debug:
• Source: {SOURCE_CHANNEL_ID} {'✅' if source_channel_ok else '❌'}
• Prédiction: {PREDICTION_CHANNEL_ID} {'✅' if prediction_channel_ok else '❌'}
• Admin: {ADMIN_ID}
• Décalage: +{PREDICTION_OFFSET}
• Jeu actuel: #{current_game_number}
• Prédictions actives: {len(pending_predictions)}
• Port: {PORT}"""
    await event.respond(debug_msg)


@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok
    if event.is_group or event.is_channel:
        return
    await event.respond("🔍 Vérification des canaux...")
    result_msg = ""

    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
        source_channel_ok = True
        result_msg += f"✅ Source: {getattr(source_entity, 'title', 'N/A')}\n"
    except Exception as e:
        source_channel_ok = False
        result_msg += f"❌ Source: {str(e)[:50]}\n"

    try:
        pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
        test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test...")
        await asyncio.sleep(1)
        await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
        prediction_channel_ok = True
        result_msg += f"✅ Prédiction: {getattr(pred_entity, 'title', 'N/A')}"
    except Exception as e:
        prediction_channel_ok = False
        result_msg += f"❌ Prédiction: {str(e)[:50]}"

    await event.respond(result_msg)


@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    global transfer_enabled
    if event.is_group or event.is_channel:
        return
    transfer_enabled = True
    await event.respond("✅ Transfert activé")


@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stop_transfert(event):
    global transfer_enabled
    if event.is_group or event.is_channel:
        return
    transfer_enabled = False
    await event.respond("⛔ Transfert désactivé")


@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return
    await event.respond(f"""📖 Aide

Nouvelle règle:
1. Analyse le 2ème groupe de parenthèses
2. Prend la 1ère couleur
3. Prédit N + {PREDICTION_OFFSET}

Exemple:
#N1100. ✅8(K♥️J♦️8♥️) - 2(7♣️5♣️10♥️)
→ 2ème groupe: 7♣️5♣️10♥️
→ 1ère couleur: ♣️
→ Prédiction: #1102

Statuts:
• ⏳ EN COURS → En attente
• ✅0️⃣ → Réussite immédiate
• ✅1️⃣ → Réussite au +1
• ✅2️⃣ → Réussite au +2
• ❌ → Échec""")


async def index(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><title>Bot Prédiction Baccarat</title></head>
    <body>
        <h1>🎯 Bot de Prédiction Baccarat</h1>
        <p>Status: ✅ En ligne</p>
        <p>Jeu actuel: #{current_game_number}</p>
        <p>Prédictions actives: {len(pending_predictions)}</p>
        <p>Décalage: +{PREDICTION_OFFSET}</p>
        <p>Port: {PORT}</p>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html', status=200)


async def health_check(request):
    return web.Response(text="OK", status=200)


async def status_api(request):
    return web.json_response({
        "status": "running",
        "source_channel": SOURCE_CHANNEL_ID,
        "source_channel_ok": source_channel_ok,
        "prediction_channel": PREDICTION_CHANNEL_ID,
        "prediction_channel_ok": prediction_channel_ok,
        "current_game": current_game_number,
        "prediction_offset": PREDICTION_OFFSET,
        "pending_predictions": len(pending_predictions),
        "timestamp": datetime.now().isoformat()
    })


async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"✅ Serveur web démarré sur port {PORT}")


async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("🚀 Démarrage du bot...")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("✅ Bot Telegram connecté")

        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown')
        logger.info(f"✅ Bot opérationnel: @{username}")

        # Vérifier canaux
        try:
            await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info("✅ Canal source accessible")
        except Exception as e:
            logger.error(f"❌ Canal source inaccessible: {e}")

        try:
            await client.get_entity(PREDICTION_CHANNEL_ID)
            test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 Bot démarré!")
            await asyncio.sleep(1)
            await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
            prediction_channel_ok = True
            logger.info("✅ Canal prédiction accessible")
        except Exception as e:
            logger.error(f"❌ Canal prédiction inaccessible: {e}")

        return True
    except Exception as e:
        logger.error(f"❌ Erreur démarrage: {e}")
        return False


async def main():
    await start_web_server()
    success = await start_bot()
    if not success:
        logger.error("Échec du démarrage")
        return
    logger.info("✅ Bot complètement opérationnel")
    await client.run_until_disconnected()


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
