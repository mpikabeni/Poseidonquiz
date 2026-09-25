import os
import random
import logging
import sqlite3
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes
)

# --- RECUPÉRATION DES VARIABLES D'ENVIRONNEMENT (RENDER) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATABASE_FILE = "scores_quiz.db"

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- BASE DE DONNÉES SQLITE ---

def init_db():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    # Table des utilisateurs et points
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            username TEXT,
            points INTEGER DEFAULT 0
        )
    ''')
    
    # Table des groupes où le bot est installé
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT
        )
    ''')
    
    # Table des quiz actifs
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS active_polls (
            poll_id TEXT PRIMARY KEY,
            correct_option_id INTEGER
        )
    ''')
    conn.commit()
    conn.close()

def enregistrer_groupe(chat_id: int, title: str):
    """Enregistre le groupe dans la BDD s'il n'existe pas encore."""
    # On n'enregistre que les groupes et supergroupes (pas les discussions privées)
    if chat_id < 0:
        conn = sqlite3.connect(DATABASE_FILE)
        cursor = conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO groups (chat_id, title) VALUES (?, ?)", (chat_id, title))
        conn.commit()
        conn.close()

def obtenir_tous_les_groupes():
    """Récupère la liste de tous les IDs de groupes enregistrés."""
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT chat_id FROM groups")
    groupes = [row[0] for row in cursor.fetchall()]
    conn.close()
    return groupes

def enregistrer_sondage(poll_id: str, correct_option_id: int):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("INSERT OR REPLACE INTO active_polls (poll_id, correct_option_id) VALUES (?, ?)", 
                   (poll_id, correct_option_id))
    conn.commit()
    conn.close()

def ajouter_point(user_id: int, full_name: str, username: str):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO users (user_id, full_name, username, points)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id) DO UPDATE SET
            points = points + 1,
            full_name = excluded.full_name,
            username = excluded.username
    ''', (user_id, full_name, username))
    conn.commit()
    conn.close()

def obtenir_top10():
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT full_name, points FROM users ORDER BY points DESC LIMIT 10")
    top = cursor.fetchall()
    conn.close()
    return top

def obtenir_points_user(user_id: int):
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT points FROM users WHERE user_id = ?", (user_id,))
    res = cursor.fetchone()
    conn.close()
    return res[0] if res else 0

# --- GÉNÉRATION ET ENVOI DE QUIZ ---

def generer_quiz_anime():
    try:
        page = random.randint(1, 10)
        url = f"https://api.jikan.moe/v4/top/anime?page={page}"
        response = requests.get(url, timeout=10)
        
        if response.status_code == 200:
            data = response.json().get('data', [])
            if not data:
                return None
            
            anime = random.choice(data)
            title = anime.get('title')
            genres = [g['name'] for g in anime.get('genres', [])]

            if genres:
                correct = genres[0]
                pool = ["Action", "Romance", "Horreur", "Comédie", "Sci-Fi", "Fantasy", "Aventure", "Drame"]
                fausses = [g for g in pool if g not in genres][:3]
                options = [correct] + fausses
                random.shuffle(options)
                
                return {
                    "question": f"Quel est le genre principal de l'anime '{title}' ?",
                    "options": options,
                    "correct_id": options.index(correct)
                }
    except Exception as e:
        logging.error(f"Erreur API Jikan: {e}")
    return None

async def envoyer_sondage(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    quiz = generer_quiz_anime()
    if quiz:
        message = await context.bot.send_poll(
            chat_id=chat_id,
            question=quiz["question"],
            options=quiz["options"],
            type="quiz",
            correct_option_id=quiz["correct_id"],
            is_anonymous=False
        )
        enregistrer_sondage(message.poll.id, quiz["correct_id"])

# --- TÂCHE PROGRAMMÉE (Envoi automatique à TOUS les groupes) ---

async def quiz_automatique(context: ContextTypes.DEFAULT_TYPE):
    """Envoie un quiz automatiquement dans tous les groupes enregistrés."""
    groupes = obtenir_tous_les_groupes()
    logging.info(f"Début de l'envoi du quiz automatique à {len(groupes)} groupe(s).")
    
    for chat_id in groupes:
        try:
            await envoyer_sondage(context, chat_id)
        except Exception as e:
            logging.error(f"Impossible d'envoyer le quiz au groupe {chat_id}: {e}")

# --- HANDLERS ET COMMANDES ---

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Enregistre le groupe s'il s'agit d'un groupe
    enregistrer_groupe(update.effective_chat.id, update.effective_chat.title or "Groupe")
    await update.message.reply_text(
        "👋 Bienvenue ! Je suis le Bot Quiz Anime.\n\n"
        "• Utilisez `/quiz` pour lancer un quiz instantané.\n"
        "• Utilisez `/top` pour voir le classement général.\n"
        "• Un quiz sera automatiquement posté dans ce groupe toutes les heures !"
    )

async def cmd_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Enregistre le groupe au passage
    enregistrer_groupe(update.effective_chat.id, update.effective_chat.title or "Groupe")
    await update.message.reply_text("🎲 Recherche d'un quiz...")
    await envoyer_sondage(context, update.effective_chat.id)

async def gerer_reponse_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    answer = update.poll_answer
    poll_id = answer.poll_id
    user = answer.user
    selected_options = answer.option_ids

    if not selected_options:
        return

    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT correct_option_id FROM active_polls WHERE poll_id = ?", (poll_id,))
    res = cursor.fetchone()
    conn.close()

    if res and selected_options[0] == res[0]:
        name = user.full_name or "Anonyme"
        username = f"@{user.username}" if user.username else ""
        ajouter_point(user.id, name, username)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top_players = obtenir_top10()
    if not top_players:
        await update.message.reply_text("🏆 Aucun joueur n'a encore marqué de points !")
        return

    text = "🏆 **CLASSEMENT GÉNÉRAL** 🏆\n\n"
    medailles = ["🥇", "🥈", "🥉"]
    for i, (name, pts) in enumerate(top_players, 1):
        prefix = medailles[i - 1] if i <= 3 else f"**{i}.**"
        text += f"{prefix} {name} — **{pts} pt(s)**\n"

    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_mespoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    pts = obtenir_points_user(user.id)
    await update.message.reply_text(f"📊 {user.first_name}, vous avez **{pts} point(s)** !", parse_mode="Markdown")

# --- MAIN ---

def main():
    if not BOT_TOKEN:
        raise ValueError("ERREUR: Le BOT_TOKEN n'a pas été trouvé dans les variables d'environnement !")

    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers Commandes
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("quiz", cmd_quiz))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("mespoints", cmd_mespoints))

    # Handler Réponses Quiz
    app.add_handler(PollAnswerHandler(gerer_reponse_quiz))

    # Planification automatique (Toutes les 3600 secondes = 60 minutes)
    job_queue = app.job_queue
    if job_queue:
        job_queue.run_repeating(quiz_automatique, interval=3600, first=10)

    print("Bot démarré...")
    app.run_polling()

if __name__ == "__main__":
    main()
