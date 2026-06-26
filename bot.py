#!/usr/bin/env python3
"""
JNI Compiler Bot — компилятор JNI .so через GitHub Actions
Файл загружается в репо, потом триггерится сборка
"""

import os
import asyncio
import logging
import base64
import aiohttp
import aiofiles
import json
from datetime import datetime, date
from pathlib import Path
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

BOT_TOKEN     = os.environ["BOT_TOKEN"]
GH_TOKEN      = os.environ["GH_TOKEN"]
GH_REPO       = os.environ["GH_REPO"]
GH_WORKFLOW   = os.environ.get("GH_WORKFLOW", "compile.yml")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
MAX_WAIT      = int(os.environ.get("MAX_WAIT", "360"))

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"Bearer {GH_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ─── Хранилище профилей (в памяти) ───────────────────────────────────────────
# Формат: {user_id: {"name": str, "username": str, "first_seen": str, "total_builds": int, "today_builds": int, "last_build_date": str}}
PROFILES: dict = {}
DAILY_LIMIT = 3
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))  # Твой Telegram ID

def get_profile(user_id: int, user) -> dict:
    uid = str(user_id)
    today = date.today().isoformat()
    if uid not in PROFILES:
        PROFILES[uid] = {
            "name": user.full_name,
            "username": user.username or "",
            "first_seen": datetime.now().strftime("%d.%m.%Y"),
            "total_builds": 0,
            "today_builds": 0,
            "last_build_date": today,
        }
    else:
        # Сбрасываем дневной счётчик если новый день
        if PROFILES[uid]["last_build_date"] != today:
            PROFILES[uid]["today_builds"] = 0
            PROFILES[uid]["last_build_date"] = today
        # Обновляем имя/username если изменились
        PROFILES[uid]["name"] = user.full_name
        PROFILES[uid]["username"] = user.username or ""
    return PROFILES[uid]

def can_build(user_id: int) -> bool:
    uid = str(user_id)
    today = date.today().isoformat()
    if uid not in PROFILES:
        return True
    if PROFILES[uid]["last_build_date"] != today:
        return True
    return PROFILES[uid]["today_builds"] < DAILY_LIMIT

def increment_builds(user_id: int):
    global TOTAL_BUILDS_GLOBAL
    uid = str(user_id)
    PROFILES[uid]["total_builds"] += 1
    PROFILES[uid]["today_builds"] += 1
    TOTAL_BUILDS_GLOBAL += 1

NDK_VERSIONS = [
    "r21e", "r22b", "r23c",
    "r24",  "r25c", "r26d",
    "r27d", "r28c", "r29",
]

class JniBuild(StatesGroup):
    choosing_ndk = State()
    waiting_file = State()

bot = Bot(token=BOT_TOKEN)
dp  = Dispatcher(storage=MemoryStorage())

def ndk_keyboard() -> InlineKeyboardMarkup:
    buttons = []
    row = []
    for i, ver in enumerate(NDK_VERSIONS):
        row.append(InlineKeyboardButton(text=f"NDK {ver}", callback_data=f"ndk:{ver}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Отмена", callback_data="cancel")
    ]])

@dp.message(Command("start"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    get_profile(msg.from_user.id, msg.from_user)
    await msg.answer(
        "🔧 *JNI Compiler Bot*\n\n"
        "Компилирую JNI `.so` библиотеки для Android лаунчеров.\n\n"
        "🚀 *Что умею:*\n"
        "• Сборка JNI C/C++ в `.so`\n"
        "• arm64-v8a, armeabi-v7a, x86\\_64\n"
        "• NDK r21e — r29\n\n"
        "📋 *Команды:*\n"
        "• /jni — запустить сборку\n"
        "• /profile — мой профиль\n"
        "• /help — помощь\n",
        parse_mode="Markdown"
    )

@dp.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "📖 *Как пользоваться:*\n\n"
        "1️⃣ Напиши /jni\n"
        "2️⃣ Выбери версию NDK\n"
        "3️⃣ Отправь `.zip` архив с исходниками\n"
        "   Архив должен содержать `CMakeLists.txt` в корне\n"
        "4️⃣ Жди ~2-3 минуты — получишь `.so` файл\n\n"
        "📦 *Структура архива:*\n"
        "```\n"
        "sources.zip\n"
        "├── CMakeLists.txt\n"
        "└── src/\n"
        "    └── main.cpp\n"
        "```\n",
        parse_mode="Markdown"
    )

@dp.message(Command("profile"))
async def cmd_profile(msg: Message, state: FSMContext):
    user = msg.from_user
    p = get_profile(user.id, user)
    today = date.today().isoformat()
    if p["last_build_date"] != today:
        today_builds = 0
    else:
        today_builds = p["today_builds"]
    username = f"@{p['username']}" if p["username"] else "—"
    await msg.answer(
        f"👤 *Профиль*\n\n"
        f"🆔 ID: `{user.id}`\n"
        f"👤 Имя: {p['name']}\n"
        f"🔹 Имя пользователя: {username}\n\n"
        f"📊 *Статистика:*\n"
        f"• Статус: Обычный\n"
        f"• Сегодня: {today_builds}/{DAILY_LIMIT}\n"
        f"• Всего сборок: {p['total_builds']}\n\n"
        f"📅 *Даты:*\n"
        f"• Первый вход в бота: {p['first_seen']}",
        parse_mode="Markdown"
    )

@dp.message(Command("jni"))
async def cmd_jni(msg: Message, state: FSMContext):
    get_profile(msg.from_user.id, msg.from_user)
    if not can_build(msg.from_user.id):
        await msg.answer(
            f"⛔ *Лимит исчерпан!*\n\n"
            f"Сегодня ты уже использовал {DAILY_LIMIT}/{DAILY_LIMIT} сборок.\n"
            f"Приходи завтра! 🌙",
            parse_mode="Markdown"
        )
        return
    await state.clear()
    await state.set_state(JniBuild.choosing_ndk)
    await msg.answer("⚙️ *Выберите версию NDK:*", reply_markup=ndk_keyboard(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("ndk:"), JniBuild.choosing_ndk)
async def cb_ndk_chosen(cb: CallbackQuery, state: FSMContext):
    ndk = cb.data.split(":")[1]
    await state.update_data(ndk=ndk)
    await state.set_state(JniBuild.waiting_file)
    await cb.message.edit_text(
        f"✅ Выбран NDK: *{ndk}*\n\n"
        f"📁 Отправь `.zip` архив с исходниками (до 50 МБ)\n\n"
        f"Архив должен содержать `CMakeLists.txt` в корне.",
        reply_markup=cancel_keyboard(),
        parse_mode="Markdown"
    )

@dp.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Отменено. Напиши /jni чтобы начать заново.")

@dp.message(JniBuild.waiting_file, F.document)
async def on_file(msg: Message, state: FSMContext):
    doc = msg.document
    fname = doc.file_name or ""

    if not fname.lower().endswith(".zip"):
        await msg.answer("⚠️ Нужен `.zip` архив!")
        return

    if doc.file_size and doc.file_size > 50 * 1024 * 1024:
        await msg.answer("⚠️ Файл слишком большой! Максимум 50 МБ.")
        return

    data = await state.get_data()
    ndk  = data.get("ndk", "r25c")
    await state.clear()

    status_msg = await msg.answer("⏳ Скачиваю архив...")

    # Скачиваем zip
    tmp_path = Path(f"/tmp/src_{msg.from_user.id}.zip")
    try:
        tg_file = await bot.get_file(doc.file_id)
        await bot.download_file(tg_file.file_path, destination=tmp_path)
    except Exception as e:
        log.error(f"Download error: {e}")
        await status_msg.edit_text("❌ Не удалось скачать файл.")
        return

    await status_msg.edit_text("⏳ Загружаю и запускаю сборку...")

    # Загружаем zip в репо как файл sources/upload.zip
    gh_path = f"sources/upload_{msg.from_user.id}.zip"
    uploaded = await upload_file_to_github(tmp_path, gh_path)
    tmp_path.unlink(missing_ok=True)

    if not uploaded:
        await status_msg.edit_text("❌ Не удалось загрузить файл. Попробуй ещё раз.")
        return

    # Небольшая задержка перед запуском
    await asyncio.sleep(10)

    # Триггерим compile.yml
    run_id = await trigger_workflow(ndk, gh_path, msg.from_user.id)

    if not run_id:
        await status_msg.edit_text("❌ Не удалось запустить сборку.")
        return

    await status_msg.edit_text(
        f"🔨 Сборка запущена! NDK: *{ndk}*\n"
        f"⏳ Жди 2-3 минуты...",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

    asyncio.create_task(
        poll_and_send(msg.chat.id, run_id, ndk, gh_path, status_msg.message_id)
    )

async def upload_file_to_github(local_path: Path, gh_path: str) -> bool:
    """Загружает файл в репо через GitHub Contents API"""
    async with aiofiles.open(local_path, "rb") as f:
        content = base64.b64encode(await f.read()).decode()

    # Проверяем существует ли файл (нужен sha для обновления)
    sha = None
    url = f"{GH_API}/repos/{GH_REPO}/contents/{gh_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=GH_HEADERS) as resp:
            if resp.status == 200:
                data = await resp.json()
                sha = data.get("sha")

        payload = {
            "message": f"Upload sources for compilation",
            "content": content,
        }
        if sha:
            payload["sha"] = sha

        async with session.put(url, headers=GH_HEADERS, json=payload) as resp:
            if resp.status in (200, 201):
                return True
            log.error(f"GitHub upload error {resp.status}: {await resp.text()}")
            return False

async def trigger_workflow(ndk: str, gh_path: str, user_id: int) -> str | None:
    """Триггерит compile.yml через workflow_dispatch"""
    payload = {
        "ref": "main",
        "inputs": {
            "ndk_version": ndk,
            "user_id": str(user_id),
            "zip_path": gh_path,
        }
    }
    url = f"{GH_API}/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/dispatches"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=GH_HEADERS, json=payload) as resp:
            if resp.status != 204:
                log.error(f"Workflow dispatch error {resp.status}: {await resp.text()}")
                return None

    await asyncio.sleep(5)

    runs_url = f"{GH_API}/repos/{GH_REPO}/actions/workflows/{GH_WORKFLOW}/runs?per_page=1"
    async with aiohttp.ClientSession() as session:
        async with session.get(runs_url, headers=GH_HEADERS) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            runs = data.get("workflow_runs", [])
            return str(runs[0]["id"]) if runs else None

async def poll_and_send(chat_id: int, run_id: str, ndk: str, gh_path: str, status_msg_id: int):
    waited = 0
    while waited < MAX_WAIT:
        await asyncio.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL

        status, conclusion = await get_run_status(run_id)
        log.info(f"Run {run_id}: {status}/{conclusion}")

        if status != "completed":
            continue

        # Удаляем загруженный zip из репо
        asyncio.create_task(delete_file_from_github(gh_path))

        if conclusion == "success":
            so_path = await download_artifact(run_id)
            if so_path:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=status_msg_id,
                    text=f"✅ *Готово!* NDK: `{ndk}`\n📦 Отправляю `.so`...",
                    parse_mode="Markdown"
                )
                await bot.send_document(
                    chat_id=chat_id,
                    document=FSInputFile(so_path),
                    caption=f"✅ *JNI сборка готова!*\n🔧 NDK: `{ndk}`",
                    parse_mode="Markdown"
                )
                Path(so_path).unlink(missing_ok=True)
                increment_builds(chat_id)
            else:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=status_msg_id,
                    text="⚠️ Сборка прошла, но `.so` не найден в артефактах."
                )
        else:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=status_msg_id,
                text=(
                    f"❌ *Сборка провалилась!*\n"
                    f"Проверь исходники и попробуй снова."
                ),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        return

    await bot.edit_message_text(
        chat_id=chat_id, message_id=status_msg_id,
        text="⏰ Таймаут! Сборка заняла слишком долго. Попробуй снова.",
        parse_mode="Markdown",
        disable_web_page_preview=True
    )

async def get_run_status(run_id: str) -> tuple[str, str]:
    url = f"{GH_API}/repos/{GH_REPO}/actions/runs/{run_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=GH_HEADERS) as resp:
            if resp.status != 200:
                return "unknown", ""
            data = await resp.json()
            return data.get("status", ""), data.get("conclusion", "") or ""

async def download_artifact(run_id: str) -> str | None:
    import zipfile
    url = f"{GH_API}/repos/{GH_REPO}/actions/runs/{run_id}/artifacts"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=GH_HEADERS) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
            arts = data.get("artifacts", [])
            if not arts:
                return None
            dl_url = arts[0]["archive_download_url"]

        async with session.get(dl_url, headers=GH_HEADERS, allow_redirects=True) as resp:
            if resp.status != 200:
                return None
            art_zip = Path(f"/tmp/artifact_{run_id}.zip")
            async with aiofiles.open(art_zip, "wb") as f:
                await f.write(await resp.read())

    out_dir = Path(f"/tmp/art_{run_id}")
    out_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(art_zip, "r") as z:
        z.extractall(out_dir)
    art_zip.unlink(missing_ok=True)

    for so in out_dir.rglob("*.so"):
        dest = Path(f"/tmp/{so.name}")
        so.rename(dest)
        return str(dest)
    return None

async def delete_file_from_github(gh_path: str):
    """Удаляет временный zip из репо после сборки"""
    url = f"{GH_API}/repos/{GH_REPO}/contents/{gh_path}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=GH_HEADERS) as resp:
            if resp.status != 200:
                return
            data = await resp.json()
            sha = data.get("sha")

        await session.delete(url, headers=GH_HEADERS, json={
            "message": "Remove temp sources after build",
            "sha": sha
        })

# ─── Проверка админа ──────────────────────────────────────────────────────────
def is_admin(user_id: int) -> bool:
    return ADMIN_ID != 0 and user_id == ADMIN_ID

# ─── /limit (username) (число) ────────────────────────────────────────────────
@dp.message(Command("limit"))
async def cmd_limit(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Нет доступа.")
        return

    args = msg.text.split()
    if len(args) < 3:
        await msg.answer("❌ Использование: /limit @username 3")
        return

    target_username = args[1].lstrip("@").lower()
    try:
        add_count = int(args[2])
    except ValueError:
        await msg.answer("❌ Число должно быть целым. Пример: /limit @user 2")
        return

    # Ищем пользователя по username
    found_uid = None
    for uid, p in PROFILES.items():
        if p.get("username", "").lower() == target_username:
            found_uid = uid
            break

    if not found_uid:
        await msg.answer(f"❌ Пользователь @{target_username} не найден.\nОн должен был написать боту хотя бы раз.")
        return

    today = date.today().isoformat()
    if PROFILES[found_uid]["last_build_date"] != today:
        PROFILES[found_uid]["today_builds"] = 0
        PROFILES[found_uid]["last_build_date"] = today

    # Уменьшаем today_builds чтобы добавить лимит
    PROFILES[found_uid]["today_builds"] = max(0, PROFILES[found_uid]["today_builds"] - add_count)
    remaining = DAILY_LIMIT - PROFILES[found_uid]["today_builds"]

    await msg.answer(
        f"✅ Добавлено *{add_count}* компиляций для @{target_username}\n"
        f"Осталось сегодня: *{remaining}/{DAILY_LIMIT}*",
        parse_mode="Markdown"
    )

# ─── /stats ───────────────────────────────────────────────────────────────────
@dp.message(Command("stats"))
async def cmd_stats(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Нет доступа.")
        return

    total_users = len(PROFILES)
    today = date.today().isoformat()
    active_today = sum(
        1 for p in PROFILES.values()
        if p.get("last_build_date") == today and p.get("today_builds", 0) > 0
    )

    await msg.answer(
        f"📊 *Статистика бота*\n\n"
        f"👥 Всего пользователей: *{total_users}*\n"
        f"🔥 Активных сегодня: *{active_today}*\n"
        f"🔨 Всего скомпилировано: *{TOTAL_BUILDS_GLOBAL}*",
        parse_mode="Markdown"
    )

# ─── /tab ─────────────────────────────────────────────────────────────────────
@dp.message(Command("tab"))
async def cmd_tab(msg: Message):
    if not is_admin(msg.from_user.id):
        await msg.answer("⛔ Нет доступа.")
        return

    if not PROFILES:
        await msg.answer("📭 Пока нет пользователей.")
        return

    today = date.today().isoformat()
    lines = ["👥 *Список пользователей:*\n"]

    for i, (uid, p) in enumerate(PROFILES.items(), 1):
        name = p.get("name", "—")
        username = f"@{p['username']}" if p.get("username") else "—"
        total = p.get("total_builds", 0)
        first_seen = p.get("first_seen", "—")

        if p.get("last_build_date") == today:
            used = p.get("today_builds", 0)
        else:
            used = 0
        remaining = max(0, DAILY_LIMIT - used)

        lines.append(
            f"*{i}.* {name} | {username}\n"
            f"   ⏳ Лимит: {remaining}/{DAILY_LIMIT} | 🔨 Всего: {total} | 📅 {first_seen}\n"
        )

    # Разбиваем на части если много пользователей
    text = "\n".join(lines)
    if len(text) > 4000:
        chunks = []
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) > 4000:
                chunks.append(chunk)
                chunk = line
            else:
                chunk += "\n" + line
        chunks.append(chunk)
        for chunk in chunks:
            await msg.answer(chunk, parse_mode="Markdown")
    else:
        await msg.answer(text, parse_mode="Markdown")

async def main():
    log.info("Starting JNI Compiler Bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
