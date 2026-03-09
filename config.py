"""
Configuration du bot Telegram de prédiction Baccarat
"""
import os


def parse_channel_id(env_var: str, default: str) -> int:
    value = os.getenv(env_var) or default
    channel_id = int(value)
    if channel_id > 0 and len(str(channel_id)) >= 10:
        channel_id = -channel_id
    return channel_id


# ==========================================
# IDs des canaux Telegram
# ==========================================
SOURCE_CHANNEL_ID = parse_channel_id('SOURCE_CHANNEL_ID', '-1002682552255')
PREDICTION_CHANNEL_ID = parse_channel_id('PREDICTION_CHANNEL_ID', '-1003746077228')

# ==========================================
# Informations d'identification Telegram
# ==========================================
ADMIN_ID = int(os.getenv('ADMIN_ID') or '6180384006')
API_ID = int(os.getenv('API_ID') or '2917761')
API_HASH = os.getenv('API_HASH') or 'a8639172fa8d35dbfd8ea46286d349ab'
BOT_TOKEN = os.getenv('BOT_TOKEN') or '8674132351:AAG3ReHocLGtPxkLPl2TOEDtNRn1lea7Dzg'

# ==========================================
# Configuration serveur (Render.com utilise le port 10000)
# ==========================================
PORT = int(os.getenv('PORT') or '10000')

# ==========================================
# Configuration de prédiction
# ==========================================
PREDICTION_OFFSET = int(os.getenv('PREDICTION_OFFSET') or '2')

# ==========================================
# Mapping des couleurs pour la prédiction
# ==========================================
SUIT_MAPPING = {
    '♠️': '❤️',
    '♠': '❤️',
    '❤️': '♠️',
    '❤': '♠️',
    '♥️': '♠️',
    '♥': '♠️',
    '♣️': '♦️',
    '♣': '♦️',
    '♦️': '♣️',
    '♦': '♣️'
}

ALL_SUITS = ['♠', '♥', '♦', '♣']
SUIT_DISPLAY = {
    '♠': '♠️',
    '♥': '❤️',
    '♦': '♦️',
    '♣': '♣️'
}

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
