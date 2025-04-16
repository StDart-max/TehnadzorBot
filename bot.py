import base64
import json
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Токен бота берём из переменной окружения
TOKEN = os.getenv("TOKEN")
logging.info("Токен бота загружен из переменной окружения")

# Список пользователей (Telegram ID)
USERS = [358155028]  # Твой Telegram ID

# ID админа
MAIN_ADMIN_ID = 358155028

# Таймауты (в секундах)
QUESTION_TIMEOUT = 1200  # 20 минут
REMINDER_TIMEOUT = 600  # 10 минут

# Настройка Google Таблиц
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_json_base64 = os.getenv("GOOGLE_CREDENTIALS")
creds_json = base64.b64decode(creds_json_base64).decode("utf-8")
creds_dict = json.loads(creds_json)
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)
sheet = client.open_by_key(os.getenv("SPREADSHEET_ID")).sheet1
logging.info("Подключение к Google Таблицам выполнено")

# Хранилище для ожидающих ответа пользователей
waiting_for_response = {}

# Команда /start
async def start(update: Update, context):
    logging.info(f"Команда /start от пользователя {update.message.from_user.id}")
    await update.message.reply_text("Привет! Я бот для сбора отчётов о ямочном ремонте. Жди вопросов или используй /report для отчёта.")

# Команда /report
async def report(update: Update, context):
    logging.info(f"Команда /report от пользователя {update.message.from_user.id}")
    try:
        end_date = datetime.now().date() - timedelta(days=1)  # Вчера
        start_date = end_date - timedelta(days=6)  # 7 дней назад
        date_range = [start_date + timedelta(days=x) for x in range(7)]

        report_data = {date: {"crews": 0, "people": 0, "equipment": 0, "total": 0, "critical": 0} for date in date_range}

        records = sheet.get_all_records()
        for record in records:
            if "Отчёт за дату" not in record:
                continue
            try:
                record_date = datetime.strptime(record["Отчёт за дату"], "%d.%m.%Y").date()
                if start_date <= record_date <= end_date:
                    report_data[record_date]["crews"] += int(record.get("Бригад", 0))
                    report_data[record_date]["people"] += int(record.get("Людей", 0))
                    report_data[record_date]["equipment"] += int(record.get("Техники", 0))
                    report_data[record_date]["total"] += float(record.get("Всего м²", 0))
                    report_data[record_date]["critical"] += float(record.get("Критической м²", 0))
            except (ValueError, KeyError):
                continue

        report_text = "Отчёт об устранении ямочности за последние 7 дней:\n(Дата, Бригад, Людей, Техники, Всего м², Критической м²)\n"
        total_m2, total_critical = 0, 0

        for date in date_range:
            data = report_data.get(date, {"crews": 0, "people": 0, "equipment": 0, "total": 0, "critical": 0})
            report_text += (
                f"{date}, {data['crews']} бригад, {data['people']} чел, "
                f"{data['equipment']} ед, {data['total']} м², {data['critical']} м²\n"
            )
            total_m2 += data["total"]
            total_critical += data["critical"]

        report_text += f"\nИтого за неделю: {total_m2} м² всего, {total_critical} м² критической"
        await update.message.reply_text(report_text)
    except Exception as e:
        await update.message.reply_text(f"Ошибка при получении отчёта: {e}")

# Функция для отправки вопросов
async def send_questions(application: Application, offset: int, user_id: int, day_index: int = None):
    # Динамически вычисляем дату на момент выполнения задачи
    report_date = (datetime.now() + timedelta(days=offset)).strftime("%d.%m.%Y")
    logging.info(f"Функция send_questions запущена для user_id={user_id}, report_date={report_date}, day_index={day_index}")
    
    question = f"Выполняли ли вы ямочный ремонт {report_date}?"
    
    waiting_for_response[user_id] = {"date": report_date, "step": "initial", "day_index": day_index}
    keyboard = [
        [InlineKeyboardButton("Да", callback_data=f"yes_{report_date}_{user_id}"),
         InlineKeyboardButton("Нет", callback_data=f"no_{report_date}_{user_id}")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await application.bot.send_message(
        chat_id=user_id, text=question, reply_markup=reply_markup
    )
    
    application.job_queue.run_once(
        remind_user, REMINDER_TIMEOUT, data=user_id, name=str(user_id)
    )
    application.job_queue.run_once(
        notify_admin, QUESTION_TIMEOUT, data=user_id, name=f"notify_{user_id}"
    )
    
    logging.info(f"Сообщение отправлено пользователю {user_id}")

# Напоминание пользователю
async def remind_user(context):
    user_id = context.job.data
    if user_id in waiting_for_response:
        await context.bot.send_message(
            chat_id=user_id, text="Пожалуйста, ответьте на вопрос о ямочном ремонте."
        )

# Уведомление админу
async def notify_admin(context):
    user_id = context.job.data
    if user_id in waiting_for_response:
        await context.bot.send_message(
            chat_id=MAIN_ADMIN_ID, text=f"Нет ответа от пользователя с ID {user_id}!"
        )
        del waiting_for_response[user_id]

# Обработка ответа на кнопки
async def handle_response(update: Update, context):
    query = update.callback_query
    await query.answer()
    data = query.data.split("_")
    response, date_str, user_id = data[0], data[1], int(data[2])

    if user_id not in waiting_for_response:
        await query.message.reply_text("Сессия истекла. Пожалуйста, дождитесь нового вопроса.")
        return

    day_index = waiting_for_response[user_id].get("day_index")

    if response == "no":
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([now, user_id, date_str, 0, 0, 0, 0, 0])
        await query.message.reply_text("Спасибо за ответ!")
    else:
        waiting_for_response[user_id]["step"] = "crew"
        await query.message.reply_text("Сколько работало бригад?")
        return  # Не удаляем из waiting_for_response, ждём данные

    # Если это понедельник утром, задаём следующий вопрос
    if day_index is not None and day_index < 2:
        next_index = day_index + 1
        offsets = [-3, -2, -1]  # Пятница, суббота, воскресенье
        report_date = (datetime.now() + timedelta(days=offsets[next_index])).strftime("%d.%m.%Y")
        context.job_queue.run_once(
            lambda ctx: send_questions(ctx.application, report_date, user_id, next_index),
            1,  # Запуск через 1 секунду
            data=user_id,
            name=f"next_question_{user_id}_{next_index}"
        )

    del waiting_for_response[user_id]

# Обработка текстовых ответов
async def handle_text(update: Update, context):
    user_id = update.message.from_user.id
    if user_id not in waiting_for_response:
        await update.message.reply_text("Пожалуйста, дождитесь вопроса.")
        return

    step = waiting_for_response[user_id]["step"]
    date_str = waiting_for_response[user_id]["date"]
    day_index = waiting_for_response[user_id].get("day_index")
    text = update.message.text

    try:
        if step == "crew":
            waiting_for_response[user_id]["crew_count"] = int(text)
            waiting_for_response[user_id]["step"] = "people"
            await update.message.reply_text("Сколько работало человек?")
        elif step == "people":
            waiting_for_response[user_id]["people_count"] = int(text)
            waiting_for_response[user_id]["step"] = "equipment"
            await update.message.reply_text("Сколько единиц техники?")
        elif step == "equipment":
            waiting_for_response[user_id]["equipment_count"] = int(text)
            waiting_for_response[user_id]["step"] = "total_repair"
            await update.message.reply_text("Сколько устранено ямочности всего (м²)?")
        elif step == "total_repair":
            waiting_for_response[user_id]["total_repair"] = float(text)
            waiting_for_response[user_id]["step"] = "critical_repair"
            await update.message.reply_text("Сколько устранено критической ямочности (м²)?")
        elif step == "critical_repair":
            waiting_for_response[user_id]["critical_repair"] = float(text)
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet.append_row([
                now, user_id, date_str,
                waiting_for_response[user_id]["crew_count"],
                waiting_for_response[user_id]["people_count"],
                waiting_for_response[user_id]["equipment_count"],
                waiting_for_response[user_id]["total_repair"],
                waiting_for_response[user_id]["critical_repair"]
            ])
            await update.message.reply_text("Информация собрана. Спасибо!")
            
            # Если это понедельник утром, задаём следующий вопрос
            if day_index is not None and day_index < 2:
                next_index = day_index + 1
                offsets = [-3, -2, -1]  # Пятница, суббота, воскресенье
                report_date = (datetime.now() + timedelta(days=offsets[next_index])).strftime("%d.%m.%Y")
                context.job_queue.run_once(
                    lambda ctx: send_questions(ctx.application, report_date, user_id, next_index),
                    1,  # Запуск через 1 секунду
                    data=user_id,
                    name=f"next_question_{user_id}_{next_index}"
                )
            
            del waiting_for_response[user_id]
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите число. Попробуйте снова.")

# Настройка планировщика
async def post_init(application: Application):
    logging.info("Инициализация планировщика начата")
    scheduler = AsyncIOScheduler()

    # Сложное расписание (время в UTC, Минск = UTC+3)
    schedules = [
        # Тест: каждые 10 секунд
        {"day": "*", "hour": "*", "minute": "*", "second": "*/10", "offset": -1, "day_index": None},
        # Понедельник 08:10 Минск (05:10 UTC): за пятницу
        {"day": "mon", "hour": 5, "minute": 10, "offset": -3, "day_index": 0},
        # Понедельник 14:10 Минск (11:10 UTC): за понедельник
        {"day": "mon", "hour": 11, "minute": 10, "offset": 0, "day_index": None},
        # Среда 15:30 Минск (12:30 UTC): за вторник
        {"day": "wed", "hour": 12, "minute": 30, "offset": -1, "day_index": None},
        # Четверг 08:10 Минск (05:10 UTC): за среду
        {"day": "thu", "hour": 5, "minute": 10, "offset": -1, "day_index": None},
        # Пятница 08:10 Минск (05:10 UTC): за четверг
        {"day": "fri", "hour": 5, "minute": 10, "offset": -1, "day_index": None},
    ]

    for schedule in schedules:
        for user_id in USERS:
            second = schedule.get("second", 0)  # Учитываем поле second из расписания
            logging.info(f"Добавляю задачу для user_id={user_id}, день={schedule['day']}, время={schedule['hour']}:{schedule['minute']}:{second} UTC, offset={schedule['offset']}")
            scheduler.add_job(
                send_questions,
                trigger=CronTrigger(
                    day_of_week=schedule["day"],
                    hour=schedule["hour"],
                    minute=schedule["minute"],
                    second=second,  # Теперь second берётся из расписания
                    timezone="UTC"
                ),
                args=[application, schedule["offset"], user_id, schedule["day_index"]]
            )

    scheduler.start()
    logging.info("Планировщик успешно запущен")

def main():
    logging.info("Запуск бота начат")
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("report", report))
    app.add_handler(CallbackQueryHandler(handle_response))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    print("Бот запущен...")
    logging.info("Бот успешно запущен, начинаю polling")
    app.run_polling()

if __name__ == "__main__":
    main()
