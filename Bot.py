import os
import json
import logging
import asyncio
import threading
import time
from datetime import datetime

from flask import Flask, request
import firebase_admin
from firebase_admin import credentials, firestore
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, 
    InputMediaPhoto, BotCommand
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode
import re
import html

# -----------------------------------------------------------------------------
# 1. CONFIGURATION & SETUP
# -----------------------------------------------------------------------------

# Environment Variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
PUBLIC_CHANNEL_ID = os.environ.get("PUBLIC_CHANNEL_ID")  # e.g. -100...
PUBLIC_CHANNEL_LINK = os.environ.get("PUBLIC_CHANNEL_LINK")
ADMIN_TELEGRAM_ID = int(os.environ.get("ADMIN_TELEGRAM_ID", 0))
FIREBASE_KEY_JSON = os.environ.get("FIREBASE_KEY")

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Firebase Initialization
if not firebase_admin._apps:
    cred_dict = json.loads(FIREBASE_KEY_JSON)
    cred = credentials.Certificate(cred_dict)
    firebase_admin.initialize_app(cred)

db = firestore.client()

# Flask App for Render Webserver
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is running..."

# -----------------------------------------------------------------------------
# 2. HELPER FUNCTIONS
# -----------------------------------------------------------------------------

async def check_membership(user_id, bot):
    """Check if user is a member of the required channel."""
    try:
        member = await bot.get_chat_member(chat_id=PUBLIC_CHANNEL_ID, user_id=user_id)
        if member.status in ['member', 'administrator', 'creator']:
            return True
        return False
    except Exception as e:
        logger.error(f"Membership check failed: {e}")
        # If bot isn't admin in channel or id is wrong, fail safe to allow (or block)
        return False

def get_user_data(user_id):
    doc = db.collection('users').document(str(user_id)).get()
    if doc.exists:
        return doc.to_dict()
    return None

def create_user(user_id, referrer_id=None):
    now = datetime.utcnow()
    user_data = {
        'totalAttempts': 0,
        'totalCorrect': 0,
        'referralCount': 0,
        'createdAt': now,
        'currentSession': None
    }
    db.collection('users').document(str(user_id)).set(user_data)
    
    # Handle Referral
    if referrer_id and str(referrer_id) != str(user_id):
        # Record referral
        ref_doc = db.collection('referrals').document()
        ref_doc.set({
            'inviter_id': str(referrer_id),
            'invited_id': str(user_id),
            'timestamp': now
        })
        # Increment inviter count
        inviter_ref = db.collection('users').document(str(referrer_id))
        if inviter_ref.get().exists:
            inviter_ref.update({'referralCount': firestore.Increment(1)})
            return True # Indicates successful referral
    return False

def get_ad_to_show(current_index):
    """Fetch next ad based on circular order."""
    ads_ref = db.collection('ads').where('isActive', '==', True).order_by('order_index').stream()
    ads = [ad.to_dict() for ad in ads_ref]
    
    if not ads:
        return None
    
    # Simple circular logic: Use modulo of the ad count
    # Logic: If we have 3 ads, and it's the 1st ad-break (index 0), show ad 0.
    # 2nd ad break (index 1) show ad 1.
    # We maintain a global or deterministic rotation based on time or random if not strict.
    # Spec says: "Rotate circular 1->2->3".
    # We will use a simple counter based on current_index (which is passed as a counter of ad breaks)
    
    ad_idx = current_index % len(ads)
    return ads[ad_idx]


def escape_md(text):
    """Escape characters that may break Telegram Markdown parsing for inserted DB text.

    We only escape content coming from the database (question text, options,
    explanations) while keeping the bot's own Markdown markers (like **..**) intact.
    """
    if not isinstance(text, str):
        return text
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!])', r'\\\\\1', text)


def escape_html(text):
    """HTML-escape DB/user-provided text before sending with ParseMode.HTML."""
    if not isinstance(text, str):
        return text
    return html.escape(text)

# -----------------------------------------------------------------------------
# 3. BOT HANDLERS
# -----------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    referrer = args[0].replace('ref_', '') if args and args[0].startswith('ref_') else None

    # 1. Create/Get User
    user_data = get_user_data(user.id)
    referral_success = False
    
    if not user_data:
        referral_success = create_user(user.id, referrer)
        user_data = get_user_data(user.id)

    # Notify inviter if referral succeeded and threshold reached
    if referral_success:
        inviter_data = get_user_data(referrer)
        if inviter_data and inviter_data.get('referralCount', 0) == 2:
            try:
                await context.bot.send_message(
                    chat_id=referrer,
                    text="🎉 **Congratulations!**\n\nYou have invited 2 users. The remaining 75 questions are now unlocked!",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass # Inviter might have blocked bot

    # 2. Check Membership
    is_member = await check_membership(user.id, context.bot)
    if not is_member:
        keyboard = [
            [InlineKeyboardButton("Join Channel", url=PUBLIC_CHANNEL_LINK)],
            [InlineKeyboardButton("Try Again", callback_data="check_membership")]
        ]
        await update.message.reply_text(
            "⚠️ **Access Denied**\n\nPlease join our channel to access the quizzes.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 3. Check for Active Session
    if user_data.get('currentSession') and user_data['currentSession'].get('sessionActive'):
        keyboard = [
            [InlineKeyboardButton("Resume Session", callback_data="resume_session")],
            [InlineKeyboardButton("Start New", callback_data="main_menu")]
        ]
        await update.message.reply_text(
            "🔄 **Resume Quiz?**\n\nYou have an unfinished session.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Fetch Active Departments
    deps_ref = db.collection('departments').where('isActive', '==', True).stream()
    keyboard = []
    
    for dep in deps_ref:
        d_data = dep.to_dict()
        if d_data.get('totalQuestions', 0) > 0:
            keyboard.append([InlineKeyboardButton(dep.id, callback_data=f"dept_{dep.id}")])
    
    keyboard.append([InlineKeyboardButton("📊 My Score", callback_data="show_score")])
    
    text = "📚 **Select a Department**\nChoose a subject to start the quiz."
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)

# -----------------------------------------------------------------------------
# 4. QUIZ ENGINE
# -----------------------------------------------------------------------------

async def start_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE, dept_id):
    user_id = update.effective_user.id
    
    # Initialize Session
    session = {
        'department_id': dept_id,
        'current_question_index': 0,
        'correct_in_session': 0,
        'attempted_in_session': 0,
        'sessionActive': True,
        'ad_break_counter': 0
    }
    
    db.collection('users').document(str(user_id)).update({'currentSession': session})
    
    await send_question(update, context, user_id, session)

async def send_question(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id, session):
    dept_id = session['department_id']
    q_index = session['current_question_index']
    
    # --- CHECK REFERRAL LOCK (After Q25) ---
    if q_index == 25:
        user_doc = get_user_data(user_id)
        if user_doc.get('referralCount', 0) < 2:
            # Send Summary first
            await send_session_summary(update, context, session, "🔒 Progress Locked")
            
            ref_link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
            text = (
                "🔒 **Content Locked**\n\n"
                "You have completed the free 25 questions.\n"
                "**Invite 2 friends** to unlock the remaining 75 questions!\n\n"
                f"Your Referral Link:\n`{ref_link}`"
            )
            keyboard = [[InlineKeyboardButton("Check Status & Continue", callback_data="check_lock")]]
            
            if update.callback_query:
                await update.callback_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            else:
                await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
            return

    # --- CHECK COMPLETION (After Q100) ---
    if q_index >= 100:
        await send_session_summary(update, context, session, "🏆 Session Complete")
        db.collection('users').document(str(user_id)).update({'currentSession.sessionActive': False})
        await show_main_menu(update, context)
        return

    # --- AD LOGIC (Every 5 Qs, but not at 0) ---
    if q_index > 0 and q_index % 5 == 0:
        ad = get_ad_to_show(session.get('ad_break_counter', 0))
        if ad:
            # Increment ad break counter
            session['ad_break_counter'] = session.get('ad_break_counter', 0) + 1
            db.collection('users').document(str(user_id)).update({'currentSession': session})
            
            # Send Ad
            try:
                # Assuming message_id is from the channel or a stored file_id
                # Note: 'message_link' is stored, but to forward we need chat_id and message_id.
                # Simplified: Admin stores file_id or we send the link.
                # Spec says "Bot forwards/resends".
                if ad.get('message_link'):
                     await context.bot.send_message(chat_id=user_id, text=f"📢 **Sponsor**\n{ad['message_link']}")
                
                time.sleep(2) # Slight delay
            except Exception as e:
                logger.error(f"Ad error: {e}")

    # --- FETCH QUESTION ---
    # Questions are stored in 'departments/{dept_id}/questions/{q_id}'
    # We assume questions are ordered by 'question_number' or ID.
    # To avoid high reads, we should query by number.
    q_num = q_index + 1
    
    # Query for the specific question number
    q_ref = db.collection('departments').document(dept_id).collection('questions').where('question_number', '==', q_num).limit(1).stream()
    
    question_data = None
    for q in q_ref:
        question_data = q.to_dict()
        break
    
    if not question_data:
        # Fallback if question missing
        await context.bot.send_message(chat_id=user_id, text="Error: Question not found. Ending session.")
        await show_main_menu(update, context)
        return

    # Construct UI: Put full options in the message text, keep buttons short (A/B/C/D)
    opts = question_data['options']
    # Use HTML-escaped DB content to avoid visible backslashes from Markdown escaping.
    q_text = escape_html(question_data['question_text'])
    a_text = escape_html(opts.get('a', ''))
    b_text = escape_html(opts.get('b', ''))
    c_text = escape_html(opts.get('c', ''))
    d_text = escape_html(opts.get('d', ''))

    text = (
        f"<b>Question {q_num}/100</b>\n\n{q_text}\n\n"
        f"A. {a_text}\n"
        f"B. {b_text}\n"
        f"C. {c_text}\n"
        f"D. {d_text}"
    )

    # Buttons: short labels only so long option text isn't truncated on small screens
    row1 = [
        InlineKeyboardButton("A", callback_data=f"ans_a_{q_num}"),
        InlineKeyboardButton("B", callback_data=f"ans_b_{q_num}")
    ]
    row2 = [
        InlineKeyboardButton("C", callback_data=f"ans_c_{q_num}"),
        InlineKeyboardButton("D", callback_data=f"ans_d_{q_num}")
    ]
    row3 = [InlineKeyboardButton("🏠 Home", callback_data="home_confirm")]

    markup = InlineKeyboardMarkup([row1, row2, row3])
    
    # Store Correct Answer in Callback Data is risky for cheaters, 
    # but strictly following prompt we check answer in backend.
    
    if update.callback_query:
        # If previous message was an answer explanation, send new message
        # If simply flow, edit (but editing text with different height can be jumpy)
        # Spec says: "Edit message" for results. For new question, usually send new.
        await update.callback_query.message.reply_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML)

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = update.effective_user.id
    data = query.data # e.g. ans_a_12
    
    parts = data.split('_')
    selected_opt = parts[1] # 'a'
    q_num = int(parts[2])
    
    # Get Session
    user_doc = db.collection('users').document(str(user_id))
    user_data = user_doc.get().to_dict()
    session = user_data.get('currentSession')
    
    if not session or not session.get('sessionActive'):
        await query.answer("Session expired.")
        return

    # Prevent spamming (Check if question index matches)
    if (session['current_question_index'] + 1) != q_num:
        await query.answer("Please wait...", show_alert=True)
        return

    # Fetch Question Data again for verification
    dept_id = session['department_id']
    q_ref = db.collection('departments').document(dept_id).collection('questions').where('question_number', '==', q_num).limit(1).stream()
    q_data = None
    for q in q_ref:
        q_data = q.to_dict()
        break
        
    is_correct = (selected_opt == q_data['answer'])
    
    # Update Stats
    updates = {
        'totalAttempts': firestore.Increment(1),
        'currentSession.attempted_in_session': firestore.Increment(1),
        'currentSession.current_question_index': firestore.Increment(1)
    }
    
    if is_correct:
        updates['totalCorrect'] = firestore.Increment(1)
        updates['currentSession.correct_in_session'] = firestore.Increment(1)
        status_text = "✓ Correct"
    else:
        status_text = "✗ Incorrect"
        
    user_doc.update(updates)
    
    # Update Session Object Locally for Next Step
    session['current_question_index'] += 1
    session['attempted_in_session'] += 1
    if is_correct:
        session['correct_in_session'] += 1

    # Edit Message
    explanation = q_data.get('explanation', 'No explanation provided.')
    correct_ans_key = q_data['answer']
    correct_text = q_data['options'][correct_ans_key]

    # HTML-escape DB-provided texts for safe HTML formatting
    esc_question_text = escape_html(q_data['question_text'])
    esc_opts = {k: escape_html(v) for k, v in q_data['options'].items()}
    esc_explanation = escape_html(explanation)
    esc_correct_text = escape_html(correct_text)

    # Build the result block to append below the question and options (do not remove/modify the question)
    result_block = (
        f"{status_text}\n\n"
        f"<b>Correct Answer:</b> {correct_ans_key.upper()}. {esc_correct_text}\n"
        f"<b>Explanation:</b> {esc_explanation}"
    )

    # Reconstruct original question+options text (same format as when sent)
    original_text = (
        f"<b>Question {q_num}/100</b>\n\n{esc_question_text}\n\n"
        f"A. {esc_opts.get('a','')}\n"
        f"B. {esc_opts.get('b','')}\n"
        f"C. {esc_opts.get('c','')}\n"
        f"D. {esc_opts.get('d','')}"
    )

    # Combine and replace the option buttons with navigation (so the four choice buttons are removed)
    combined_text = f"{original_text}\n\n{result_block}"

    nav_buttons = [[InlineKeyboardButton("Next ➡️", callback_data="next_question")]]

    await query.edit_message_text(text=combined_text, reply_markup=InlineKeyboardMarkup(nav_buttons), parse_mode=ParseMode.HTML)

async def next_question_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_data = get_user_data(user_id)
    session = user_data.get('currentSession')
    await send_question(update, context, user_id, session)

async def send_session_summary(update: Update, context: ContextTypes.DEFAULT_TYPE, session, title):
    total = session.get('attempted_in_session', 0)
    correct = session.get('correct_in_session', 0)
    acc = (correct / total * 100) if total > 0 else 0
    
    text = (
        f"<b>{title}</b>\n\n"
        f"Department: {session['department_id']}\n"
        f"Questions Attempted: {total}\n"
        f"Correct Answers: {correct}\n"
        f"Accuracy: {acc:.1f}%"
    )
    if update.callback_query:
        await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# -----------------------------------------------------------------------------
# 5. GENERAL CALLBACK ROUTER
# -----------------------------------------------------------------------------

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user_id = update.effective_user.id
    
    if data == "check_membership":
        if await check_membership(user_id, context.bot):
            await query.answer("Access Granted!")
            await show_main_menu(update, context)
        else:
            await query.answer("Still not a member. Please join the channel.", show_alert=True)
            
    elif data == "main_menu":
        await show_main_menu(update, context)
        
    elif data.startswith("dept_"):
        dept_id = data.replace("dept_", "")
        await start_quiz(update, context, dept_id)
        
    elif data.startswith("ans_"):
        await handle_answer(update, context)
        
    elif data == "next_question":
        await next_question_handler(update, context)
        
    elif data == "home_confirm":
        # Ask for confirmation
        kb = [
            [InlineKeyboardButton("Yes, Exit", callback_data="home_exit"), InlineKeyboardButton("Cancel", callback_data="home_cancel")]
        ]
        await query.message.reply_text(
            "⚠️ **Exit Session?**\n\nYour progress will be saved.",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "home_exit":
        user_data = get_user_data(user_id)
        if user_data and user_data.get('currentSession'):
            await send_session_summary(update, context, user_data['currentSession'], "Paused Session")
        await show_main_menu(update, context)

    elif data == "home_cancel":
        await query.message.delete() # Remove the warning message
        
    elif data == "show_score":
        user_data = get_user_data(user_id)
        attempts = user_data.get('totalAttempts', 0)
        correct = user_data.get('totalCorrect', 0)
        acc = (correct / attempts * 100) if attempts > 0 else 0
        text = (
            "📊 **Your Overall Performance**\n\n"
            f"Total Attempts: {attempts}\n"
            f"Total Correct: {correct}\n"
            f"Overall Accuracy: {acc:.1f}%"
        )
        kb = [[InlineKeyboardButton("🔙 Back", callback_data="main_menu")]]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
    
    elif data == "check_lock":
        # Re-check referral count
        user_data = get_user_data(user_id)
        if user_data.get('referralCount', 0) >= 2:
            await query.answer("Unlocked!", show_alert=True)
            await next_question_handler(update, context)
        else:
            await query.answer(f"Referrals: {user_data.get('referralCount',0)}/2. Invite more!", show_alert=True)

    elif data == "resume_session":
        # Just call next_question logic, it pulls from state
        await next_question_handler(update, context)
    
    await query.answer()

# -----------------------------------------------------------------------------
# 6. ADMIN HANDLERS
# -----------------------------------------------------------------------------

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_TELEGRAM_ID:
        return
    
    kb = [
        [InlineKeyboardButton("Upload JSON", callback_data="admin_upload")],
        [InlineKeyboardButton("Total Users", callback_data="admin_users")],
        [InlineKeyboardButton("Add Ad", callback_data="admin_ad")]
    ]
    await update.message.reply_text("🛠 **Admin Panel**", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    
    if data == "admin_users":
        # Count Users
        users = db.collection('users').count().get()
        count = users[0][0].value
        await query.message.reply_text(f"👥 Total Registered Users: {count}")
    
    elif data == "admin_upload":
        await query.message.reply_text("📂 **Upload Mode**\n\n1. Reply with the Department Name.\n2. Then upload the JSON file.")
        context.user_data['admin_state'] = 'awaiting_dept_name'

    # --- ADDED THIS BLOCK ---
    elif data == "admin_ad":
        await query.message.reply_text("Send the text/link content for the Ad.")
        context.user_data['admin_state'] = 'awaiting_ad_link'
    
    await query.answer() # Always answer the query to stop the loading animation

async def admin_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_TELEGRAM_ID:
        return

    state = context.user_data.get('admin_state')
    
    # 1. Capture Department Name
    if state == 'awaiting_dept_name':
        dept_name = update.message.text.strip()
        context.user_data['upload_dept'] = dept_name
        context.user_data['admin_state'] = 'awaiting_json'
        await update.message.reply_text(f"Selected Department: **{dept_name}**\nNow upload the JSON file.", parse_mode=ParseMode.MARKDOWN)
        return

    # 2. Process JSON File
    if state == 'awaiting_json' and update.message.document:
        doc = update.message.document
        f = await doc.get_file()
        byte_array = await f.download_as_bytearray()
        
        try:
            questions = json.loads(byte_array.decode('utf-8'))
            dept_name = context.user_data['upload_dept']
            
            # Save to Firestore
            dept_ref = db.collection('departments').document(dept_name)
            batch = db.batch()
            
            # Update Department Info
            dept_ref.set({
                'isActive': True,
                'totalQuestions': len(questions)
            })
            
            # Upload Questions
            for q in questions:
                # Ensure structure matches spec
                q_num = q.get('question_number')
                q_doc = dept_ref.collection('questions').document(str(q_num))
                # Not using batch for subcollection in loop to avoid limits on huge files, 
                # but for <500 items batch is fine. Using direct set for safety.
                q_doc.set(q)
            
            await update.message.reply_text(f"✅ Successfully uploaded {len(questions)} questions to {dept_name}.")
            context.user_data['admin_state'] = None
            
            # Ask to post to channel
            await update.message.reply_text("Send a photo with caption to post this update to the public channel (or /cancel).")
            context.user_data['admin_state'] = 'awaiting_post'
            context.user_data['post_dept'] = dept_name
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing JSON: {e}")

    # 3. Post to Public Channel
    if state == 'awaiting_post' and update.message.photo:
        dept_name = context.user_data.get('post_dept')
        caption = update.message.caption or f"New Quiz Available: {dept_name}"
        
        # Add Deep Link
        deep_link = f"https://t.me/{context.bot.username}?start=dept_{dept_name}"
        final_caption = f"{caption}\n\n👉 Start Quiz: {deep_link}"
        
        await context.bot.send_photo(
            chat_id=PUBLIC_CHANNEL_ID,
            photo=update.message.photo[-1].file_id,
            caption=final_caption
        )
        await update.message.reply_text("✅ Posted to channel.")
        context.user_data['admin_state'] = None
        
    # 4. Add Ad
    if state == 'awaiting_ad_link':
        # Simple flow: User sent message link
        link = update.message.text
        # Save Ad
        db.collection('ads').add({
            'message_link': link,
            'order_index': int(time.time()),
            'isActive': True
        })
        await update.message.reply_text("✅ Ad saved.")
        context.user_data['admin_state'] = None

async def admin_ad_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "admin_ad":
        await query.message.reply_text("Send the text/link content for the Ad.")
        context.user_data['admin_state'] = 'awaiting_ad_link'

# -----------------------------------------------------------------------------
# 7. MAIN EXECUTION
# -----------------------------------------------------------------------------

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # User Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(admin_callback, pattern="^admin_"))
    application.add_handler(CallbackQueryHandler(callback_router)) # Catches everything else
    
    # Admin Command
    application.add_handler(CommandHandler("ethioegzam", admin_panel))
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.TEXT, admin_message_handler))

    # Run Flask in separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Run Bot
    print("Bot is polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
