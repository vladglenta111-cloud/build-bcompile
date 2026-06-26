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
    await msg.answer(
        "🔧 *JNI Compiler Bot*\n\n"
        "Компилирую JNI `.so` библиотеки для Android лаунчеров.\n\n"
        "🚀 *Что умею:*\n"
        "• Сборка JNI C/C++ в `.so`\n"
        "• arm64-v8a, armeabi-v7a, x86\\_64\n"
        "• NDK r21e — r29\n\n"
        "📋 *Команды:*\n"
        "• /jni — запустить сборку\n"
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

@dp.message(Command("jni"))
async def cmd_jni(msg: Message, state: FSMContext):
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

    await status_msg.edit_text("⏳ Загружаю в GitHub и запускаю сборку...")

    # Загружаем zip в репо как файл sources/upload.zip
    gh_path = f"sources/upload_{msg.from_user.id}.zip"
    uploaded = await upload_file_to_github(tmp_path, gh_path)
    tmp_path.unlink(missing_ok=True)

    if not uploaded:
        await status_msg.edit_text("❌ Не удалось загрузить файл в GitHub.\nПроверь GH_TOKEN — нужны права `repo` + `workflow`.")
        return

    # Ждём чтобы GitHub точно увидел загруженный файл
    await asyncio.sleep(10)

    # Триггерим compile.yml
    run_id = await trigger_workflow(ndk, gh_path, msg.from_user.id)

    if not run_id:
        await status_msg.edit_text("❌ Не удалось запустить сборку.")
        return

    await status_msg.edit_text(
        f"🔨 Сборка запущена! NDK: *{ndk}*\n"
        f"⏳ Жди 2-3 минуты...\n\n"
        f"🔗 [Логи](https://github.com/{GH_REPO}/actions/runs/{run_id})",
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
                    f"🔗 [Смотреть логи](https://github.com/{GH_REPO}/actions/runs/{run_id})"
                ),
                parse_mode="Markdown",
                disable_web_page_preview=True
            )
        return

    await bot.edit_message_text(
        chat_id=chat_id, message_id=status_msg_id,
        text=f"⏰ Таймаут! Сборка заняла больше {MAX_WAIT//60} мин.\n🔗 [Логи](https://github.com/{GH_REPO}/actions/runs/{run_id})",
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

async def main():
    log.info("Starting JNI Compiler Bot...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
