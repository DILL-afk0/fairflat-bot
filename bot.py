import sqlite3
import logging
import threading
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler


# ==================== НАСТРОЙКИ ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
DATABASE = "fairflat_fix.db"
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Участники
USERS = {
    '@DILLC7': 'Матрос',
    '@djumshut2000': 'Борода', 
    '@naattive': 'Даник'
}

# Админы (только ты можешь сбрасывать статистику)
ADMINS = {'@DILLC7'}

# Минимальный баланс
MIN_BALANCE = -10

# Задачи
TASKS = {
    'санузел': {
        'points': 4,
        'rules': '• Мойка унитаза\n• Пол в туалете'
    },
    'ванна': {
        'points': 3,
        'rules': '• Мойка ванны/душа\n• Мойка раковины\n• Уборка на стиральной машине'
    },
    'кухня': {
        'points': 3,
        'rules': '• Пылесос пола на кухне\n• Уборка общего стола\n• Уборка стола у раковины\n• Уборка плиты'
    },
    'коридор': {
        'points': 2,
        'rules': '• Коврики в коридоре\n• Тумбочка/полка\n• Порядок у входной двери'
    },
    'пылесос': {
        'points': 2,
        'rules': '• Пылесос всей квартиры\n• Убрать пылесос на место'
    },
    'мусор': {
        'points': 1,
        'rules': '• Вынести все пакеты с мусором\n• Заменить пакеты в ведрах'
    },
    'готовка': {
        'points': 3,
        'rules': '• Приготовление еды ДЛЯ ВСЕХ участников\n• Уборка после готовки (кроме посуды)'
    },
    'посуда': {
        'points': 2,
        'rules': '• Мытьё посуды после ОБЩЕЙ готовки\n• Протирка стола после еды\n• Чистка плиты если нужно'
    }
}

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    """Инициализация базы данных"""
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    c = conn.cursor()
    
    # Пользователи
    c.execute('''CREATE TABLE IF NOT EXISTS users
                 (telegram TEXT PRIMARY KEY,
                  name TEXT,
                  is_home BOOLEAN DEFAULT 1,
                  balance INTEGER DEFAULT 0)''')
    
    # Задачи (добавили confirmed_at)
    c.execute('''CREATE TABLE IF NOT EXISTS tasks_done
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  task TEXT,
                  user_telegram TEXT,
                  user_name TEXT,
                  points INTEGER,
                  confirmed_by TEXT,
                  date TEXT,
                  confirmed_at TEXT,
                  is_confirmed BOOLEAN DEFAULT 0,
                  is_penalty BOOLEAN DEFAULT 0,
                  details TEXT)''')
    
    # Очередь
    c.execute('''CREATE TABLE IF NOT EXISTS queue
                 (task TEXT PRIMARY KEY,
                  last_user TEXT,
                  last_date TEXT)''')
    
    # Добавляем пользователей
    for telegram, name in USERS.items():
        c.execute('''INSERT OR IGNORE INTO users (telegram, name) VALUES (?, ?)''',
                  (telegram, name))
    
    # Инициализируем очередь
    for task in TASKS.keys():
        c.execute("INSERT OR IGNORE INTO queue (task, last_user) VALUES (?, ?)",
                  (task, 'никто'))
    
    conn.commit()
    conn.close()
    print("✅ База данных инициализирована")

def get_conn():
    """Получить соединение с БД"""
    conn = sqlite3.connect(DATABASE, check_same_thread=False)
    return conn

def execute_query(query, params=()):
    """Выполнить запрос"""
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute(query, params)
        conn.commit()
        if query.strip().upper().startswith('SELECT'):
            return c.fetchall()
        elif query.strip().upper().startswith('INSERT'):
            return c.lastrowid
        return True
    except Exception as e:
        print(f"❌ Ошибка БД: {e}")
        return None
    finally:
        conn.close()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================
def get_user_name(telegram):
    """Получить имя пользователя"""
    return USERS.get(telegram, telegram)

def is_admin(telegram):
    """Проверить админ ли"""
    return telegram in ADMINS

def get_next_for_task(task):
    """Определить кто должен делать задачу"""
    # Кто дома
    home_users = execute_query(
        "SELECT telegram, name FROM users WHERE is_home = 1"
    )
    
    if not home_users:
        return None, None
    
    # Кто последний делал эту задачу
    result = execute_query(
        "SELECT last_user FROM queue WHERE task = ?", (task,)
    )
    last_user = result[0][0] if result else 'никто'
    
    # Считаем для каждого когда последний раз делал
    user_stats = []
    for telegram, name in home_users:
        result = execute_query(
            '''SELECT MAX(date) FROM tasks_done 
               WHERE task = ? AND user_name = ? AND is_confirmed = 1
               AND is_penalty = 0''', 
            (task, name)
        )
        
        if result and result[0][0]:
            last_date = datetime.strptime(result[0][0], '%Y-%m-%d %H:%M:%S')
            days_ago = (datetime.now() - last_date).days
        else:
            days_ago = 999  # Никогда не делал
            
        user_stats.append({
            'telegram': telegram,
            'name': name,
            'days_ago': days_ago
        })
    
    # Сортируем: кто дольше не делал → первый
    user_stats.sort(key=lambda x: x['days_ago'], reverse=True)
    return user_stats[0]['telegram'], user_stats[0]['name'], last_user

def update_queue(task, user_name):
    """Обновить очередь после выполнения"""
    execute_query(
        "UPDATE queue SET last_user = ?, last_date = ? WHERE task = ?",
        (user_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'), task)
    )

def update_balance(telegram, points):
    """Обновить баланс пользователя (не ниже MIN_BALANCE)"""
    # Получаем текущий баланс
    result = execute_query(
        "SELECT balance FROM users WHERE telegram = ?", (telegram,)
    )
    current_balance = result[0][0] if result else 0
    
    # Обновляем (но не ниже MIN_BALANCE)
    new_balance = max(current_balance + points, MIN_BALANCE)
    execute_query(
        "UPDATE users SET balance = ? WHERE telegram = ?",
        (new_balance, telegram)
    )
    
    return new_balance

# ==================== ОСНОВНЫЕ КОМАНДЫ ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главное меню"""
    user = update.effective_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    # Проверяем регистрацию
    if telegram not in USERS:
        await update.message.reply_text(
            "👋 *Привет!*\n\n"
            "Я бот для справедливого распределения дел в квартире.\n"
            "Участники:\n"
            "• Матрос (@DILLC7)\n"
            "• Борода (@djumshut2000)\n"
            "• Даник (@naattive)\n\n"
            "Если ты один из них, используй кнопки ниже.",
            parse_mode='Markdown'
        )
        return
    
    user_name = USERS[telegram]
    
    # Главное меню
    keyboard = [
        [InlineKeyboardButton("🎯 Кто что должен?", callback_data='menu_who')],
        [InlineKeyboardButton("✅ Я сделал задачу", callback_data='menu_did')],
        [InlineKeyboardButton("🍽️ Готовка/посуда", callback_data='menu_food')],
        [InlineKeyboardButton("⚠️ Штраф/нарушение", callback_data='menu_penalty')],
        [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
        [InlineKeyboardButton("🚪 Отметить отъезд/возвращение", callback_data='menu_home')],
        [InlineKeyboardButton("📋 Правила системы", callback_data='rules')]
    ]
    
    # Добавляем админку если админ
    if is_admin(telegram):
        keyboard.insert(6, [InlineKeyboardButton("⚙ Админка", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"🏠 *Главное меню*\n\n"
        f"Привет, {user_name}! Выберите действие:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать главное меню"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if telegram not in USERS:
        return
    
    user_name = USERS[telegram]
    
    keyboard = [
        [InlineKeyboardButton("🎯 Кто что должен?", callback_data='menu_who')],
        [InlineKeyboardButton("✅ Я сделал задачу", callback_data='menu_did')],
        [InlineKeyboardButton("🍽️ Готовка/посуда", callback_data='menu_food')],
        [InlineKeyboardButton("⚠️ Штраф/нарушение", callback_data='menu_penalty')],
        [InlineKeyboardButton("📊 Статистика", callback_data='stats')],
        [InlineKeyboardButton("🚪 Отметить отъезд/возвращение", callback_data='menu_home')],
        [InlineKeyboardButton("📋 Правила системы", callback_data='rules')]
    ]
    
    # Добавляем админку если админ
    if is_admin(telegram):
        keyboard.insert(6, [InlineKeyboardButton("⚙ Админка", callback_data='admin_panel')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    new_text = f"🏠 *Главное меню*\n\nПривет, {user_name}! Выберите действие:"
    
    if query.message.text != new_text:
        await query.edit_message_text(
            new_text,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    else:
        await query.edit_message_reply_markup(reply_markup=reply_markup)

# Продолжи дальше!
# ==================== МЕНЮ "КТО ЧТО ДОЛЖЕН" ====================

async def menu_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню выбора задачи"""
    query = update.callback_query
    await query.answer()
    
    keyboard = []
    tasks = list(TASKS.keys())
    
    for i in range(0, len(tasks), 2):
        row = []
        if i < len(tasks):
            row.append(InlineKeyboardButton(tasks[i], callback_data=f'who_{tasks[i]}'))
        if i + 1 < len(tasks):
            row.append(InlineKeyboardButton(tasks[i+1], callback_data=f'who_{tasks[i+1]}'))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🏠 Назад", callback_data='main_menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "🎯 *Выберите задачу, чтобы узнать кто должен делать:*",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def process_who(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора задачи"""
    query = update.callback_query
    await query.answer()
    
    task = query.data.replace('who_', '')
    
    if task not in TASKS:
        await query.edit_message_text("❌ Задача не найдена")
        return
    
    # Определяем следующего
    next_tg, next_name, last_user = get_next_for_task(task)
    
    if not next_name:
        await query.edit_message_text("❌ Все в отъезде!")
        return
    
    # Информация о последнем выполнении этой задачи именно этим человеком
    result = execute_query(
        '''SELECT MAX(date) FROM tasks_done 
           WHERE task = ? AND user_name = ? AND is_confirmed = 1 AND is_penalty = 0''',
        (task, next_name)
    )
    
    if result and result[0][0]:
        last_date = datetime.strptime(result[0][0], '%Y-%m-%d %H:%M:%S')
        last_str = last_date.strftime('%d.%m.%Y')
    else:
        last_str = "никогда"
    
    # Информация из очереди
    queue_info = execute_query(
        "SELECT last_user, last_date FROM queue WHERE task = ?", (task,)
    )
    if queue_info and queue_info[0][1]:
        q_last_user, q_last_date = queue_info[0]
        q_last_date_dt = datetime.strptime(q_last_date, '%Y-%m-%d %H:%M:%S')
        q_last_date_str = q_last_date_dt.strftime('%d.%m.%Y')
        queue_text = f"👥 *Последним делал:* {q_last_user} ({q_last_date_str})\n"
    else:
        queue_text = "👥 *Последним делал:* никто\n"
    
    response = (
        f"🎯 *{task.upper()}*\n\n"
        f"👤 *Должен делать:* {next_name}\n"
        f"📅 *Последний раз он делал:* {last_str}\n"
        f"{queue_text}"
        f"⭐ *Баллов за задачу:* {TASKS[task]['points']}\n\n"
        f"*Что входит в задачу:*\n{TASKS[task]['rules']}\n\n"
        f"{next_tg}, твоя очередь!"
    )
    
    keyboard = [
        [InlineKeyboardButton("✅ Я сделал эту задачу", callback_data=f'did_{task}')],
        [InlineKeyboardButton("🎯 Выбрать другую задачу", callback_data='menu_who')],
        [InlineKeyboardButton("🏠 Назад", callback_data='main_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(response, parse_mode='Markdown', reply_markup=reply_markup)

# ==================== МЕНЮ "Я СДЕЛАЛ ЗАДАЧУ" ====================

async def menu_did(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню выполненных задач"""
    query = update.callback_query
    await query.answer()
    
    keyboard = []
    tasks = list(TASKS.keys())
    
    for i in range(0, len(tasks), 2):
        row = []
        if i < len(tasks):
            row.append(InlineKeyboardButton(tasks[i], callback_data=f'did_{tasks[i]}'))
        if i + 1 < len(tasks):
            row.append(InlineKeyboardButton(tasks[i+1], callback_data=f'did_{tasks[i+1]}'))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton("🏠 Назад", callback_data='main_menu')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "✅ *Какую задачу вы выполнили?*\n\n"
        "Выберите из списка:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def process_did(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выполнения задачи"""
    query = update.callback_query
    await query.answer()
    
    task = query.data.replace('did_', '')
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if telegram not in USERS:
        await query.edit_message_text("❌ Вы не участник системы!")
        return
    
    user_name = USERS[telegram]
    
    # Записываем задачу как неподтверждённую (date = время выполнения)
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    task_id = execute_query(
        '''INSERT INTO tasks_done 
           (task, user_telegram, user_name, points, date)
           VALUES (?, ?, ?, ?, ?)''',
        (task, telegram, user_name, TASKS[task]['points'], now_str)
    )
        
    if not task_id:
        await query.edit_message_text("❌ Ошибка при сохранении задачи")
        return
    
    # Находим кого можно попросить подтвердить (кроме себя)
    possible_confirmers = execute_query(
        "SELECT telegram, name FROM users WHERE telegram != ? AND is_home = 1",
        (telegram,)
    )
    
    if not possible_confirmers:
        await query.edit_message_text(
            f"✅ *Задача записана!*\n\n"
            f"👤 {user_name} → *{task}*\n"
            f"⭐ {TASKS[task]['points']} баллов\n\n"
            f"Сейчас нет других участников дома для подтверждения.\n"
            f"Задача будет ждать подтверждения.",
            parse_mode='Markdown'
        )
        return
    
    # Создаём кнопки подтверждения
    keyboard = []
    for conf_tg, conf_name in possible_confirmers:
        keyboard.append([
            InlineKeyboardButton(
                f"✅ {conf_name} подтверждает",
                callback_data=f'confirm_{task_id}_{conf_name}'
            )
        ])
    
    keyboard.append([InlineKeyboardButton("❌ Отменить", callback_data=f'cancel_{task_id}')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🔄 *Требуется подтверждение*\n\n"
        f"👤 *{user_name}* выполнил(а): *{task}*\n"
        f"⭐ Баллов: {TASKS[task]['points']}\n"
        f"🕒 {datetime.now().strftime('%H:%M %d.%m.%Y')}\n\n"
        f"Подтвердить может 1 другой участник:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

# ==================== ПОДТВЕРЖДЕНИЕ / ОТМЕНА ЗАДАЧ ====================

async def process_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка подтверждения задачи или штрафа"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if not data.startswith('confirm_'):
        await query.edit_message_text("❌ Неверный формат подтверждения")
        return
    
    parts = data.replace('confirm_', '').split('_')
    
    if len(parts) < 2:
        await query.edit_message_text("❌ Ошибка в данных")
        return
    
    task_id = int(parts[0])
    expected_confirmer = parts[1]
    
    # Кто подтверждает
    confirmer = query.from_user
    confirmer_tg = f"@{confirmer.username}" if confirmer.username else confirmer.first_name
    
    if confirmer_tg not in USERS:
        await query.edit_message_text("❌ Вы не участник системы!")
        return
    
    confirmer_name = USERS[confirmer_tg]
    
    # Проверяем, что подтверждает правильный человек
    if confirmer_name != expected_confirmer:
        await query.edit_message_text(f"❌ Подтверждать должен {expected_confirmer}!")
        return
    
    # Получаем информацию о задаче
    result = execute_query(
        '''SELECT task, user_telegram, user_name, points, is_confirmed, is_penalty 
           FROM tasks_done WHERE id = ?''',
        (task_id,)
    )
    
    if not result:
        await query.edit_message_text("❌ Задача не найдена!")
        return
    
    task, doer_tg, doer_name, points, is_confirmed, is_penalty = result[0]
    
    # Проверяем, не подтверждена ли уже
    if is_confirmed:
        await query.edit_message_text("✅ Эта запись уже подтверждена!")
        return
    
    # Проверяем, что не подтверждает свою задачу/штраф
    if doer_name == confirmer_name:
        await query.edit_message_text("❌ Нельзя подтверждать свою запись!")
        return
    
    # Подтверждаем задачу (confirmed_at = сейчас, date не трогаем)
    confirmed_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    execute_query(
        '''UPDATE tasks_done 
           SET confirmed_by = ?, is_confirmed = 1, confirmed_at = ?
           WHERE id = ?''',
        (confirmer_name, confirmed_at, task_id)
    )
    
    # Обновляем баланс с ограничением до MIN_BALANCE
    new_balance = update_balance(doer_tg, points)
    
    # Если не штраф - обновляем очередь
    if not is_penalty and task in TASKS:
        update_queue(task, doer_name)
    
    response = (
        f"✅ *ПОДТВЕРЖДЕНО!*\n\n"
        f"👤 {doer_name}\n"
        f"📝 Запись: *{task}*\n"
        f"👍 Подтвердил: {confirmer_name}\n"
        f"⭐ Баллов: {points}\n"
        f"📊 Новый баланс: {new_balance} (не ниже {MIN_BALANCE})\n"
        f"🕒 {datetime.now().strftime('%H:%M %d.%m.%Y')}"
    )
    
    await query.edit_message_text(response, parse_mode='Markdown')

async def cancel_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена неподтверждённой задачи"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    task_id = int(data.replace('cancel_', ''))
    
    # Просто удаляем запись если она ещё не подтверждена
    result = execute_query(
        "SELECT is_confirmed FROM tasks_done WHERE id = ?", (task_id,)
    )
    if not result:
        await query.edit_message_text("❌ Задача не найдена")
        return
    
    is_confirmed = result[0][0]
    if is_confirmed:
        await query.edit_message_text("❌ Нельзя отменить уже подтверждённую запись")
        return
    
    execute_query("DELETE FROM tasks_done WHERE id = ?", (task_id,))
    await query.edit_message_text("❌ Задача отменена")

# ==================== ГОТОВКА И ПОСУДА ====================

async def menu_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню готовки/посуды"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("🍳 Я приготовил для всех", callback_data='cooked_all')],
        [InlineKeyboardButton("🍽️ Я помыл посуду", callback_data='washed_dishes')],
        [InlineKeyboardButton("🏠 Назад", callback_data='main_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    rules = (
        "🍽️ *ПРАВИЛА ГОТОВКИ И ПОСУДЫ*\n\n"
        "1. *Если готовил ДЛЯ ВСЕХ:*\n"
        "   • Получаешь 3 балла за готовку\n"
        "   • Посуду моет ТОТ, КТО КУШАЛ\n"
        "   • Кто не кушал → не обязан мыть\n\n"
        "2. *Если готовил ТОЛЬКО ДЛЯ СЕБЯ:*\n"
        "   • Баллов не получаешь\n"
        "   • Моёшь посуду сам\n\n"
        "Выберите действие:"
    )
    
    await query.edit_message_text(rules, parse_mode='Markdown', reply_markup=reply_markup)

async def cooked_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Запись готовки для всех"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if telegram not in USERS:
        await query.edit_message_text("❌ Вы не участник!")
        return
    
    user_name = USERS[telegram]
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    cook_id = execute_query(
        '''INSERT INTO tasks_done 
           (task, user_telegram, user_name, points, details, date)
           VALUES (?, ?, ?, ?, ?, ?)''',
        ('готовка', telegram, user_name, 3, 'для всех', now_str)
    )
        
    if not cook_id:
        await query.edit_message_text("❌ Ошибка при сохранении")
        return
    
    possible_confirmers = execute_query(
        "SELECT telegram, name FROM users WHERE telegram != ? AND is_home = 1",
        (telegram,)
    )
    
    if not possible_confirmers:
        await query.edit_message_text(
            f"✅ *Готовка записана!*\n\n"
            f"👤 {user_name} приготовил(а) для всех\n"
            f"⭐ 3 балла\n\n"
            f"Нет других дома для подтверждения.",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for conf_tg, conf_name in possible_confirmers:
        keyboard.append([
            InlineKeyboardButton(
                f"✅ {conf_name} подтверждает готовку",
                callback_data=f'confirm_{cook_id}_{conf_name}'
            )
        ])
    
    keyboard.append([InlineKeyboardButton(
        "🍽️ Я помыл посуду после этой готовки", 
        callback_data=f'dishes_{cook_id}'
    )])
    
    keyboard.append([InlineKeyboardButton("🏠 Назад", callback_data='menu_food')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ *Готовка записана!*\n\n"
        f"👤 {user_name} приготовил(а) для всех\n"
        f"⭐ 3 балла (нужно подтверждение)\n\n"
        f"Посуду должен мыть ТОТ, КТО КУШАЛ.",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def dishes_after_cooking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Помыл посуду после конкретной готовки"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    cook_id = int(data.replace('dishes_', ''))
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if telegram not in USERS:
        await query.edit_message_text("❌ Вы не участник!")
        return
    
    user_name = USERS[telegram]
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    task_id = execute_query(
        '''INSERT INTO tasks_done 
           (task, user_telegram, user_name, points, details, date)
           VALUES (?, ?, ?, ?, ?, ?)''',
        ('посуда', telegram, user_name, 2, f'после готовки #{cook_id}', now_str)
    )
        
    if not task_id:
        await query.edit_message_text("❌ Ошибка при сохранении")
        return
    
    possible_confirmers = execute_query(
        "SELECT telegram, name FROM users WHERE telegram != ? AND is_home = 1",
        (telegram,)
    )
    
    if not possible_confirmers:
        await query.edit_message_text(
            f"✅ *Записано!*\n\n"
            f"👤 {user_name} помыл(а) посуду\n"
            f"⭐ 2 балла\n\n"
            f"Нет других дома для подтверждения.",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for conf_tg, conf_name in possible_confirmers:
        keyboard.append([
            InlineKeyboardButton(
                f"✅ {conf_name} подтверждает мытьё посуды",
                callback_data=f'confirm_{task_id}_{conf_name}'
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🔄 *Подтвердите мытьё посуды*\n\n"
        f"👤 {user_name} помыл(а) посуду после готовки\n"
        f"⭐ 2 балла\n\n"
        f"Подтвердить может тот, кто тоже кушал:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def washed_dishes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Общая функция для мытья посуды"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if telegram not in USERS:
        await query.edit_message_text("❌ Вы не участник!")
        return
    
    user_name = USERS[telegram]
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    task_id = execute_query(
        '''INSERT INTO tasks_done 
           (task, user_telegram, user_name, points, date)
           VALUES (?, ?, ?, ?, ?)''',
        ('посуда', telegram, user_name, 2, now_str)
    )
        
    if not task_id:
        await query.edit_message_text("❌ Ошибка при сохранении")
        return
    
    possible_confirmers = execute_query(
        "SELECT telegram, name FROM users WHERE telegram != ? AND is_home = 1",
        (telegram,)
    )
    
    if not possible_confirmers:
        await query.edit_message_text(
            f"✅ *Записано!*\n\n"
            f"👤 {user_name} помыл(а) посуду\n"
            f"⭐ 2 балла\n\n"
            f"Нет других дома для подтверждения.",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for conf_tg, conf_name in possible_confirmers:
        keyboard.append([
            InlineKeyboardButton(
                f"✅ {conf_name} подтверждает",
                callback_data=f'confirm_{task_id}_{conf_name}'
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"🔄 *Подтвердите мытьё посуды*\n\n"
        f"👤 {user_name} помыл(а) посуду\n"
        f"⭐ 2 балла\n\n"
        f"Подтвердить может другой участник:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

# ==================== ШТРАФЫ ====================

async def menu_penalty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню штрафов"""
    query = update.callback_query
    await query.answer()
    
    keyboard = [
        [InlineKeyboardButton("💧 Не убрал за собой", callback_data='penalty_mess')],
        [InlineKeyboardButton("❌ Не сделал назначенное", callback_data='penalty_task')],
        [InlineKeyboardButton("🚮 Оставил мусор", callback_data='penalty_trash')],
        [InlineKeyboardButton("🏠 Назад", callback_data='main_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "⚠️ *ШТРАФНАЯ СИСТЕМА*\n\n"
        "• Не убрал за собой → -1 балл\n"
        "• Не сделал назначенное → -2 балла\n"
        "• Оставил мусор → -1 балл\n\n"
        "Штраф подтверждается другим участником.\n"
        "Тот, кто назначает штраф, не может его подтвердить!\n\n"
        f"Баланс не должен быть меньше: {MIN_BALANCE} баллов.\n\n"
        "Выберите нарушение:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def penalty_type_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор типа штрафа"""
    query = update.callback_query
    await query.answer()
    
    penalty_type = query.data
    
    penalties = {
        'penalty_mess': ('Не убрал за собой', -1),
        'penalty_task': ('Не сделал назначенное', -2),
        'penalty_trash': ('Оставил мусор', -1)
    }
    
    if penalty_type not in penalties:
        await query.edit_message_text("❌ Ошибка")
        return
    
    penalty_name, points = penalties[penalty_type]
    
    # Сохраняем в контексте
    context.user_data['penalty_info'] = {
        'name': penalty_name,
        'points': points
    }
    
    # Показываем список участников (кроме себя)
    user = query.from_user
    user_tg = f"@{user.username}" if user.username else user.first_name
    
    keyboard = []
    for telegram, name in USERS.items():
        if telegram != user_tg:
            keyboard.append([
                InlineKeyboardButton(
                    f"⚠️ {name}",
                    callback_data=f'penalty_user_{telegram}'
                )
            ])
    
    keyboard.append([InlineKeyboardButton("🏠 Назад", callback_data='menu_penalty')])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ *Кто нарушил?*\n\n"
        f"Нарушение: {penalty_name}\n"
        f"Штраф: {points} баллов\n\n"
        f"Баланс не ниже: {MIN_BALANCE} баллов.\n\n"
        "Выберите участника:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def create_penalty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Создание штрафа"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_tg = data.replace('penalty_user_', '')
    
    if 'penalty_info' not in context.user_data:
        await query.edit_message_text("❌ Информация о штрафе потеряна")
        return
    
    penalty_info = context.user_data['penalty_info']
    penalty_name = penalty_info['name']
    points = penalty_info['points']
    
    user_name = USERS.get(user_tg, user_tg)
    creator_tg = f"@{query.from_user.username}" if query.from_user.username else query.from_user.first_name
    creator_name = USERS.get(creator_tg, creator_tg)
    
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    penalty_id = execute_query(
        '''INSERT INTO tasks_done 
           (task, user_telegram, user_name, points, is_penalty, details, date)
           VALUES (?, ?, ?, ?, 1, ?, ?)''',
        (f"Штраф: {penalty_name}", user_tg, user_name, points,
         f"Назначил: {creator_name}", now_str)
    )
        
    if not penalty_id:
        await query.edit_message_text("❌ Ошибка при создании штрафа")
        return
    
    # Находим кто может подтвердить (кроме того кто назначил и того кому назначили)
    possible_confirmers = execute_query(
        '''SELECT telegram, name FROM users 
           WHERE telegram != ? AND telegram != ? AND is_home = 1''',
        (creator_tg, user_tg)
    )
    
    if not possible_confirmers:
        await query.edit_message_text(
            f"⚠️ *Штраф записан!*\n\n"
            f"👤 {user_name}\n"
            f"📝 {penalty_name}\n"
            f"⭐ Штраф: {points} баллов\n\n"
            f"Нет других участников для подтверждения штрафа.\n"
            f"Баланс не ниже: {MIN_BALANCE} баллов.",
            parse_mode='Markdown'
        )
        return
    
    keyboard = []
    for conf_tg, conf_name in possible_confirmers:
        keyboard.append([
            InlineKeyboardButton(
                f"✅ {conf_name} подтверждает штраф",
                callback_data=f'confirm_{penalty_id}_{conf_name}'
            )
        ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"⚠️ *Штраф создан!*\n\n"
        f"👤 {user_name}\n"
        f"📝 {penalty_name}\n"
        f"⭐ Штраф: {points} баллов\n"
        f"👮 Назначил: {creator_name}\n\n"
        f"Подтвердить может другой участник (не тот кто назначил).\n"
        f"Баланс не ниже: {MIN_BALANCE} баллов.",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

# ==================== СТАТИСТИКА ====================

async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать статистику"""
    query = update.callback_query
    await query.answer()
    
    current_time = datetime.now().strftime('%H:%M:%S')
    
    stats_text = (
        f"📊 *СТАТИСТИКА И БАЛАНСЫ*\n"
        f"🕒 Обновлено: {current_time}\n"
        f"🔻 Баланс не ниже: {MIN_BALANCE}\n\n"
    )
    
    # Для каждого участника
    for telegram, name in USERS.items():
        result = execute_query(
            "SELECT balance, is_home FROM users WHERE telegram = ?", (telegram,)
        )
        
        if result:
            balance, is_home = result[0]
            status = "🏠" if is_home else "✈️"
            
            week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
            week_result = execute_query(
                '''SELECT SUM(points) FROM tasks_done 
                   WHERE user_telegram = ? AND is_confirmed = 1 AND date > ?''',
                (telegram, week_ago)
            )
            
            week_points = week_result[0][0] if week_result and week_result[0][0] else 0
            
            stats_text += f"{status} *{name}:*\n"
            stats_text += f"  📊 Баланс: {balance} баллов\n"
            stats_text += f"  📈 За неделю: {week_points} баллов\n\n"
    
    # Самые частые задачи
    week_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d %H:%M:%S')
    frequent_result = execute_query(
        '''SELECT task, COUNT(*) as cnt FROM tasks_done 
           WHERE is_confirmed = 1 AND date > ? AND is_penalty = 0
           GROUP BY task ORDER BY cnt DESC LIMIT 3''', (week_ago,)
    )
    
    if frequent_result:
        stats_text += "🎯 *Частые задачи за неделю:*\n"
        for task, cnt in frequent_result:
            stats_text += f"• {task}: {cnt} раз\n"
    
    # Кнопки: обновить, по людям, назад
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data='stats_refresh')],
        [
            InlineKeyboardButton("👤 Матрос", callback_data='user_stats_@DILLC7'),
            InlineKeyboardButton("👤 Борода", callback_data='user_stats_@djumshut2000')
        ],
        [
            InlineKeyboardButton("👤 Даник", callback_data='user_stats_@naattive')
        ],
        [InlineKeyboardButton("🏠 Назад", callback_data='main_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        stats_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def refresh_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обновить статистику"""
    query = update.callback_query
    await query.answer()
    
    await show_stats(update, context)

async def show_user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подробная статистика по конкретному пользователю"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    user_tg = data.replace('user_stats_', '')
    
    if user_tg not in USERS:
        await query.edit_message_text("❌ Пользователь не найден")
        return
    
    user_name = USERS[user_tg]
    
    stats_text = f"📊 *Статистика: {user_name}*\n\n"
    
    rows = execute_query(
        '''SELECT date, task, points, confirmed_by, is_penalty, details, is_confirmed
           FROM tasks_done
           WHERE user_telegram = ?
           ORDER BY date DESC
           LIMIT 20''',
        (user_tg,)
    )
    
    if not rows:
        stats_text += "Пока нет записей.\n"
    else:
        for date_str, task, points, confirmed_by, is_penalty, details, is_confirmed in rows:
            dt = datetime.strptime(date_str, '%Y-%m-%d %H:%M:%S')
            date_human = dt.strftime('%d.%m %H:%M')
            kind = "штраф" if is_penalty else "задача"
            status = "✅" if is_confirmed else "⏳"
            conf_text = f" / подтверждён {confirmed_by}" if confirmed_by else ""
            details_text = f" / {details}" if details else ""
            
            stats_text += (
                f"{status} [{date_human}] {kind}: *{task}* "
                f"({points} балл.){conf_text}{details_text}\n"
            )
    
    keyboard = [
        [InlineKeyboardButton("⬅ Назад к статистике", callback_data='stats')],
        [InlineKeyboardButton("🏠 В главное меню", callback_data='main_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        stats_text,
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

# ==================== ОТЪЕЗД/ВОЗВРАЩЕНИЕ ====================

async def menu_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Меню смены статуса дома"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if telegram not in USERS:
        await query.edit_message_text("❌ Вы не участник!")
        return
    
    result = execute_query(
        "SELECT is_home FROM users WHERE telegram = ?", (telegram,)
    )
    
    if not result:
        await query.edit_message_text("❌ Ошибка базы данных")
        return
    
    is_home = result[0][0]
    user_name = USERS[telegram]
    
    action = "Уехать ✈️" if is_home else "Вернуться 🏠"
    callback = "leave" if is_home else "return"
    
    keyboard = [
        [InlineKeyboardButton(action, callback_data=callback)],
        [InlineKeyboardButton("🏠 Назад", callback_data='main_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    status = "дома 🏠" if is_home else "в отъезде ✈️"
    
    await query.edit_message_text(
        f"👤 *{user_name}*\n"
        f"Сейчас вы: {status}\n\n"
        f"Нажмите кнопку чтобы изменить статус:",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def toggle_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Смена статуса дома"""
    query = update.callback_query
    await query.answer()
    
    action = query.data
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    new_status = 0 if action == 'leave' else 1
    status_text = "уехал(а) ✈️" if new_status == 0 else "вернулся(ась) 🏠"
    
    execute_query(
        "UPDATE users SET is_home = ? WHERE telegram = ?",
        (new_status, telegram)
    )
    
    user_name = USERS[telegram]
    await query.edit_message_text(f"✅ {user_name} {status_text}!")

# ==================== ПРАВИЛА ====================

async def show_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать полные правила"""
    query = update.callback_query
    await query.answer()
    
    rules_text = (
        "📋 *ПОЛНЫЕ ПРАВИЛА СИСТЕМЫ*\n\n"
        
        "🎯 *Логика распределения задач:*\n"
        "• Кто дольше всех не делал задачу → тот делает\n"
        "• Уехавшие не участвуют в распределении\n"
        "• После возвращения не нужно 'догонять'\n"
        "• Баланс баллов может быть отрицательным, но не ниже "
        f"{MIN_BALANCE}\n\n"
        
        "✅ *Подтверждение задач:*\n"
        "• Подтверждает 1 другой участник\n"
        "• Нельзя подтверждать свою задачу или штраф\n"
        "• Если все в отъезде → запись ждёт подтверждения\n\n"
        
        "🍽️ *ПРАВИЛА ГОТОВКИ И ПОСУДЫ:*\n"
        "1. *Готовил для всех:*\n"
        "   • Получаешь 3 балла за готовку\n"
        "   • Посуду моет ТОТ, КТО КУШАЛ\n"
        "   • Кто не кушал → не обязан мыть\n\n"
        "2. *Готовил только для себя:*\n"
        "   • Баллов не получаешь\n"
        "   • Моёшь посуду сам\n\n"
        
        "⚠️ *ШТРАФНАЯ СИСТЕМА:*\n"
        "• Не убрал за собой → -1 балл\n"
        "• Не сделал назначенное → -2 балла\n"
        "• Оставил мусор → -1 балл\n"
        "• Штраф подтверждается другим участником\n"
        "• Тот, кто назначил штраф, не может его подтвердить\n"
        f"• Баланс при штрафах не опускается ниже {MIN_BALANCE}\n\n"
        
        "⚖️ *БАЛЛЬНАЯ СИСТЕМА:*\n"
        "• Санузел → 4 балла\n"
        "• Ванна → 3 балла\n"
        "• Кухня → 3 балла\n"
        "• Готовка для всех → 3 балла\n"
        "• Коридор/пылесос/посуда → 2 балла\n"
        "• Мусор → 1 балл\n\n"
        
        "🔧 *ЧТО ВХОДИТ В ЗАДАЧИ:*\n"
    )
    
    for task, info in TASKS.items():
        rules_text += f"\n• *{task.upper()}* ({info['points']} баллов):\n{info['rules']}\n"
    
    rules_text += "\n🏠 *Отъезд и возвращение:*\n"
    rules_text += "• Отмечайте отъезд заранее\n"
    rules_text += "• Уехавшие не получают новые задачи\n"
    rules_text += "• После возвращения продолжаете с того же места\n\n"
    
    rules_text += "📊 *Статистика:*\n"
    rules_text += "• Кнопка '🔄 Обновить' показывает актуальные данные\n"
    rules_text += "• Баллы не сбрасываются сами по себе, только через админку\n"
    rules_text += "• Есть подробные отчёты по каждому человеку\n"
    
    keyboard = [[InlineKeyboardButton("🏠 Назад", callback_data='main_menu')]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(rules_text, parse_mode='Markdown', reply_markup=reply_markup)

# ==================== АДМИНКА ====================

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Админ-меню"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if not is_admin(telegram):
        await query.edit_message_text("❌ У вас нет прав администратора")
        return
    
    keyboard = [
        [InlineKeyboardButton("♻ Полный сброс статистики", callback_data='admin_reset_confirm')],
        [InlineKeyboardButton("🏠 Назад", callback_data='main_menu')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "⚙ *АДМИНКА*\n\n"
        "• Здесь можно полностью очистить статистику.\n"
        "• Балансы будут обнулены.\n"
        "• Очередь по задачам будет сброшена.\n\n"
        "Будьте осторожны.",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def admin_reset_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Подтверждение сброса статистики"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if not is_admin(telegram):
        await query.edit_message_text("❌ У вас нет прав администратора")
        return
    
    keyboard = [
        [InlineKeyboardButton("✅ Да, сбросить всё", callback_data='admin_reset_yes')],
        [InlineKeyboardButton("❌ Нет, отмена", callback_data='admin_reset_no')]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        "⚠️ *ВНИМАНИЕ! ПОЛНЫЙ СБРОС СТАТИСТИКИ*\n\n"
        "Будет выполнено:\n"
        "• Удаление всех записей о задачах и штрафах\n"
        "• Обнуление балансов\n"
        "• Сброс очереди по задачам\n\n"
        "Действие необратимо.\n\n"
        "Ты точно хочешь продолжить?",
        parse_mode='Markdown',
        reply_markup=reply_markup
    )

async def admin_reset_yes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнить сброс статистики"""
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    telegram = f"@{user.username}" if user.username else user.first_name
    
    if not is_admin(telegram):
        await query.edit_message_text("❌ У вас нет прав администратора")
        return
    
    # Удаляем все задачи/штрафы
    execute_query("DELETE FROM tasks_done")
    # Обнуляем балансы
    execute_query("UPDATE users SET balance = 0")
    # Сбрасываем очередь
    execute_query("UPDATE queue SET last_user = 'никто', last_date = NULL")
    
    await query.edit_message_text(
        "✅ Вся статистика сброшена.\n"
        "• Все балансы обнулены.\n"
        "• Очередь задач очищена.\n\n"
        "Можно начинать с чистого листа."
    )

async def admin_reset_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена сброса статистики"""
    query = update.callback_query
    await query.answer()
    
    await admin_panel(update, context)

# ==================== ОБРАБОТЧИК КНОПОК ====================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Главный обработчик кнопок"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    try:
        if data == 'main_menu':
            await show_main_menu(update, context)
            
        elif data == 'menu_who':
            await menu_who(update, context)
            
        elif data.startswith('who_'):
            await process_who(update, context)
            
        elif data == 'menu_did':
            await menu_did(update, context)
            
        elif data.startswith('did_'):
            await process_did(update, context)
            
        elif data.startswith('confirm_'):
            await process_confirmation(update, context)
            
        elif data.startswith('cancel_'):
            await cancel_task(update, context)
            
        elif data == 'menu_food':
            await menu_food(update, context)
            
        elif data == 'cooked_all':
            await cooked_all(update, context)
            
        elif data.startswith('dishes_'):
            await dishes_after_cooking(update, context)
            
        elif data == 'washed_dishes':
            await washed_dishes(update, context)
            
        elif data == 'menu_penalty':
            await menu_penalty(update, context)
            
        elif data in ['penalty_mess', 'penalty_task', 'penalty_trash']:
            await penalty_type_selected(update, context)
            
        elif data.startswith('penalty_user_'):
            await create_penalty(update, context)
            
        elif data == 'stats':
            await show_stats(update, context)
            
        elif data == 'stats_refresh':
            await refresh_stats(update, context)
            
        elif data.startswith('user_stats_'):
            await show_user_stats(update, context)
            
        elif data == 'menu_home':
            await menu_home(update, context)
            
        elif data in ['leave', 'return']:
            await toggle_home(update, context)
            
        elif data == 'rules':
            await show_rules(update, context)
            
        elif data == 'admin_panel':
            await admin_panel(update, context)
            
        elif data == 'admin_reset_confirm':
            await admin_reset_confirm(update, context)
            
        elif data == 'admin_reset_yes':
            await admin_reset_yes(update, context)
            
        elif data == 'admin_reset_no':
            await admin_reset_no(update, context)
            
        else:
            await query.edit_message_text("❌ Неизвестная команда")
            
    except Exception as e:
        print(f"❌ Ошибка в обработчике кнопок: {e}")
        await query.edit_message_text("❌ Произошла ошибка, попробуйте ещё раз")

# ==================== ЗАПУСК БОТА ====================

def main():
    init_db()

    updater = Updater(TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_command))

    dp.add_handler(CallbackQueryHandler(main_menu, pattern="^main_menu$"))
    dp.add_handler(CallbackQueryHandler(show_tasks_menu, pattern="^tasks_menu$"))
    dp.add_handler(CallbackQueryHandler(process_task_choice, pattern="^task_"))
    dp.add_handler(CallbackQueryHandler(process_did, pattern="^did_"))
    dp.add_handler(CallbackQueryHandler(process_confirmation, pattern="^confirm_"))
    dp.add_handler(CallbackQueryHandler(cancel_task, pattern="^cancel_"))

    dp.add_handler(CallbackQueryHandler(menu_food, pattern="^menu_food$"))
    dp.add_handler(CallbackQueryHandler(cooked_all, pattern="^cooked_all$"))
    dp.add_handler(CallbackQueryHandler(dishes_after_cooking, pattern="^dishes_"))
    dp.add_handler(CallbackQueryHandler(washed_dishes, pattern="^washed_dishes$"))

    dp.add_handler(CallbackQueryHandler(menu_penalty, pattern="^menu_penalty$"))
    dp.add_handler(CallbackQueryHandler(penalty_mess, pattern="^penalty_mess$"))
    dp.add_handler(CallbackQueryHandler(penalty_task, pattern="^penalty_task$"))
    dp.add_handler(CallbackQueryHandler(penalty_trash, pattern="^penalty_trash$"))
    dp.add_handler(CallbackQueryHandler(process_penalty_reason, pattern="^penalty_reason_"))
    dp.add_handler(CallbackQueryHandler(confirm_penalty, pattern="^penalty_confirm_"))
    dp.add_handler(CallbackQueryHandler(cancel_penalty, pattern="^penalty_cancel_"))

    logging.info("Бот запущен, ждём сообщения...")
    updater.start_polling()
    updater.idle()


if __name__ == "__main__":
    main()

