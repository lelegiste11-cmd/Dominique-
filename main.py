import os
import asyncio
import re
import logging
import sys
from datetime import datetime, timedelta, timezone
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from aiohttp import web
from config import (
    API_ID, API_HASH, BOT_TOKEN, ADMIN_ID,
    SOURCE_CHANNEL_ID, PREDICTION_CHANNEL_ID, PORT,
    SUIT_MAPPING, ALL_SUITS, SUIT_DISPLAY
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
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

# Configuration de la prédiction (configurable par l'admin)
PREDICTION_OFFSET = int(os.getenv('PREDICTION_OFFSET', '2'))  # 'a' dans N+a

pending_predictions = {}
queued_predictions = {}
recent_games = {}
processed_messages = set()
last_transferred_game = None
current_game_number = 0

MAX_PENDING_PREDICTIONS = 2
PROXIMITY_THRESHOLD = 3

source_channel_ok = False
prediction_channel_ok = False

# Mapping des couleurs avec noms complets
SUIT_NAMES = {
    '♠️': 'Pique',
    '♠': 'Pique',
    '♥️': 'Cœur',
    '♥': 'Cœur',
    '❤️': 'Cœur',
    '❤': 'Cœur',
    '♦️': 'Carreaux',
    '♦': 'Carreaux',
    '♣️': 'Trèfle',
    '♣': 'Trèfle'
}

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

def get_suits_in_group(group_str: str):
    normalized = normalize_suits(group_str)
    return [s for s in ALL_SUITS if s in normalized]

def get_first_suit(group_str: str):
    """Récupère la première couleur trouvée dans le groupe"""
    normalized = normalize_suits(group_str)
    for char in normalized:
        if char in ALL_SUITS:
            return SUIT_DISPLAY.get(char, char)
    return None

def get_suit_name(suit: str) -> str:
    """Retourne le nom complet de la couleur"""
    return SUIT_NAMES.get(suit, suit)

def count_cards(group_str: str) -> int:
    normalized = normalize_suits(group_str)
    return sum(normalized.count(s) for s in ALL_SUITS)

def has_suit_in_group(group_str: str, target_suit: str) -> bool:
    normalized = normalize_suits(group_str)
    target_normalized = normalize_suits(target_suit)
    for suit in ALL_SUITS:
        if suit in target_normalized and suit in normalized:
            return True
    return False

async def send_prediction_to_channel(target_game: int, suit: str, base_game: int, immediate: bool = False):
    """Envoie une prédiction au canal"""
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
                logger.info(f"✅ Prédiction envoyée au canal de prédiction {PREDICTION_CHANNEL_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur envoi prédiction au canal: {e}")
        else:
            logger.warning(f"⚠️ Canal de prédiction non accessible, prédiction non envoyée")

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
    """Met à jour le statut d'une prédiction"""
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
                logger.info(f"✅ Prédiction #{game_number} mise à jour dans le canal: {new_status}")
            except Exception as e:
                logger.error(f"❌ Erreur mise à jour dans le canal: {e}")

        pred['status'] = new_status
        logger.info(f"Prédiction #{game_number} mise à jour: {new_status}")

        if new_status in ['✅0️⃣', '✅1️⃣', '✅2️⃣', '❌']:
            del pending_predictions[game_number]
            logger.info(f"Prédiction #{game_number} terminée et supprimée")

        return True

    except Exception as e:
        logger.error(f"Erreur mise à jour prédiction: {e}")
        return False

def is_message_finalized(message: str) -> bool:
    """Vérifie si un message est finalisé (pour la vérification)"""
    if '⏰' in message:
        return False
    return '✅' in message or '🔰' in message

def is_message_valid_for_prediction(message: str) -> bool:
    """Vérifie si un message est valide pour lancer une prédiction (même si non finalisé)"""
    # On accepte les messages avec ⏰ (en cours) pour la prédiction
    # mais ils doivent avoir la structure correcte
    return '#N' in message or '#n' in message

async def check_prediction_result(game_number: int, first_group: str):
    """Vérifie le résultat d'une prédiction"""
    # Vérifier la prédiction pour ce numéro
    if game_number in pending_predictions:
        pred = pending_predictions[game_number]
        target_suit = pred['suit']

        if has_suit_in_group(first_group, target_suit):
            await update_prediction_status(game_number, '✅0️⃣')
            logger.info(f"Prédiction #{game_number} réussie immédiatement!")
            return True
        else:
            pred['check_count'] = 1
            logger.info(f"Prédiction #{game_number}: couleur non trouvée, attente du jeu suivant")
            return False

    # Vérifier la prédiction pour le numéro précédent (jeu +1)
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
                logger.info(f"Prédiction #{prev_game}: couleur non trouvée au +1, attente +2")
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
    """
    Traite un message pour la prédiction ou la vérification
    is_finalized: True si le message est finalisé (✅ ou 🔰), False si en cours (⏰)
    """
    global last_transferred_game, current_game_number
    try:
        game_number = extract_game_number(message_text)
        if game_number is None:
            return

        current_game_number = game_number

        # Éviter le traitement multiple du même message
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

        logger.info(f"Jeu #{game_number} traité (finalisé={is_finalized}) - G1: {first_group}, G2: {second_group}")

        # Transfert des messages finalisés
        if is_finalized and transfer_enabled and ADMIN_ID and ADMIN_ID != 0 and last_transferred_game != game_number:
            try:
                transfer_msg = f"📨 **Message finalisé du canal source:**\n\n{message_text}"
                await client.send_message(ADMIN_ID, transfer_msg)
                last_transferred_game = game_number
                logger.info(f"✅ Message finalisé #{game_number} transféré à l'admin {ADMIN_ID}")
            except Exception as e:
                logger.error(f"❌ Erreur transfert à l'admin: {e}")

        # VÉRIFICATION: uniquement sur messages finalisés
        if is_finalized:
            await check_prediction_result(game_number, first_group)

        # PRÉDICTION: sur tous les messages valides (même non finalisés)
        # La nouvelle règle: analyser le deuxième groupe de parenthèses
        first_suit_second_group = get_first_suit(second_group)

        if first_suit_second_group:
            target_game = game_number + PREDICTION_OFFSET

            # Vérifier si une prédiction existe déjà pour ce numéro
            if target_game not in pending_predictions:
                logger.info(f"🎯 Nouvelle prédiction déclenchée: Jeu #{game_number} -> Prédiction #{target_game}, couleur: {first_suit_second_group}")
                await send_prediction_to_channel(target_game, first_suit_second_group, game_number)
            else:
                logger.info(f"Prédiction #{target_game} existe déjà, ignorée")

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

        logger.info(f"Message reçu de chat_id={chat_id}, attendu={SOURCE_CHANNEL_ID}")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message du canal source: {message_text[:80]}...")

            # Déterminer si le message est finalisé
            is_finalized = is_message_finalized(message_text)

            # Traiter pour prédiction (même si non finalisé) et vérification (si finalisé)
            await process_message_for_prediction(message_text, chat_id, is_finalized)

    except Exception as e:
        logger.error(f"Erreur handle_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.MessageEdited())
async def handle_edited_message(event):
    try:
        chat = await event.get_chat()
        chat_id = chat.id if hasattr(chat, 'id') else event.chat_id

        if chat_id > 0 and hasattr(chat, 'broadcast') and chat.broadcast:
            chat_id = -1000000000000 - chat_id

        logger.info(f"Message édité de chat_id={chat_id}, attendu={SOURCE_CHANNEL_ID}")

        if chat_id == SOURCE_CHANNEL_ID:
            message_text = event.message.message
            logger.info(f"Message édité dans canal source: {message_text[:80]}...")

            # Déterminer si le message est finalisé
            is_finalized = is_message_finalized(message_text)

            # Traiter pour prédiction et vérification
            await process_message_for_prediction(message_text, chat_id, is_finalized)

    except Exception as e:
        logger.error(f"Erreur handle_edited_message: {e}")
        import traceback
        logger.error(traceback.format_exc())

@client.on(events.NewMessage(pattern='/start'))
async def cmd_start(event):
    if event.is_group or event.is_channel:
        return

    logger.info(f"Commande /start reçue de {event.sender_id}")
    await event.respond("""🤖 **Bot de Prédiction Baccarat**

Ce bot surveille un canal source et envoie des prédictions automatiques.

**Commandes:**
• `/status` - Voir les prédictions en cours
• `/help` - Aide détaillée
• `/debug` - Informations de débogage
• `/checkchannels` - Vérifier l'accès aux canaux
• `/setoffset <nombre>` - Définir le décalage de prédiction (défaut: 2)""")

@client.on(events.NewMessage(pattern='/status'))
async def cmd_status(event):
    if event.is_group or event.is_channel:
        return

    logger.info(f"Commande /status reçue de {event.sender_id}")

    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return

    status_msg = f"📊 **État des prédictions:**\n\n"
    status_msg += f"🎮 Jeu actuel: #{current_game_number}\n"
    status_msg += f"📐 Décalage de prédiction: +{PREDICTION_OFFSET}\n\n"

    if pending_predictions:
        status_msg += f"**🔮 Prédictions actives ({len(pending_predictions)}):**\n"
        for game_num, pred in sorted(pending_predictions.items()):
            distance = game_num - current_game_number
            status_msg += f"• Jeu #{game_num}: {pred['suit']} ({pred['suit_name']}) - Statut: {pred['status']} (dans {distance} jeux)\n"
    else:
        status_msg += "**🔮 Aucune prédiction active**\n"

    await event.respond(status_msg)

@client.on(events.NewMessage(pattern='/setoffset'))
async def cmd_setoffset(event):
    """Commande pour changer le décalage de prédiction"""
    global PREDICTION_OFFSET

    if event.is_group or event.is_channel:
        return

    if event.sender_id != ADMIN_ID and ADMIN_ID != 0:
        await event.respond("Commande réservée à l'administrateur")
        return

    try:
        message_text = event.message.message
        parts = message_text.split()

        if len(parts) < 2:
            await event.respond(f"Usage: `/setoffset <nombre>`\nValeur actuelle: {PREDICTION_OFFSET}")
            return

        new_offset = int(parts[1])
        if new_offset < 1 or new_offset > 10:
            await event.respond("Le décalage doit être entre 1 et 10")
            return

        PREDICTION_OFFSET = new_offset
        logger.info(f"Décalage de prédiction changé à {PREDICTION_OFFSET} par {event.sender_id}")
        await event.respond(f"✅ Décalage de prédiction défini à: **{PREDICTION_OFFSET}**\n\nLes prédictions seront maintenant envoyées pour N+{PREDICTION_OFFSET}")

    except ValueError:
        await event.respond("Veuillez entrer un nombre valide. Exemple: `/setoffset 3`")
    except Exception as e:
        logger.error(f"Erreur setoffset: {e}")
        await event.respond(f"Erreur: {str(e)}")

@client.on(events.NewMessage(pattern='/debug'))
async def cmd_debug(event):
    if event.is_group or event.is_channel:
        return

    logger.info(f"Commande /debug reçue de {event.sender_id}")

    debug_msg = f"""🔍 **Informations de débogage:**

**Configuration:**
• Source Channel: {SOURCE_CHANNEL_ID}
• Prediction Channel: {PREDICTION_CHANNEL_ID}
• Admin ID: {ADMIN_ID}
• Décalage prédiction: +{PREDICTION_OFFSET}

**Accès aux canaux:**
• Canal source: {'✅ OK' if source_channel_ok else '❌ Non accessible'}
• Canal prédiction: {'✅ OK' if prediction_channel_ok else '❌ Non accessible'}

**État:**
• Jeu actuel: #{current_game_number}
• Prédictions actives: {len(pending_predictions)}
• Port: {PORT}

**Règles actuelles:**
• Prédiction déclenchée par: 2ème groupe de parenthèses
• Décalage: N + {PREDICTION_OFFSET}
• Couleur prédite: 1ère couleur du 2ème groupe
• Vérification: sur messages finalisés uniquement"""

    await event.respond(debug_msg)

@client.on(events.NewMessage(pattern='/checkchannels'))
async def cmd_checkchannels(event):
    global source_channel_ok, prediction_channel_ok

    if event.is_group or event.is_channel:
        return

    logger.info(f"Commande /checkchannels reçue de {event.sender_id}")

    await event.respond("🔍 Vérification des accès aux canaux...")

    result_msg = "📡 **Résultat de la vérification:**\n\n"

    try:
        source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
        source_title = getattr(source_entity, 'title', 'N/A')
        source_channel_ok = True
        result_msg += f"✅ **Canal source** ({SOURCE_CHANNEL_ID}):\n"
        result_msg += f"   Nom: {source_title}\n"
        result_msg += f"   Statut: Accessible\n\n"
        logger.info(f"✅ Canal source accessible: {source_title}")
    except Exception as e:
        source_channel_ok = False
        result_msg += f"❌ **Canal source** ({SOURCE_CHANNEL_ID}):\n"
        result_msg += f"   Erreur: {str(e)[:100]}\n"
        result_msg += f"   Action: Ajoutez le bot comme membre du canal\n\n"
        logger.error(f"❌ Canal source non accessible: {e}")

    pred_title = "Inconnu"
    try:
        try:
            pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
            pred_title = getattr(pred_entity, 'title', 'N/A')
        except Exception as entity_err:
            logger.warning(f"get_entity a échoué, tentative d'envoi direct...")
            try:
                test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test...")
                await asyncio.sleep(1)
                await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
                pred_title = getattr(pred_entity, 'title', 'N/A')
            except:
                raise entity_err

        try:
            test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test de permissions...")
            await asyncio.sleep(1)
            await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
            prediction_channel_ok = True
            result_msg += f"✅ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
            result_msg += f"   Nom: {pred_title}\n"
            result_msg += f"   Statut: Accessible avec droits d'écriture\n\n"
            logger.info(f"✅ Canal prédiction accessible avec droits: {pred_title}")
        except Exception as write_error:
            prediction_channel_ok = False
            result_msg += f"⚠️ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
            result_msg += f"   Nom: {pred_title}\n"
            result_msg += f"   Erreur écriture: {str(write_error)[:50]}\n"
            result_msg += f"   Action: Le bot doit être ADMINISTRATEUR du canal\n\n"
            logger.warning(f"⚠️ Canal prédiction sans droits d'écriture: {write_error}")
    except Exception as e:
        prediction_channel_ok = False
        me = await client.get_me()
        bot_username = getattr(me, 'username', 'votre_bot')
        result_msg += f"❌ **Canal prédiction** ({PREDICTION_CHANNEL_ID}):\n"
        result_msg += f"   Erreur: {str(e)[:80]}\n"
        result_msg += f"   Action: Ajoutez @{bot_username} comme ADMINISTRATEUR du canal\n\n"
        logger.error(f"❌ Canal prédiction non accessible: {e}")

    if source_channel_ok and prediction_channel_ok:
        result_msg += "🎉 **Tout est prêt!** Le bot peut fonctionner normalement."
    else:
        result_msg += "⚠️ **Actions requises** pour que le bot fonctionne correctement."

    await event.respond(result_msg)

transfer_enabled = True

@client.on(events.NewMessage(pattern='/transfert'))
async def cmd_transfert(event):
    if event.is_group or event.is_channel:
        return

    global transfer_enabled
    transfer_enabled = True
    logger.info(f"Transfert activé par {event.sender_id}")
    await event.respond("✅ Transfert des messages finalisés activé!\n\nVous recevrez tous les messages finalisés du canal source.")

@client.on(events.NewMessage(pattern='/activetransfert'))
async def cmd_active_transfert(event):
    if event.is_group or event.is_channel:
        return

    global transfer_enabled
    if transfer_enabled:
        await event.respond("✅ Le transfert est déjà activé!")
    else:
        transfer_enabled = True
        logger.info(f"Transfert réactivé par {event.sender_id}")
        await event.respond("✅ Transfert réactivé avec succès!")

@client.on(events.NewMessage(pattern='/stoptransfert'))
async def cmd_stop_transfert(event):
    if event.is_group or event.is_channel:
        return

    global transfer_enabled
    transfer_enabled = False
    logger.info(f"Transfert désactivé par {event.sender_id}")
    await event.respond("⛔ Transfert des messages désactivé.")

@client.on(events.NewMessage(pattern='/help'))
async def cmd_help(event):
    if event.is_group or event.is_channel:
        return

    logger.info(f"Commande /help reçue de {event.sender_id}")

    await event.respond(f"""📖 **Aide - Bot de Prédiction**

**Nouvelle règle de prédiction:**
1. Le bot surveille le canal source
2. **Dès réception** d'un message (même avec ⏰), analyse le **2ème groupe** de parenthèses
3. Identifie la **première couleur** dans ce 2ème groupe
4. Prédit le jeu **N + {PREDICTION_OFFSET}** avec cette couleur
5. **La vérification** attend toujours que le message soit finalisé (✅ ou 🔰)

**Exemple:**
```
#N1100. ✅8(K♥️J♦️8♥️) - 2(7♣️5♣️10♥️) #T10
```
→ 2ème groupe: `7♣️5♣️10♥️`
→ Première couleur: `♣️` (trèfle)
→ Prédiction: Jeu **#1102** (1100 + {PREDICTION_OFFSET}) en **♣️**

**Format de prédiction:**
```
📡 PRÉDICTION #1102
🎯 Couleur: ♣️ Trèfle
🌪️ Statut: ⏳ EN COURS
```

**Résultats possibles:**
• **✅0️⃣** = Couleur trouvée au numéro prédit (N+{PREDICTION_OFFSET})
• **✅1️⃣** = Couleur trouvée au numéro +1
• **✅2️⃣** = Couleur trouvée au numéro +2
• **❌** = Échec après 3 tentatives

**Commandes:**
• `/start` - Démarrer le bot
• `/status` - Voir les prédictions en cours
• `/setoffset <n>` - Changer le décalage (défaut: 2)
• `/checkchannels` - Vérifier l'accès aux canaux
• `/transfert` - Activer transfert des messages
• `/stoptransfert` - Désactiver le transfert
• `/debug` - Informations de débogage

**Configuration:**
Le décalage de prédiction (N+a) peut être modifié avec `/setoffset`. Par défaut a={PREDICTION_OFFSET}.""")

async def index(request):
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Bot Prédiction Baccarat</title>
        <meta charset="utf-8">
    </head>
    <body>
        <h1>🎯 Bot de Prédiction Baccarat</h1>
        <p>Le bot est en ligne et surveille les canaux.</p>
        <p><strong>Jeu actuel:</strong> #{current_game_number}</p>
        <p><strong>Prédictions actives:</strong> {len(pending_predictions)}</p>
        <p><strong>Décalage de prédiction:</strong> +{PREDICTION_OFFSET}</p>
        <ul>
            <li><a href="/health">Health Check</a></li>
            <li><a href="/status">Statut (JSON)</a></li>
        </ul>
    </body>
    </html>
    """
    return web.Response(text=html, content_type='text/html', status=200)

async def health_check(request):
    return web.Response(text="OK", status=200)

async def status_api(request):
    status_data = {
        "status": "running",
        "source_channel": SOURCE_CHANNEL_ID,
        "source_channel_ok": source_channel_ok,
        "prediction_channel": PREDICTION_CHANNEL_ID,
        "prediction_channel_ok": prediction_channel_ok,
        "current_game": current_game_number,
        "prediction_offset": PREDICTION_OFFSET,
        "pending_predictions": len(pending_predictions),
        "recent_games": len(recent_games),
        "timestamp": datetime.now().isoformat()
    }
    return web.json_response(status_data)

async def start_web_server():
    app = web.Application()
    app.router.add_get('/', index)
    app.router.add_get('/health', health_check)
    app.router.add_get('/status', status_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Serveur web démarré sur 0.0.0.0:{PORT}")

async def start_bot():
    global source_channel_ok, prediction_channel_ok
    try:
        logger.info("Démarrage du bot...")
        await client.start(bot_token=BOT_TOKEN)
        logger.info("Bot Telegram connecté")

        session = client.session.save()
        logger.info(f"Session Telegram: {session[:50]}... (sauvegardez ceci dans TELEGRAM_SESSION)")

        me = await client.get_me()
        username = getattr(me, 'username', 'Unknown') or f"ID:{getattr(me, 'id', 'Unknown')}"
        logger.info(f"Bot opérationnel: @{username}")

        logger.info("Tentative de découverte des canaux...")

        try:
            source_entity = await client.get_entity(SOURCE_CHANNEL_ID)
            source_channel_ok = True
            logger.info(f"✅ Accès au canal source confirmé: {getattr(source_entity, 'title', 'N/A')}")
        except Exception as e:
            source_channel_ok = False
            logger.error(f"❌ Impossible d'accéder au canal source: {e}")
            logger.error("Le bot doit être ajouté comme membre du canal source!")

        try:
            try:
                pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
                logger.info(f"✅ Accès au canal de prédiction: {getattr(pred_entity, 'title', 'N/A')}")
            except Exception as entity_err:
                logger.warning(f"⚠️ get_entity a échoué, tentative d'envoi direct...")
                try:
                    test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🔍 Test de connexion...")
                    await asyncio.sleep(1)
                    await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                    pred_entity = await client.get_entity(PREDICTION_CHANNEL_ID)
                    logger.info(f"✅ Canal découvert via envoi: {getattr(pred_entity, 'title', 'N/A')}")
                except Exception as send_err:
                    raise entity_err

            try:
                test_msg = await client.send_message(PREDICTION_CHANNEL_ID, "🤖 Bot connecté et prêt à envoyer des prédictions!")
                await asyncio.sleep(2)
                await client.delete_messages(PREDICTION_CHANNEL_ID, test_msg.id)
                prediction_channel_ok = True
                logger.info("✅ Permissions d'écriture confirmées dans le canal de prédiction")
            except Exception as write_err:
                prediction_channel_ok = False
                logger.error(f"❌ Pas de droits d'écriture dans le canal de prédiction: {write_err}")
                logger.error("Le bot doit être ADMINISTRATEUR du canal de prédiction!")
        except Exception as e:
            prediction_channel_ok = False
            logger.error(f"❌ Impossible d'accéder au canal de prédiction: {e}")
            logger.error(f"⚠️ Assurez-vous d'ajouter @{username} comme ADMINISTRATEUR du canal de prédiction!")

        logger.info(f"Surveillance du canal source: {SOURCE_CHANNEL_ID}")
        logger.info(f"Envoi des prédictions vers: {PREDICTION_CHANNEL_ID}")
        logger.info(f"📋 Nouvelle règle: Prédiction basée sur 2ème groupe, décalage +{PREDICTION_OFFSET}")

        return True
    except Exception as e:
        logger.error(f"Erreur démarrage: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False

async def main():
    try:
        await start_web_server()

        success = await start_bot()
        if not success:
            logger.error("Échec du démarrage du bot")
            return

        logger.info("Bot complètement opérationnel - En attente de messages...")
        await client.run_until_disconnected()

    except Exception as e:
        logger.error(f"Erreur dans main: {e}")
        import traceback
        logger.error(traceback.format_exc())
    finally:
        await client.disconnect()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté par l'utilisateur")
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        import traceback
        logger.error(traceback.format_exc())
