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
from urllib.parse import quote_plus, unquote_plus
from uuid import uuid4

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

def create_user(user_id, referrer_id=None, ref_dept=None):
    """Create a user record and optionally record a referral for a specific department.

    Returns a tuple: (created: bool, referral_recorded: bool, dept_unlocked: bool)
    """
    now = datetime.utcnow()
    user_doc_ref = db.collection('users').document(str(user_id))
    user_data = {
        'totalAttempts': 0,
        'totalCorrect': 0,
        'referral_counts': {},    # dept_id -> int
        'unlocked_departments': {}, # dept_id -> True
        'createdAt': now,
        'currentSession': None
    }
    user_doc_ref.set(user_data)

    # Handle Referral (department-specific)
    if referrer_id and str(referrer_id) != str(user_id) and ref_dept:
        # Record referral
        ref_record = db.collection('referrals').document()
        ref_record.set({
            'inviter_id': str(referrer_id),
            'invited_id': str(user_id),
            'dept_id': str(ref_dept),
            'timestamp': now
        })

        inviter_ref = db.collection('users').document(str(referrer_id))
        try:
            # Increment inviter's count for that department
            inviter_ref.update({f'referral_counts.{ref_dept}': firestore.Increment(1)})
        except Exception:
            # If inviter doc doesn't exist or field missing, create/init it
            inviter_ref.set({
                'referral_counts': {ref_dept: 1},
                'unlocked_departments': {}
            }, merge=True)

        # Check if inviter reached threshold for this department
        inviter_doc = inviter_ref.get().to_dict() if inviter_ref.get().exists else {}
        dept_count = int(inviter_doc.get('referral_counts', {}).get(ref_dept, 0))
        dept_unlocked = False
        if dept_count >= 2:
            # Unlock this department for inviter
            try:
                inviter_ref.update({f'unlocked_departments.{ref_dept}': True})
            except Exception:
                inviter_ref.set({'unlocked_departments': {ref_dept: True}}, merge=True)
            dept_unlocked = True

        return True, True, dept_unlocked

    return True, False, False

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
    referrer = None
    ref_dept = None
    dept_to_start = None

    if args and args[0].startswith('ref_'):
        # format expected: ref_<inviter>_dept_<deptCode>
        rest = unquote_plus(args[0][4:])
        if '_dept_' in rest:
            inviter, dept_code = rest.split('_dept_', 1)
            referrer = inviter
            ref_dept = dept_code
        else:
            referrer = rest
    elif args and args[0].startswith('dept_'):
        dept_to_start = unquote_plus(args[0][5:])

    # 1. Create/Get User
    user_data = get_user_data(user.id)
    referral_recorded = False
    dept_unlocked = False

    if not user_data:
        _, referral_recorded, dept_unlocked = create_user(user.id, referrer, ref_dept)
        user_data = get_user_data(user.id)

    # Keep basic user profile fields up-to-date
    try:
        db.collection('users').document(str(user.id)).update({
            'first_name': user.first_name or '',
            'last_name': user.last_name or '',
            'username': user.username or ''
        })
    except Exception:
        # If update fails (rare), ensure fields exist by setting merge
        db.collection('users').document(str(user.id)).set({
            'first_name': user.first_name or '',
            'last_name': user.last_name or '',
            'username': user.username or ''
        }, merge=True)

    # Notify inviter if referral recorded and department got unlocked
    if referral_recorded and referrer:
        try:
            inviter_id = int(referrer)
        except Exception:
            inviter_id = referrer
        if dept_unlocked and ref_dept:
            # Prefer showing the human-friendly department name (displayName)
            try:
                dept_doc = db.collection('departments').document(str(ref_dept)).get()
                if dept_doc.exists:
                    dept_display = dept_doc.to_dict().get('displayName') or str(ref_dept)
                else:
                    dept_display = str(ref_dept)
            except Exception:
                dept_display = str(ref_dept)

            try:
                await context.bot.send_message(
                    chat_id=inviter_id,
                    text=f"🎉 **Congratulations!**\n\nYou have invited 2 users for *{dept_display}*. That department is now unlocked for you!",
                    parse_mode=ParseMode.MARKDOWN
                )
            except:
                pass

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

    # If start link specified a department, go directly to that department's quiz
    if dept_to_start:
        await start_quiz(update, context, dept_to_start)
        return

    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Fetch Active Departments
    deps_ref = db.collection('departments').where('isActive', '==', True).stream()
    keyboard = []
    
    for dep in deps_ref:
        d_data = dep.to_dict()
        if d_data.get('totalQuestions', 0) > 0:
            # Show the human-friendly display name if available
            label = d_data.get('displayName') or dep.id
            keyboard.append([InlineKeyboardButton(label, callback_data=f"dept_{dep.id}")])
    
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
        # If this department isn't unlocked for the user, require referrals specific to this dept
        unlocked = False
        if user_doc:
            unlocked = bool(user_doc.get('unlocked_departments', {}).get(dept_id, False))
        if not unlocked:
            # Send Summary first
            await send_session_summary(update, context, session, "🔒 Progress Locked")
            # Dept-specific referral link
            # build ref param as: ref_<inviter>_dept_<deptCode>
            ref_param = f"ref_{user_id}_dept_{dept_id}"
            ref_link = f"https://t.me/{context.bot.username}?start={quote_plus(ref_param)}"
            text = (
                "🔒 **Content Locked**\n\n"
                "You have completed the free 25 questions for this department.\n"
                "**Invite 2 friends using your department referral link** to unlock the remaining 75 questions for this department!\n\n"
                f"Your Referral Link:\n`{ref_link}`"
            )
            cb_dept = quote_plus(dept_id)
            keyboard = [[InlineKeyboardButton("Check Status & Continue", callback_data=f"check_lock_{cb_dept}")]]
            
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
                # Ad storage supports: {type: 'photo'|'video'|'text', file_id, caption, text}
                if ad.get('type') == 'photo' and ad.get('file_id'):
                    await context.bot.send_photo(chat_id=user_id, photo=ad.get('file_id'), caption=ad.get('caption',''))
                elif ad.get('type') == 'video' and ad.get('file_id'):
                    try:
                        await context.bot.send_video(chat_id=user_id, video=ad.get('file_id'), caption=ad.get('caption',''))
                    except Exception:
                        # Fallback: if original was uploaded as a document, try sending as document
                        try:
                            await context.bot.send_document(chat_id=user_id, document=ad.get('file_id'), caption=ad.get('caption',''))
                        except Exception as e:
                            logger.error(f"Failed to send video ad as video or document: {e}")
                elif ad.get('type') == 'text' and ad.get('text'):
                    await context.bot.send_message(chat_id=user_id, text=ad.get('text'))
                elif ad.get('message_link'):
                    await context.bot.send_message(chat_id=user_id, text=f"📢 **Sponsor**\n{ad['message_link']}")

                time.sleep(2)
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

    # --- Update leaderboard per-department ---
    try:
        lb_ref = db.collection('leaderboard').document(str(user_id))
        dept = dept_id
        # Ensure doc exists otherwise prepare initial structure
        try:
            # Increment attempts
            lb_ref.update({
                f'departments.{dept}.attempts': firestore.Increment(1),
                'updatedAt': datetime.utcnow()
            })
            if is_correct:
                lb_ref.update({f'departments.{dept}.correct': firestore.Increment(1)})
        except Exception:
            # Doc or field missing - create initial doc for this dept
            init = {
                'departments': {
                    dept: {
                        'attempts': 1,
                        'correct': 1 if is_correct else 0
                    }
                },
                'updatedAt': datetime.utcnow()
            }
            lb_ref.set(init, merge=True)
    except Exception as e:
        logger.error(f"Leaderboard update failed: {e}")

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
    
    elif data.startswith("check_lock"):
        # Re-check referral count for the specific department
        # callback_data may be 'check_lock' or 'check_lock_<dept_enc>'
        user_data = get_user_data(user_id)
        dept = None
        if data == "check_lock":
            dept = None
        else:
            # parse dept after prefix
            try:
                dept_enc = data.replace("check_lock_", "", 1)
                dept = unquote_plus(dept_enc)
            except Exception:
                dept = None

        if dept:
            unlocked = bool(user_data.get('unlocked_departments', {}).get(dept, False))
            if unlocked:
                await query.answer("Unlocked!", show_alert=True)
                await next_question_handler(update, context)
            else:
                count = int(user_data.get('referral_counts', {}).get(dept, 0))
                await query.answer(f"Referrals for {dept}: {count}/2. Invite more!", show_alert=True)
        else:
            # Fallback to global behavior if no dept provided
            total_refs = 0
            try:
                total_refs = sum(int(v) for v in user_data.get('referral_counts', {}).values())
            except Exception:
                total_refs = 0
            if total_refs >= 2:
                await query.answer("Unlocked!", show_alert=True)
                await next_question_handler(update, context)
            else:
                await query.answer(f"Total referrals: {total_refs}/2. Invite more!", show_alert=True)

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
        await query.message.reply_text(
            "📣 Send the ad now as either:\n- Photo (with optional caption)\n- Video (with optional caption)\n- Plain text\n\nThe bot will store only the Telegram `file_id` or the text."
        )
        context.user_data['admin_state'] = 'awaiting_ad'
    
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

            # Generate a unique dept code (alphanumeric) and save displayName
            dept_code = uuid4().hex[:8]
            # Save to Firestore using dept_code as document id
            dept_ref = db.collection('departments').document(dept_code)
            batch = db.batch()

            # Update Department Info (store displayName and code)
            dept_ref.set({
                'displayName': dept_name,
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
            
            # remember both display name and code for posting
            await update.message.reply_text(f"✅ Successfully uploaded {len(questions)} questions to {dept_name} (code: {dept_code}).")
            context.user_data['admin_state'] = None
            
            # Ask to post to channel
            await update.message.reply_text("Send a photo with caption to post this update to the public channel (or /cancel).")
            context.user_data['admin_state'] = 'awaiting_post'
            context.user_data['post_dept'] = dept_name
            context.user_data['post_dept_id'] = dept_code
            
        except Exception as e:
            await update.message.reply_text(f"❌ Error processing JSON: {e}")

    # 3. Post to Public Channel
    if state == 'awaiting_post' and update.message.photo:
        dept_name = context.user_data.get('post_dept')
        dept_code = context.user_data.get('post_dept_id')
        caption = update.message.caption or f"New Quiz Available: {dept_name}"

        # Add Deep Link using department code: dept_<code>
        if dept_code:
            deep_link = f"https://t.me/{context.bot.username}?start=dept_{dept_code}"
        else:
            # Fallback to old behavior (dept name) if code missing
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
    if state == 'awaiting_ad':
        # Photo
        if update.message.photo:
            file_id = update.message.photo[-1].file_id
            caption = update.message.caption or ''
            db.collection('ads').add({
                'type': 'photo',
                'file_id': file_id,
                'caption': caption,
                'order_index': int(time.time()),
                'isActive': True
            })
            await update.message.reply_text("✅ Photo ad saved (file_id stored).")
            context.user_data['admin_state'] = None
            return

        # Video sent as a document (some clients send videos as documents)
        if update.message.document and getattr(update.message.document, 'mime_type', '').startswith('video'):
            file_id = update.message.document.file_id
            caption = update.message.caption or ''
            db.collection('ads').add({
                'type': 'video',
                'file_id': file_id,
                'caption': caption,
                'order_index': int(time.time()),
                'isActive': True
            })
            await update.message.reply_text("✅ Video ad saved (file_id stored).")
            context.user_data['admin_state'] = None
            return

        # Video
        if update.message.video:
            file_id = update.message.video.file_id
            caption = update.message.caption or ''
            db.collection('ads').add({
                'type': 'video',
                'file_id': file_id,
                'caption': caption,
                'order_index': int(time.time()),
                'isActive': True
            })
            await update.message.reply_text("✅ Video ad saved (file_id stored).")
            context.user_data['admin_state'] = None
            return

        # Plain Text
        if update.message.text:
            text = update.message.text
            db.collection('ads').add({
                'type': 'text',
                'text': text,
                'order_index': int(time.time()),
                'isActive': True
            })
            await update.message.reply_text("✅ Text ad saved.")
            context.user_data['admin_state'] = None
            return

async def admin_ad_trigger(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "admin_ad":
        await query.message.reply_text("Send the text/link content for the Ad.")
        context.user_data['admin_state'] = 'awaiting_ad_link'


async def admin_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: compute and display leaderboards.
    Shows Top 10 overall by average department accuracy and Top 3 per department.
    """
    user_id = update.effective_user.id
    if user_id != ADMIN_TELEGRAM_ID:
        return

    # Fetch all leaderboard entries
    docs = db.collection('leaderboard').stream()
    overall_list = []  # (user_id, overall_avg, total_attempts)
    dept_lists = {}    # dept_id -> [(user_id, acc, attempts)]

    for d in docs:
        uid = d.id
        data = d.to_dict() or {}
        depts = data.get('departments', {})
        per_accs = []
        total_attempts = 0
        for dept, stats in depts.items():
            att = int(stats.get('attempts', 0))
            cor = int(stats.get('correct', 0))
            if att > 0:
                acc = cor / att
                per_accs.append(acc)
                total_attempts += att
                dept_lists.setdefault(dept, []).append((uid, acc, att))

        if per_accs:
            overall_avg = sum(per_accs) / len(per_accs)
            overall_list.append((uid, overall_avg, total_attempts))

    # Top 10 overall
    overall_list.sort(key=lambda x: x[1], reverse=True)
    top10 = overall_list[:10]

    parts = []
    parts.append("*Top 10 Users — Overall Average Accuracy*\n")
    if not top10:
        parts.append("No leaderboard data available.")
    else:
        for idx, (uid, avg, attempts) in enumerate(top10, start=1):
            user_doc = db.collection('users').document(uid).get()
            udata = user_doc.to_dict() if user_doc.exists else {}
            name = udata.get('first_name') or udata.get('username') or uid
            parts.append(f"{idx}. {name} — {avg*100:.1f}% ({attempts} attempts)")

    # Top 3 per department
    parts.append("\n*Top 3 Per Department*\n")
    if not dept_lists:
        parts.append("No per-department scores yet.")
    else:
        for dept, arr in dept_lists.items():
            parts.append(f"{dept}:")
            arr.sort(key=lambda x: x[1], reverse=True)
            for j, (uid, acc, att) in enumerate(arr[:3], start=1):
                user_doc = db.collection('users').document(uid).get()
                udata = user_doc.to_dict() if user_doc.exists else {}
                name = udata.get('first_name') or udata.get('username') or uid
                parts.append(f" {j}. {name} — {acc*100:.1f}% ({att} attempts)")

    text = "\n".join(parts)
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

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
    application.add_handler(CommandHandler("leaderboard", admin_leaderboard))
    # Include VIDEO filter so video messages trigger the admin handler
    application.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO | filters.TEXT | filters.VIDEO, admin_message_handler))

    # Run Flask in separate thread
    flask_thread = threading.Thread(target=run_flask)
    flask_thread.daemon = True
    flask_thread.start()

    # Run Bot
    print("Bot is polling...")
    application.run_polling()

if __name__ == '__main__':
    main()
