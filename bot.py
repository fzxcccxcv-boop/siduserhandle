import asyncio
import html
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import psutil
from telethon import TelegramClient, events, utils
from telethon.errors import (
    AuthKeyUnregisteredError,
    AuthRestartError,
    ChatAdminRequiredError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    PhoneNumberInvalidError,
    RightForbiddenError,
    SessionPasswordNeededError,
    SessionRevokedError,
    UserAlreadyParticipantError,
    UserDeactivatedBanError,
    UserNotParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.account import GetPasswordRequest, UpdateProfileRequest
from telethon.tl.functions.channels import (
    EditAdminRequest,
    GetParticipantRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.messages import ImportChatInviteRequest
from telethon.tl.types import (
    ChannelParticipantAdmin,
    ChannelParticipantCreator,
    ChatAdminRights,
)

# ==============================================================================
# FILE PATHS & JSON STORAGE ENGINE
# ==============================================================================
CONFIG_FILE = "config.json"
SLOTS_FILE = "slots.json"
OWNERS_FILE = "owners.json"
COMMANDS_FILE = "commands.json"
LOGS_FILE = "audit_logs.json"

CLIENT_DEVICE = "Desktop Sakura"
CLIENT_SYSTEM = "Windows 11"
CLIENT_APP = "5.6.3 x64"

DEFAULT_FILES = {
    CONFIG_FILE: {
        "api_id": 12345678,
        "api_hash": "your_telegram_api_hash_here",
        "bot_token": "your_bot_token_here",
        "main_owner_id": 123456789,
        "brand_name": "𝗦𝗜𝗗",
        "max_slots": 25,
        "banner_photo": "",
        "auto_promote_title": "Sid Admin",
        "join_delay_sec": 4,
        "watchdog_interval_min": 30,
    },
    SLOTS_FILE: {"slots": {}},
    OWNERS_FILE: {"main_owner": 123456789, "co_owners": []},
    COMMANDS_FILE: {
        "commands": {
            "ALL:.welcome": {
                "trigger": ".welcome",
                "scope": "ALL",
                "reply": "🌸 Welcome! Managed by Slot #{slot} ({owner}).",
                "created_by": 123456789,
            }
        }
    },
    LOGS_FILE: {"logs": []},
}


def ensure_storage() -> None:
    for path, default_data in DEFAULT_FILES.items():
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default_data, f, indent=2, ensure_ascii=False)


def read_json(path: str) -> Dict[str, Any]:
    ensure_storage()
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if path == COMMANDS_FILE and "commands" in data:
        migrated: Dict[str, Any] = {}
        changed = False
        for k, v in data["commands"].items():
            if ":" not in k:
                scope = str(v.get("scope", "ALL"))
                trig = k.lower()
                migrated[f"{scope}:{trig}"] = {
                    "trigger": trig,
                    "scope": scope,
                    "reply": v.get("reply", ""),
                    "created_by": v.get("created_by", 0),
                }
                changed = True
            else:
                migrated[k] = v
        if changed:
            data["commands"] = migrated
            write_json(COMMANDS_FILE, data)

    return data


def write_json(path: str, data: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def now_str() -> str:
    return datetime.now().strftime("%d %b %Y • %I:%M %p")


def log_audit(action: str, actor_id: int, details: str) -> None:
    data = read_json(LOGS_FILE)
    logs = data.get("logs", [])
    logs.insert(
        0,
        {
            "timestamp": now_str(),
            "action": action,
            "actor_id": actor_id,
            "details": details,
        },
    )
    data["logs"] = logs[:500]
    write_json(LOGS_FILE, data)


def build_userbot_client(session_str: str = "", timeout: int = 15) -> TelegramClient:
    return TelegramClient(
        StringSession(session_str),
        API_ID,
        API_HASH,
        loop=LOOP,
        device_model=CLIENT_DEVICE,
        system_version=CLIENT_SYSTEM,
        app_version=CLIENT_APP,
        lang_code="en",
        system_lang_code="en-US",
        connection_retries=2,
        retry_delay=1,
        timeout=timeout,
    )


# ==============================================================================
# AUTHORIZATION, RUNTIME WORKERS & STATE (PYTHON 3.14 EVENT LOOP SAFE)
# ==============================================================================
ensure_storage()
CFG = read_json(CONFIG_FILE)

API_ID = int(CFG["api_id"])
API_HASH = str(CFG["api_hash"])
BOT_TOKEN = str(CFG["bot_token"])
BRAND = str(CFG.get("brand_name", "𝗦𝗜𝗗"))

owners_init = read_json(OWNERS_FILE)
if owners_init.get("main_owner") != int(CFG["main_owner_id"]):
    owners_init["main_owner"] = int(CFG["main_owner_id"])
    write_json(OWNERS_FILE, owners_init)

USER_STATES: Dict[Tuple[int, int], Dict[str, Any]] = {}
PENDING_LOGINS: Dict[Tuple[int, int], Dict[str, Any]] = {}
ACTIVE_WORKERS: Dict[str, Dict[str, Any]] = {}
START_TIME = time.time()

LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(LOOP)
bot = TelegramClient("sid_handler_bot", API_ID, API_HASH, loop=LOOP)


# ==============================================================================
# TELEGRAM BOT API ENGINE (REAL COLORED INLINE BUTTONS: GREEN / RED / BLUE)
# ==============================================================================
# Telegram Bot API InlineKeyboardButton `style` values:
#   "success" -> Green
#   "danger"  -> Red
#   "primary" -> Blue
def btn(text: str, callback_data: str, style: Optional[str] = None) -> Dict[str, str]:
    item: Dict[str, str] = {"text": text, "callback_data": callback_data}
    if style in ("primary", "success", "danger"):
        item["style"] = style
    return item


def md_to_html(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    escaped = re.sub(r"(?<!\*)\*([^*\n]+?)\*(?!\*)", r"<i>\1</i>", escaped)
    return escaped


def _sync_bot_api_post(
    method: str, payload: Dict[str, Any], file_field: Optional[Tuple[str, str]] = None
) -> Dict[str, Any]:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"

    if file_field:
        field_name, file_path = file_field
        boundary = f"----SakuraBoundary{uuid.uuid4().hex}"
        body_parts: List[bytes] = []

        for k, v in payload.items():
            if v is None:
                continue
            val_str = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            body_parts.append(
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n'
                f"{val_str}\r\n".encode("utf-8")
            )

        filename = os.path.basename(file_path)
        with open(file_path, "rb") as fp:
            file_bytes = fp.read()

        body_parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
            f"Content-Type: image/jpeg\r\n\r\n".encode("utf-8")
            + file_bytes
            + b"\r\n"
        )
        body_parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        raw_body = b"".join(body_parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    else:
        clean_payload = {k: v for k, v in payload.items() if v is not None}
        raw_body = json.dumps(clean_payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}

    req = urllib.request.Request(url, data=raw_body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            return json.loads(err.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "description": str(err)}
    except Exception as exc:
        return {"ok": False, "description": str(exc)}


async def bot_api_call(
    method: str, payload: Dict[str, Any], file_field: Optional[Tuple[str, str]] = None
) -> Dict[str, Any]:
    return await asyncio.to_thread(_sync_bot_api_post, method, payload, file_field)


async def api_send(
    chat_id: int,
    text: str,
    buttons: Optional[List[List[Dict[str, str]]]] = None,
    photo: Optional[str] = None,
    reply_to: Optional[int] = None,
) -> Optional[int]:
    peer_id = utils.get_peer_id(chat_id)
    markup = {"inline_keyboard": buttons} if buttons else None
    html_text = md_to_html(text)

    if photo:
        is_local = os.path.isfile(photo)
        payload: Dict[str, Any] = {
            "chat_id": peer_id,
            "caption": html_text,
            "parse_mode": "HTML",
            "reply_markup": markup,
            "reply_to_message_id": reply_to,
        }
        if is_local:
            res = await bot_api_call("sendPhoto", payload, file_field=("photo", photo))
        else:
            payload["photo"] = photo
            res = await bot_api_call("sendPhoto", payload)

        if res.get("ok"):
            result = res.get("result", {})
            if is_local and result.get("photo"):
                cached_id = result["photo"][-1].get("file_id")
                if cached_id:
                    cfg = read_json(CONFIG_FILE)
                    cfg["banner_photo"] = cached_id
                    write_json(CONFIG_FILE, cfg)
            return result.get("message_id")

    msg_payload: Dict[str, Any] = {
        "chat_id": peer_id,
        "text": html_text,
        "parse_mode": "HTML",
        "reply_markup": markup,
        "reply_to_message_id": reply_to,
        "disable_web_page_preview": True,
    }
    res = await bot_api_call("sendMessage", msg_payload)
    if not res.get("ok") and "parse" in str(res.get("description", "")).lower():
        msg_payload["text"] = text
        msg_payload.pop("parse_mode", None)
        res = await bot_api_call("sendMessage", msg_payload)
    if res.get("ok"):
        return res.get("result", {}).get("message_id")
    return None


async def api_edit(
    chat_id: int,
    message_id: int,
    text: str,
    buttons: Optional[List[List[Dict[str, str]]]] = None,
    is_caption: bool = False,
) -> bool:
    peer_id = utils.get_peer_id(chat_id)
    markup = {"inline_keyboard": buttons} if buttons else None
    html_text = md_to_html(text)

    if is_caption:
        payload: Dict[str, Any] = {
            "chat_id": peer_id,
            "message_id": message_id,
            "caption": html_text,
            "parse_mode": "HTML",
            "reply_markup": markup,
        }
        res = await bot_api_call("editMessageCaption", payload)
        if res.get("ok") or "not modified" in str(res.get("description", "")).lower():
            return True

    txt_payload: Dict[str, Any] = {
        "chat_id": peer_id,
        "message_id": message_id,
        "text": html_text,
        "parse_mode": "HTML",
        "reply_markup": markup,
        "disable_web_page_preview": True,
    }
    res = await bot_api_call("editMessageText", txt_payload)
    if not res.get("ok") and "parse" in str(res.get("description", "")).lower():
        txt_payload["text"] = text
        txt_payload.pop("parse_mode", None)
        res = await bot_api_call("editMessageText", txt_payload)
    return bool(res.get("ok") or "not modified" in str(res.get("description", "")).lower())


async def api_delete(chat_id: int, message_id: int) -> None:
    await bot_api_call(
        "deleteMessage", {"chat_id": utils.get_peer_id(chat_id), "message_id": message_id}
    )


async def api_answer_callback(
    callback_query_id: str, text: str = "", show_alert: bool = False
) -> None:
    await bot_api_call(
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        },
    )


def prompt_hint(is_private: bool) -> str:
    return "" if is_private else "\n\n💬 *In groups, reply directly to this message.*"


def is_main_owner(user_id: int) -> bool:
    owners = read_json(OWNERS_FILE)
    return int(user_id) == int(owners.get("main_owner", 0))


def is_authorized(user_id: int) -> bool:
    owners = read_json(OWNERS_FILE)
    if int(user_id) == int(owners.get("main_owner", 0)):
        return True
    return int(user_id) in [int(x) for x in owners.get("co_owners", [])]


def format_uptime(since: float = START_TIME) -> str:
    seconds = max(0, int(time.time() - since))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins:
        parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


async def cleanup_pending_login(key: Tuple[int, int]) -> None:
    pending = PENDING_LOGINS.pop(key, None)
    if pending and pending.get("client"):
        try:
            await pending["client"].disconnect()
        except Exception:
            pass


# ==============================================================================
# LIVE USERBOT WORKER HOSTING ENGINE (PER-SLOT + GLOBAL COMMANDS)
# ==============================================================================
async def _userbot_worker_loop(slot_id: str, slot_info: Dict[str, Any]) -> None:
    session_str = slot_info["session"]
    owner_name = slot_info.get("owner_name", "Owner")
    ub = build_userbot_client(session_str)
    ACTIVE_WORKERS[slot_id]["client"] = ub

    @ub.on(events.NewMessage)
    async def on_userbot_message(ev: events.NewMessage.Event) -> None:
        raw = (ev.raw_text or "").strip()
        if not raw:
            return

        sender = ev.sender_id
        if not ev.out and (not sender or not is_authorized(sender)):
            return

        cmd_lower = raw.split()[0].lower()

        if cmd_lower == ".ping":
            t0 = time.perf_counter()
            msg = await (ev.edit("⚡ *Pinging...*") if ev.out else ev.reply("⚡ *Pinging...*"))
            ms = round((time.perf_counter() - t0) * 1000)
            ACTIVE_WORKERS[slot_id]["cmds_handled"] += 1
            await msg.edit(
                f"🌸 **{BRAND} 𝗦𝗟𝗢𝗧 #{slot_id}** (`{owner_name}`)\n"
                f"⚡ **Pong:** `{ms} ms` | ⏱️ **Uptime:** `{format_uptime(ACTIVE_WORKERS[slot_id]['started_at'])}`"
            )
            return

        if cmd_lower == ".alive":
            ACTIVE_WORKERS[slot_id]["cmds_handled"] += 1
            cmds_db = read_json(COMMANDS_FILE).get("commands", {})
            slot_cmds = [
                v["trigger"]
                for v in cmds_db.values()
                if str(v.get("scope")) in ("ALL", str(slot_id))
            ]
            reply_txt = (
                f"🌸 **{BRAND} 𝗨𝗦𝗘𝗥𝗕𝗢𝗧 𝗛𝗢𝗦𝗧 𝗔𝗖𝗧𝗜𝗩𝗘** 🌸\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"🎰 **Slot:** `#{slot_id}` | 👤 **Owner:** `{owner_name}`\n"
                f"⏱️ **Worker Uptime:** `{format_uptime(ACTIVE_WORKERS[slot_id]['started_at'])}`\n"
                f"⚙️ **Slot Commands:** `{len(slot_cmds)}` ({', '.join(slot_cmds[:6]) or 'None'})"
            )
            await (ev.edit(reply_txt) if ev.out else ev.reply(reply_txt))
            return

        if cmd_lower == ".stats":
            ACTIVE_WORKERS[slot_id]["cmds_handled"] += 1
            cpu = psutil.cpu_percent(interval=None)
            ram = psutil.virtual_memory().percent
            handled = ACTIVE_WORKERS[slot_id]["cmds_handled"]
            txt = (
                f"📊 **𝗦𝗟𝗢𝗧 #{slot_id} 𝗟𝗜𝗩𝗘 𝗧𝗘𝗟𝗘𝗠𝗘𝗧𝗥𝗬**\n"
                f"👤 **Owner:** `{owner_name}`\n"
                f"🖥️ **Host CPU / RAM:** `{cpu}%` | `{ram}%`\n"
                f"⚡ **Commands Executed:** `{handled}`\n"
                f"⏱️ **Session Uptime:** `{format_uptime(ACTIVE_WORKERS[slot_id]['started_at'])}`"
            )
            await (ev.edit(txt) if ev.out else ev.reply(txt))
            return

        if cmd_lower == ".cmds":
            ACTIVE_WORKERS[slot_id]["cmds_handled"] += 1
            cmds_db = read_json(COMMANDS_FILE).get("commands", {})
            slot_specific = [
                f"`{v['trigger']}` (Slot #{slot_id})"
                for v in cmds_db.values()
                if str(v.get("scope")) == str(slot_id)
            ]
            global_cmds = [
                f"`{v['trigger']}` (All)"
                for v in cmds_db.values()
                if str(v.get("scope")) == "ALL"
            ]
            txt = (
                f"⚙️ **𝗔𝗖𝗧𝗜𝗩𝗘 𝗖𝗢𝗠𝗠𝗔𝗡𝗗𝗦 — 𝗦𝗟𝗢𝗧 #{slot_id}**\n"
                f"• **Built-in:** `.ping`, `.alive`, `.stats`, `.cmds`, `.id`, `.leave`\n"
                f"• **Slot-Exclusive:** {', '.join(slot_specific) or 'None'}\n"
                f"• **Global (All Slots):** {', '.join(global_cmds) or 'None'}"
            )
            await (ev.edit(txt) if ev.out else ev.reply(txt))
            return

        if cmd_lower == ".id":
            ACTIVE_WORKERS[slot_id]["cmds_handled"] += 1
            rep = await ev.get_reply_message()
            rep_id = rep.sender_id if rep else "None"
            txt = f"🆔 **Chat ID:** `{ev.chat_id}`\n👤 **Replied User ID:** `{rep_id}`"
            await (ev.edit(txt) if ev.out else ev.reply(txt))
            return

        if cmd_lower == ".leave" and not ev.is_private:
            ACTIVE_WORKERS[slot_id]["cmds_handled"] += 1
            await (
                ev.edit(f"👋 *Slot #{slot_id} leaving chat...*")
                if ev.out
                else ev.reply(f"👋 *Slot #{slot_id} leaving chat...*")
            )
            await ub(LeaveChannelRequest(ev.chat_id))
            return

        cmds_db = read_json(COMMANDS_FILE).get("commands", {})
        cmd_cfg = cmds_db.get(f"{slot_id}:{cmd_lower}") or cmds_db.get(f"ALL:{cmd_lower}")
        if cmd_cfg:
            ACTIVE_WORKERS[slot_id]["cmds_handled"] += 1
            chat_obj = await ev.get_chat()
            chat_title = getattr(chat_obj, "title", "Private Chat")
            formatted_reply = (
                str(cmd_cfg.get("reply", ""))
                .replace("{slot}", str(slot_id))
                .replace("{owner}", owner_name)
                .replace("{chat}", str(chat_title))
            )
            if ev.out:
                await ev.edit(formatted_reply)
            else:
                await ev.reply(formatted_reply)

    try:
        await ub.connect()
        if not await ub.is_user_authorized():
            ACTIVE_WORKERS[slot_id]["state"] = "unauthorized"
            return
        ACTIVE_WORKERS[slot_id]["state"] = "running"
        await ub.run_until_disconnected()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        if slot_id in ACTIVE_WORKERS:
            ACTIVE_WORKERS[slot_id]["state"] = f"error: {str(exc)[:30]}"
    finally:
        try:
            await ub.disconnect()
        except Exception:
            pass


async def start_slot_worker(slot_id: str) -> Tuple[bool, str]:
    slots = read_json(SLOTS_FILE).get("slots", {})
    info = slots.get(slot_id)
    if not info:
        return False, "Slot not found"
    if info.get("status") == "resting":
        return False, "Slot is in Rest Mode"
    if info.get("status") in ("dead", "error"):
        return False, "Slot session is dead/invalid"

    existing = ACTIVE_WORKERS.get(slot_id)
    if existing and existing.get("task") and not existing["task"].done():
        return True, "Already running"

    ACTIVE_WORKERS[slot_id] = {
        "client": None,
        "task": None,
        "started_at": time.time(),
        "cmds_handled": 0,
        "state": "starting",
    }
    task = asyncio.create_task(_userbot_worker_loop(slot_id, info))
    ACTIVE_WORKERS[slot_id]["task"] = task
    return True, "Worker started"


async def stop_slot_worker(slot_id: str) -> Tuple[bool, str]:
    worker = ACTIVE_WORKERS.pop(slot_id, None)
    if not worker:
        return False, "Worker not running"
    task = worker.get("task")
    if task and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return True, "Worker stopped"


def count_running_workers() -> int:
    return sum(
        1
        for w in ACTIVE_WORKERS.values()
        if w.get("task") and not w["task"].done()
    )


# ==============================================================================
# SAKURA UI & COLORED MENU BUILDERS (PM & GROUP FULL SUPPORT)
# ==============================================================================
def sakura_header(subtitle: str) -> str:
    return (
        f"🌸 ˚₊· ͟͟͞͞➳❥ **{BRAND} 𝗨𝗦𝗘𝗥𝗕𝗢𝗧 𝗛𝗔𝗡𝗗𝗟𝗘𝗥** 🌸\n"
        f"꒷꒦︶꒷꒦︶ ๋ ࣭ ⭑💮 {subtitle} 💮⭑ ࣭ ๋ ︶꒷꒦︶꒷꒦\n\n"
    )


def build_main_menu_caption() -> str:
    slots_data = read_json(SLOTS_FILE).get("slots", {})
    cfg = read_json(CONFIG_FILE)
    owners = read_json(OWNERS_FILE)
    cmds_db = read_json(COMMANDS_FILE).get("commands", {})

    total = len(slots_data)
    active = sum(1 for s in slots_data.values() if s.get("status") == "active")
    resting = sum(1 for s in slots_data.values() if s.get("status") == "resting")
    dead = sum(1 for s in slots_data.values() if s.get("status") in ("dead", "error"))
    running_hosts = count_running_workers()

    pings = [
        s.get("ping_ms", 0)
        for s in slots_data.values()
        if s.get("status") == "active"
        and isinstance(s.get("ping_ms"), (int, float))
        and s.get("ping_ms") > 0
    ]
    avg_ping = f"{round(sum(pings) / len(pings))} ms" if pings else "N/A"
    cpu = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory().percent
    frame_status = "🖼️ Active" if cfg.get("banner_photo") else "⬜ Not Set"

    text = sakura_header("𝗦𝗔𝗞𝗨𝗥𝗔 𝗖𝗢𝗡𝗧𝗥𝗢𝗟 𝗣𝗔𝗡𝗘𝗟")
    text += (
        f"╭─── 🎐 **𝗦𝗟𝗢𝗧 & 𝗛𝗢𝗦𝗧𝗜𝗡𝗚 𝗢𝗩𝗘𝗥𝗩𝗜𝗘𝗪**\n"
        f"│ 💮 **Total Slots:** `{total} / {cfg.get('max_slots', 25)}`\n"
        f"│ 🚀 **Hosted Live Workers:** `{running_hosts} running`\n"
        f"│ 🟢 **Healthy Ready:** `{active}` | 🌙 **Resting:** `{resting}` | 🔴 **Dead:** `{dead}`\n"
        f"│ ⚡ **Avg Session Ping:** `{avg_ping}`\n"
        f"╰──────────────────────\n\n"
        f"╭─── 🫧 **𝗦𝗬𝗦𝗧𝗘𝗠 & 𝗣𝗔𝗡𝗘𝗟 𝗧𝗘𝗟𝗘𝗠𝗘𝗧𝗥𝗬**\n"
        f"│ 🖥️ **CPU / RAM Load:** `{cpu}%` | `{ram}%`\n"
        f"│ ⚙️ **Custom Commands:** `{len(cmds_db)}` | 👑 **Co-Owners:** `{len(owners.get('co_owners', []))}`\n"
        f"│ 🖼️ **Upper Frame:** `{frame_status}` | ⏱️ **Uptime:** `{format_uptime()}`\n"
        f"╰──────────────────────\n"
        f"🌸 *Reply `sudo all` in any group to promote a user from all active slots.*"
    )
    return text


def build_main_menu_buttons(user_id: int) -> List[List[Dict[str, str]]]:
    rows = [
        [
            btn("🚀 Host All Bots", "menu_host_0", "primary"),
            btn("📲 Add via OTP / 2FA", "menu_otp_start", "success"),
        ],
        [
            btn("➕ Add String Session", "menu_add_slot", "success"),
            btn("🗂️ Manage Slots", "menu_slots_0", "primary"),
        ],
        [
            btn("🔗 Group Join & Deploy", "menu_deploy_group", "primary"),
            btn("🚪 Group Leave All", "menu_leave_group", "danger"),
        ],
        [
            btn("⚡ Sudo All / Admin", "menu_promote_pm", "primary"),
            btn("⚙️ Per-Slot Commands", "menu_cmds_0", "primary"),
        ],
        [
            btn("🩺 Full Health Check", "menu_health_all", "success"),
            btn("🌙 Rest / Wake Panel", "menu_rest_0", "danger"),
        ],
        [
            btn("🪪 Sync Slot Bios", "menu_profile_sync", "primary"),
            btn("📊 Reports & Logs", "menu_reports", "primary"),
        ],
        [
            btn("🖼️ Set Menu Photo Frame", "menu_set_frame", "primary"),
        ],
    ]
    if is_main_owner(user_id):
        rows.append([btn("👑 Manage Owners", "menu_owners", "primary")])
    rows.append([btn("🔄 Refresh Sakura Panel", "menu_home", "success")])
    return rows


def build_group_menu_caption(chat_title: str, chat_id: int) -> str:
    slots_data = read_json(SLOTS_FILE).get("slots", {})
    cfg = read_json(CONFIG_FILE)
    active = sum(1 for s in slots_data.values() if s.get("status") == "active")
    resting = sum(1 for s in slots_data.values() if s.get("status") == "resting")
    dead = sum(1 for s in slots_data.values() if s.get("status") in ("dead", "error"))

    text = sakura_header("𝗚𝗥𝗢𝗨𝗣 𝗦𝗔𝗞𝗨𝗥𝗔 𝗖𝗢𝗡𝗧𝗥𝗢𝗟 𝗣𝗔𝗡𝗘𝗟")
    text += (
        f"╭─── 🌸 **𝗚𝗥𝗢𝗨𝗣 𝗗𝗘𝗧𝗔𝗜𝗟𝗦**\n"
        f"│ 🏷️ **Group:** `{chat_title}`\n"
        f"│ 🆔 **Chat ID:** `{chat_id}`\n"
        f"╰──────────────────────\n\n"
        f"╭─── 🎐 **𝗨𝗦𝗘𝗥𝗕𝗢𝗧 𝗦𝗟𝗢𝗧𝗦 & 𝗛𝗢𝗦𝗧𝗦**\n"
        f"│ 💮 **Total Slots:** `{len(slots_data)} / {cfg.get('max_slots', 25)}` | 🚀 **Hosted:** `{count_running_workers()}`\n"
        f"│ 🟢 **Ready:** `{active}` | 🌙 **Rest:** `{resting}` | 🔴 **Dead:** `{dead}`\n"
        f"╰──────────────────────\n"
        f"✨ *All controls & slots work directly here. Reply `sudo all` to promote any user.*"
    )
    return text


def build_group_menu_buttons(user_id: int) -> List[List[Dict[str, str]]]:
    rows = [
        [
            btn("⚡ Sudo All Here", "grp_promote", "primary"),
            btn("🔍 Scan Slots Here", "grp_scan", "success"),
        ],
        [
            btn("🚀 Host All Bots", "menu_host_0", "primary"),
            btn("📲 Add via OTP / 2FA", "menu_otp_start", "success"),
        ],
        [
            btn("➕ Add String Session", "menu_add_slot", "success"),
            btn("🗂️ Manage Slots", "menu_slots_0", "primary"),
        ],
        [
            btn("🔗 Group Join & Deploy", "menu_deploy_group", "primary"),
            btn("🚪 Group Leave All", "menu_leave_group", "danger"),
        ],
        [
            btn("⚙️ Per-Slot Commands", "menu_cmds_0", "primary"),
            btn("🩺 Full Health Check", "menu_health_all", "success"),
        ],
        [
            btn("🌙 Rest / Wake Panel", "menu_rest_0", "danger"),
            btn("🪪 Sync Slot Bios", "menu_profile_sync", "primary"),
        ],
        [
            btn("📊 Reports & Logs", "menu_reports", "primary"),
            btn("🖼️ Set Photo Frame", "menu_set_frame", "primary"),
        ],
    ]
    if is_main_owner(user_id):
        rows.append([btn("👑 Manage Owners", "menu_owners", "primary")])
    rows.append([btn("🔄 Refresh Group Panel", "grp_refresh", "success")])
    return rows


async def send_or_edit_sakura(
    event: Any,
    text: str,
    buttons: List[List[Dict[str, str]]],
    force_new: bool = False,
) -> None:
    cfg = read_json(CONFIG_FILE)
    banner = cfg.get("banner_photo", "").strip()
    chat_id = event.chat_id

    if force_new:
        await api_send(chat_id, text, buttons=buttons, photo=banner or None)
        return

    msg = await event.get_message()
    msg_id = event.message_id
    has_media = bool(msg and msg.media)

    if banner and has_media:
        ok = await api_edit(chat_id, msg_id, text, buttons=buttons, is_caption=True)
        if not ok:
            await api_delete(chat_id, msg_id)
            await api_send(chat_id, text, buttons=buttons, photo=banner)
    elif banner and not has_media:
        await api_delete(chat_id, msg_id)
        await api_send(chat_id, text, buttons=buttons, photo=banner)
    elif not banner and has_media:
        await api_delete(chat_id, msg_id)
        await api_send(chat_id, text, buttons=buttons, photo=None)
    else:
        ok = await api_edit(chat_id, msg_id, text, buttons=buttons, is_caption=False)
        if not ok:
            await api_send(chat_id, text, buttons=buttons, photo=None)


# ==============================================================================
# TELETHON USERBOT INSPECTION, JOIN, LEAVE & PROMOTION ENGINE
# ==============================================================================
async def inspect_userbot_session(session_str: str) -> Dict[str, Any]:
    ub = build_userbot_client(session_str, timeout=12)
    t0 = time.perf_counter()
    try:
        await ub.connect()
        if not await ub.is_user_authorized():
            return {
                "ok": False,
                "status": "dead",
                "reason": "Session unauthorized / expired",
                "ping_ms": 0,
            }
        me = await ub.get_me()
        ping_ms = round((time.perf_counter() - t0) * 1000)

        if not me or getattr(me, "deleted", False):
            return {
                "ok": False,
                "status": "dead",
                "reason": "Account deleted or unavailable",
                "ping_ms": ping_ms,
            }

        dialogs = await ub.get_dialogs(limit=100)
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "Unnamed"
        username = f"@{me.username}" if me.username else "No Username"
        restricted = getattr(me, "restricted", False)

        return {
            "ok": True,
            "status": "active",
            "reason": "Restricted flag detected" if restricted else "Healthy & Online",
            "ping_ms": ping_ms,
            "user_id": me.id,
            "full_name": full_name,
            "username": username,
            "phone": getattr(me, "phone", "Hidden") or "Hidden",
            "dialogs_count": len(dialogs),
            "restricted": restricted,
        }
    except (AuthKeyUnregisteredError, SessionRevokedError):
        return {
            "ok": False,
            "status": "dead",
            "reason": "AuthKey unregistered / Session revoked",
            "ping_ms": 0,
        }
    except UserDeactivatedBanError:
        return {
            "ok": False,
            "status": "dead",
            "reason": "Account banned / deactivated",
            "ping_ms": 0,
        }
    except FloodWaitError as fw:
        return {
            "ok": False,
            "status": "error",
            "reason": f"FloodWait ({fw.seconds}s)",
            "ping_ms": 0,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "reason": str(exc)[:80],
            "ping_ms": 0,
        }
    finally:
        try:
            await ub.disconnect()
        except Exception:
            pass


async def join_group_via_userbot(
    session_str: str, group_link: str
) -> Tuple[bool, str, Optional[int]]:
    ub = build_userbot_client(session_str, timeout=15)
    try:
        await ub.connect()
        if not await ub.is_user_authorized():
            return False, "Dead session", None

        raw = group_link.strip()
        if "t.me/+" in raw:
            invite_hash = raw.split("t.me/+", 1)[1].split("?")[0].strip("/")
            res = await ub(ImportChatInviteRequest(invite_hash))
            chat_id = res.chats[0].id if getattr(res, "chats", None) else None
            return True, "Joined via invite link", chat_id
        elif "joinchat/" in raw:
            invite_hash = raw.split("joinchat/", 1)[1].split("?")[0].strip("/")
            res = await ub(ImportChatInviteRequest(invite_hash))
            chat_id = res.chats[0].id if getattr(res, "chats", None) else None
            return True, "Joined via joinchat link", chat_id
        else:
            clean = (
                raw.replace("https://t.me/", "")
                .replace("http://t.me/", "")
                .strip("/")
            )
            target_arg = int(clean) if clean.lstrip("-").isdigit() else clean
            ent = await ub.get_entity(target_arg)
            await ub(JoinChannelRequest(ent))
            return True, f"Joined {getattr(ent, 'title', clean)}", ent.id
    except UserAlreadyParticipantError:
        return True, "Already a member", None
    except FloodWaitError as fw:
        return False, f"FloodWait ({fw.seconds}s)", None
    except Exception as exc:
        return False, str(exc)[:45], None
    finally:
        try:
            await ub.disconnect()
        except Exception:
            pass


async def leave_group_via_userbot(session_str: str, group_identifier: str) -> Tuple[bool, str]:
    ub = build_userbot_client(session_str, timeout=15)
    try:
        await ub.connect()
        if not await ub.is_user_authorized():
            return False, "Dead session"

        clean = (
            group_identifier.strip()
            .replace("https://t.me/", "")
            .replace("http://t.me/", "")
            .strip("/")
        )
        target_arg = int(clean) if clean.lstrip("-").isdigit() else clean
        ent = await ub.get_entity(target_arg)
        await ub(LeaveChannelRequest(ent))
        return True, f"Left {getattr(ent, 'title', clean)}"
    except UserNotParticipantError:
        return True, "Not in group"
    except Exception as exc:
        return False, str(exc)[:45]
    finally:
        try:
            await ub.disconnect()
        except Exception:
            pass


async def promote_via_userbot(
    session_str: str, target_identifier: str, chat_identifier: Optional[str] = None
) -> Tuple[bool, str]:
    cfg = read_json(CONFIG_FILE)
    rank_title = str(cfg.get("auto_promote_title", "Sid Admin"))[:16]

    ub = build_userbot_client(session_str, timeout=15)
    try:
        await ub.connect()
        if not await ub.is_user_authorized():
            return False, "Dead session"

        target_arg = (
            int(target_identifier)
            if str(target_identifier).lstrip("-").isdigit()
            else target_identifier
        )
        try:
            target_entity = await ub.get_input_entity(target_arg)
        except Exception as e:
            return False, f"Target unresolved: {str(e)[:35]}"

        target_chats = []
        if chat_identifier:
            raw_c = str(chat_identifier).strip()
            if "t.me/" in raw_c and "+" not in raw_c and "joinchat/" not in raw_c:
                raw_c = raw_c.split("t.me/", 1)[1].strip("/")
            chat_arg = int(raw_c) if raw_c.lstrip("-").isdigit() else raw_c
            try:
                c_ent = await ub.get_entity(chat_arg)
                target_chats.append(c_ent)
            except Exception as e:
                return False, f"Chat inaccessible: {str(e)[:35]}"
        else:
            async for dialog in ub.iter_dialogs(limit=150):
                if dialog.is_group or dialog.is_channel:
                    target_chats.append(dialog.entity)

        if not target_chats:
            return False, "No eligible groups found"

        promoted_count = 0
        last_err = "No admin rights in chat(s)"

        for chat_ent in target_chats:
            try:
                me_part = await ub(GetParticipantRequest(chat_ent, "me"))
                part = me_part.participant

                if isinstance(part, ChannelParticipantCreator):
                    rights = ChatAdminRights(
                        change_info=True,
                        post_messages=True,
                        edit_messages=True,
                        delete_messages=True,
                        ban_users=True,
                        invite_users=True,
                        pin_messages=True,
                        add_admins=True,
                        manage_call=True,
                        manage_topics=True,
                    )
                elif isinstance(part, ChannelParticipantAdmin):
                    r = part.admin_rights
                    if not getattr(r, "add_admins", False):
                        last_err = "Missing 'Add Admins' right"
                        continue
                    rights = ChatAdminRights(
                        change_info=getattr(r, "change_info", False),
                        post_messages=getattr(r, "post_messages", False),
                        edit_messages=getattr(r, "edit_messages", False),
                        delete_messages=getattr(r, "delete_messages", False),
                        ban_users=getattr(r, "ban_users", False),
                        invite_users=getattr(r, "invite_users", False),
                        pin_messages=getattr(r, "pin_messages", False),
                        add_admins=False,
                        manage_call=getattr(r, "manage_call", False),
                        manage_topics=getattr(r, "manage_topics", False),
                    )
                else:
                    last_err = "Not an admin in chat"
                    continue

                await ub(
                    EditAdminRequest(
                        channel=chat_ent,
                        user_id=target_entity,
                        admin_rights=rights,
                        rank=rank_title,
                    )
                )
                promoted_count += 1
            except (ChatAdminRequiredError, RightForbiddenError):
                last_err = "RightForbidden / Admin required"
            except UserNotParticipantError:
                last_err = "Userbot not in chat"
            except FloodWaitError as fw:
                last_err = f"FloodWait {fw.seconds}s"
                break
            except Exception as e:
                last_err = str(e)[:35]

        if promoted_count > 0:
            return True, f"Promoted in {promoted_count} chat(s)"
        return False, last_err
    except Exception as exc:
        return False, str(exc)[:45]
    finally:
        try:
            await ub.disconnect()
        except Exception:
            pass


async def check_userbot_in_chat(session_str: str, chat_id: int) -> Tuple[bool, str]:
    ub = build_userbot_client(session_str, timeout=10)
    try:
        await ub.connect()
        if not await ub.is_user_authorized():
            return False, "Dead Session"
        chat_ent = await ub.get_entity(chat_id)
        me_part = await ub(GetParticipantRequest(chat_ent, "me"))
        part = me_part.participant
        if isinstance(part, ChannelParticipantCreator):
            return True, "Creator (Full Rights)"
        if isinstance(part, ChannelParticipantAdmin):
            can_add = getattr(part.admin_rights, "add_admins", False)
            return True, "Admin (Can Promote)" if can_add else "Admin (No Promote Right)"
        return True, "Member (Not Admin)"
    except UserNotParticipantError:
        return False, "Not in Group"
    except Exception as e:
        return False, f"Err: {str(e)[:25]}"
    finally:
        try:
            await ub.disconnect()
        except Exception:
            pass


# ==============================================================================
# COMMAND HANDLERS (/start, /menu, /sid, /promote, sudo all)
# ==============================================================================
@bot.on(events.NewMessage(pattern=r"^/(start|menu|sid)(?:@\w+)?$"))
async def cmd_menu_handler(event: events.NewMessage.Event) -> None:
    sender_id = event.sender_id
    if not sender_id or not is_authorized(sender_id):
        await api_send(
            event.chat_id,
            f"🌸 **{BRAND} 𝗨𝗦𝗘𝗥𝗕𝗢𝗧 𝗛𝗔𝗡𝗗𝗟𝗘𝗥**\n"
            f"🔒 *Access Restricted. Only authorized owners can operate this panel.*",
            reply_to=event.id,
        )
        return

    key = (event.chat_id, sender_id)
    USER_STATES.pop(key, None)
    await cleanup_pending_login(key)

    if event.is_private:
        await send_or_edit_sakura(
            event,
            build_main_menu_caption(),
            build_main_menu_buttons(sender_id),
            force_new=True,
        )
    else:
        chat = await event.get_chat()
        title = getattr(chat, "title", "Group Chat")
        await send_or_edit_sakura(
            event,
            build_group_menu_caption(title, event.chat_id),
            build_group_menu_buttons(sender_id),
            force_new=True,
        )


@bot.on(events.NewMessage(func=lambda e: bool(e.raw_text)))
async def cmd_sudo_all_handler(event: events.NewMessage.Event) -> None:
    raw = (event.raw_text or "").strip()
    lowered = raw.lower()
    if not (
        lowered.startswith("/promote")
        or lowered.startswith("/sudoall")
        or lowered == "sudo all"
        or lowered.startswith("sudo all ")
    ):
        return

    sender_id = event.sender_id
    if not sender_id or not is_authorized(sender_id):
        return

    parts = raw.split(maxsplit=2 if lowered.startswith("sudo all") else 1)
    target = ""
    if lowered.startswith("sudo all") and len(parts) >= 3:
        target = parts[2].strip()
    elif not lowered.startswith("sudo all") and len(parts) >= 2:
        target = parts[1].strip()

    if not target and event.is_reply:
        replied = await event.get_reply_message()
        if replied and replied.sender_id:
            target = str(replied.sender_id)

    if not target and not event.is_private:
        target = str(sender_id)

    if not target:
        await api_send(
            event.chat_id,
            "🌸 **Usage:** Reply `sudo all` to a user in a group, or send `sudo all @username`.",
            reply_to=event.id,
        )
        return

    chat_filter = None if event.is_private else str(event.chat_id)
    await execute_bulk_promotion(event, sender_id, target, chat_filter)


# ==============================================================================
# MULTI-STEP INTERACTIVE FLOWS (WORKS IN BOTH PM & GROUPS)
# ==============================================================================
@bot.on(events.NewMessage)
async def state_input_handler(event: events.NewMessage.Event) -> None:
    if not event.sender_id:
        return

    raw = (event.raw_text or "").strip()
    if raw.startswith("/") or raw.lower() == "sudo all" or raw.lower().startswith("sudo all "):
        return

    key = (event.chat_id, event.sender_id)
    if key not in USER_STATES:
        return

    if not is_authorized(event.sender_id):
        USER_STATES.pop(key, None)
        await cleanup_pending_login(key)
        return

    state = USER_STATES[key]
    step = state["step"]
    text = raw
    is_priv = event.is_private
    hint = prompt_hint(is_priv)
    home_cb = "menu_home" if is_priv else "grp_refresh"

    # ------------------------------------------------------------------
    # FLOW A: HARDENED OTP + 2FA PHONE LOGIN WIZARD
    # ------------------------------------------------------------------
    if step == "otp_slot_num":
        if not text.isdigit() or int(text) <= 0:
            await api_send(
                event.chat_id,
                "❌ Send a valid positive **Slot Number** (e.g. `1`, `2`).",
                reply_to=event.id,
            )
            return
        max_slots = int(read_json(CONFIG_FILE).get("max_slots", 25))
        if int(text) > max_slots:
            await api_send(
                event.chat_id,
                f"❌ Slot number cannot exceed `{max_slots}`.",
                reply_to=event.id,
            )
            return
        state["data"]["slot_num"] = str(int(text))
        state["step"] = "otp_owner_name"
        await api_send(
            event.chat_id,
            f"📲 **OTP Login — Slot #{text}**\n\n"
            f"👤 Enter the **Owner Name** for this userbot slot:{hint}",
            buttons=[[btn("❌ Cancel", "otp_cancel", "danger")]],
            reply_to=event.id,
        )

    elif step == "otp_owner_name":
        if len(text) < 2 or len(text) > 40:
            await api_send(
                event.chat_id,
                "❌ Owner name must be 2–40 characters.",
                reply_to=event.id,
            )
            return
        state["data"]["owner_name"] = text
        state["step"] = "otp_phone"
        await api_send(
            event.chat_id,
            f"👤 **Owner:** `{text}`\n\n"
            f"📞 Send the **Phone Number** with country code (e.g., `+919876543210`):{hint}",
            buttons=[[btn("❌ Cancel", "otp_cancel", "danger")]],
            reply_to=event.id,
        )

    elif step == "otp_phone":
        cleaned_digits = re.sub(r"[^\d+]", "", text)
        if not cleaned_digits.startswith("+") or len(cleaned_digits) < 8:
            await api_send(
                event.chat_id,
                "❌ Include country code starting with `+` (e.g., `+919876543210`).",
                reply_to=event.id,
            )
            return

        wait_id = await api_send(
            event.chat_id,
            f"📡 **Connecting & Requesting OTP for `{cleaned_digits}`...**",
            reply_to=event.id,
        )
        await cleanup_pending_login(key)
        client = build_userbot_client(timeout=20)
        try:
            await client.connect()
            try:
                sent = await client.send_code_request(cleaned_digits)
            except AuthRestartError:
                sent = await client.send_code_request(cleaned_digits)

            PENDING_LOGINS[key] = {
                "client": client,
                "phone": cleaned_digits,
                "phone_code_hash": sent.phone_code_hash,
            }
            state["step"] = "otp_code"
            msg_txt = (
                f"✅ **OTP Dispatched to `{cleaned_digits}`!**\n\n"
                f"🔢 Send the **5-digit Telegram code**.\n"
                f"⚠️ **CRITICAL:** Put spaces between digits (e.g. `1 2 3 4 5`) so Telegram's security filter does not expire the code:{hint}"
            )
            otp_btns = [
                [btn("🔄 Resend OTP", "otp_resend", "primary")],
                [btn("❌ Cancel", "otp_cancel", "danger")],
            ]
            if wait_id:
                await api_edit(event.chat_id, wait_id, msg_txt, buttons=otp_btns)
            else:
                await api_send(event.chat_id, msg_txt, buttons=otp_btns, reply_to=event.id)
        except PhoneNumberInvalidError:
            await client.disconnect()
            if wait_id:
                await api_edit(
                    event.chat_id,
                    wait_id,
                    "❌ Invalid phone number format. Re-enter with country code:",
                )
        except PhoneNumberBannedError:
            await client.disconnect()
            USER_STATES.pop(key, None)
            if wait_id:
                await api_edit(event.chat_id, wait_id, "🚫 This phone number is banned by Telegram.")
        except FloodWaitError as fw:
            await client.disconnect()
            USER_STATES.pop(key, None)
            if wait_id:
                await api_edit(
                    event.chat_id,
                    wait_id,
                    f"⏳ FloodWait triggered by Telegram. Retry after `{fw.seconds}s`.",
                )
        except Exception as exc:
            await client.disconnect()
            USER_STATES.pop(key, None)
            if wait_id:
                await api_edit(
                    event.chat_id, wait_id, f"❌ OTP Request Failed: `{str(exc)[:85]}`"
                )

    elif step == "otp_code":
        pending = PENDING_LOGINS.get(key)
        if not pending:
            USER_STATES.pop(key, None)
            await api_send(
                event.chat_id,
                "⚠️ Login session timed out. Start OTP login again.",
                reply_to=event.id,
            )
            return

        code = re.sub(r"\D", "", text)
        if len(code) < 5:
            await api_send(
                event.chat_id,
                "❌ Enter all 5 digits separated by spaces (e.g. `1 2 3 4 5`):",
                reply_to=event.id,
            )
            return

        client: TelegramClient = pending["client"]
        try:
            if not client.is_connected():
                await client.connect()
            await client.sign_in(
                phone=pending["phone"],
                code=code,
                phone_code_hash=pending["phone_code_hash"],
            )
            await finalize_otp_login(event, key, state, client)
        except SessionPasswordNeededError:
            state["step"] = "otp_2fa"
            hint_str = ""
            try:
                pwd_info = await client(GetPasswordRequest())
                if getattr(pwd_info, "hint", None):
                    hint_str = f"\n💡 **Password Hint:** `{pwd_info.hint}`"
            except Exception:
                pass
            await api_send(
                event.chat_id,
                f"🔐 **Two-Step Verification (2FA) Required!**{hint_str}\n\n"
                f"🔑 Send the **2FA Cloud Password** for `{pending['phone']}`:{hint}",
                buttons=[[btn("❌ Cancel", "otp_cancel", "danger")]],
                reply_to=event.id,
            )
        except PhoneCodeInvalidError:
            await api_send(
                event.chat_id,
                "❌ **Incorrect OTP Code.** Re-enter with spaces (e.g. `1 2 3 4 5`):",
                buttons=[
                    [btn("🔄 Resend OTP", "otp_resend", "primary")],
                    [btn("❌ Cancel", "otp_cancel", "danger")],
                ],
                reply_to=event.id,
            )
        except PhoneCodeExpiredError:
            USER_STATES.pop(key, None)
            await cleanup_pending_login(key)
            await api_send(
                event.chat_id,
                "⌛ **OTP Code Expired** (Telegram invalidates codes pasted without spaces). "
                "Tap **📲 Add via OTP / 2FA** to start fresh.",
                reply_to=event.id,
            )
        except Exception as exc:
            USER_STATES.pop(key, None)
            await cleanup_pending_login(key)
            await api_send(
                event.chat_id,
                f"❌ Sign-in failed: `{str(exc)[:85]}`",
                reply_to=event.id,
            )

    elif step == "otp_2fa":
        pending = PENDING_LOGINS.get(key)
        if not pending:
            USER_STATES.pop(key, None)
            await api_send(
                event.chat_id,
                "⚠️ Login session expired. Restart OTP login.",
                reply_to=event.id,
            )
            return
        client = pending["client"]
        try:
            if not client.is_connected():
                await client.connect()
            await client.sign_in(password=text)
            await finalize_otp_login(event, key, state, client)
        except PasswordHashInvalidError:
            await api_send(
                event.chat_id,
                "❌ **Wrong 2FA Password.** Try entering the password again:",
                reply_to=event.id,
            )
        except Exception as exc:
            USER_STATES.pop(key, None)
            await cleanup_pending_login(key)
            await api_send(
                event.chat_id,
                f"❌ 2FA verification failed: `{str(exc)[:85]}`",
                reply_to=event.id,
            )

    # ------------------------------------------------------------------
    # FLOW B: ADD SLOT VIA STRING SESSION
    # ------------------------------------------------------------------
    elif step == "add_slot_num":
        if not text.isdigit() or int(text) <= 0:
            await api_send(
                event.chat_id,
                "❌ Send a valid positive **Slot Number** (e.g. `1`, `2`).",
                reply_to=event.id,
            )
            return
        max_slots = int(read_json(CONFIG_FILE).get("max_slots", 25))
        if int(text) > max_slots:
            await api_send(
                event.chat_id,
                f"❌ Slot number cannot exceed `{max_slots}`.",
                reply_to=event.id,
            )
            return
        state["data"]["slot_num"] = str(int(text))
        state["step"] = "add_slot_owner"
        await api_send(
            event.chat_id,
            f"🌸 **Slot #{text} Selected**\n\n👤 Enter the **Owner Name** for this slot:{hint}",
            buttons=[[btn("❌ Cancel", home_cb, "danger")]],
            reply_to=event.id,
        )

    elif step == "add_slot_owner":
        if len(text) < 2 or len(text) > 40:
            await api_send(
                event.chat_id,
                "❌ Owner name must be 2–40 characters.",
                reply_to=event.id,
            )
            return
        state["data"]["owner_name"] = text
        state["step"] = "add_slot_session"
        await api_send(
            event.chat_id,
            f"👤 **Owner:** `{text}`\n\n🔑 Paste the **Telethon StringSession**:{hint}",
            buttons=[[btn("❌ Cancel", home_cb, "danger")]],
            reply_to=event.id,
        )

    elif step == "add_slot_session":
        USER_STATES.pop(key, None)
        slot_num = state["data"]["slot_num"]
        owner_name = state["data"]["owner_name"]
        session_str = text

        wait_id = await api_send(
            event.chat_id,
            f"🌸 **Verifying & Hosting Slot #{slot_num}...**",
            reply_to=event.id,
        )
        health = await inspect_userbot_session(session_str)

        slots_db = read_json(SLOTS_FILE)
        slots_db["slots"][slot_num] = {
            "slot_id": int(slot_num),
            "owner_name": owner_name,
            "session": session_str,
            "status": health["status"],
            "added_by": event.sender_id,
            "added_at": now_str(),
            "last_checked": now_str(),
            "ping_ms": health.get("ping_ms", 0),
            "user_id": health.get("user_id", 0),
            "full_name": health.get("full_name", "Unknown"),
            "username": health.get("username", "N/A"),
            "phone": health.get("phone", "Hidden"),
            "dialogs_count": health.get("dialogs_count", 0),
            "health_note": health.get("reason", "Checked"),
        }
        write_json(SLOTS_FILE, slots_db)

        if health["ok"]:
            await start_slot_worker(slot_num)

        log_audit(
            "ADD_SLOT",
            event.sender_id,
            f"Slot #{slot_num} | Owner: {owner_name} | Account: {health.get('full_name')} ({health['status']})",
        )

        status_icon = "🟢" if health["ok"] else "🔴"
        res_txt = (
            f"🌸 **𝗦𝗟𝗢𝗧 #{slot_num} 𝗥𝗘𝗚𝗜𝗦𝗧𝗘𝗥𝗘𝗗 & 𝗛𝗢𝗦𝗧𝗘𝗗** 🌸\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"👤 **Slot Owner:** `{owner_name}`\n"
            f"🤖 **Account:** `{health.get('full_name', 'Invalid')}` ({health.get('username', 'N/A')})\n"
            f"{status_icon} **Status:** `{health['status'].upper()}` ({health['reason']})\n"
            f"⚡ **Ping:** `{health.get('ping_ms', 0)} ms` | 💬 **Dialogs:** `{health.get('dialogs_count', 0)}`"
        )
        res_btns = [
            [
                btn("🚀 Host Panel", "menu_host_0", "primary"),
                btn("🌸 Sakura Menu", home_cb, "success"),
            ]
        ]
        if wait_id:
            await api_edit(event.chat_id, wait_id, res_txt, buttons=res_btns)
        else:
            await api_send(event.chat_id, res_txt, buttons=res_btns, reply_to=event.id)

    # ------------------------------------------------------------------
    # FLOW C: PER-SLOT CUSTOM COMMAND STUDIO
    # ------------------------------------------------------------------
    elif step == "cmd_add_trigger":
        trigger = text.split()[0].lower()
        if not trigger.startswith("."):
            trigger = "." + trigger
        state["data"]["trigger"] = trigger
        scope = state["data"].get("scope", "ALL")
        scope_label = "ALL Slots" if scope == "ALL" else f"Slot #{scope}"
        state["step"] = "cmd_add_reply"
        await api_send(
            event.chat_id,
            f"⚙️ **Trigger:** `{trigger}` | 🎯 **Target:** `{scope_label}`\n\n"
            f"💬 Send the **Response Text** for this command.\n"
            f"*(Placeholders: `{{slot}}` = Slot ID, `{{owner}}` = Slot Owner, `{{chat}}` = Chat Name)*:{hint}",
            buttons=[[btn("❌ Cancel", "menu_cmds_0", "danger")]],
            reply_to=event.id,
        )

    elif step == "cmd_add_reply":
        USER_STATES.pop(key, None)
        trigger = state["data"]["trigger"]
        scope = str(state["data"].get("scope", "ALL"))
        cmds_db = read_json(COMMANDS_FILE)
        cmds_db.setdefault("commands", {})[f"{scope}:{trigger}"] = {
            "trigger": trigger,
            "scope": scope,
            "reply": text,
            "created_by": event.sender_id,
        }
        write_json(COMMANDS_FILE, cmds_db)
        scope_label = "ALL Slots" if scope == "ALL" else f"Slot #{scope}"
        log_audit("ADD_COMMAND", event.sender_id, f"Added `{trigger}` for {scope_label}")
        await api_send(
            event.chat_id,
            f"✅ **Command `{trigger}` Live for `{scope_label}`!**",
            buttons=[[btn("⚙️ Per-Slot Commands", "menu_cmds_0", "primary")]],
            reply_to=event.id,
        )

    # ------------------------------------------------------------------
    # FLOW D: GROUP JOIN, GROUP LEAVE & PROFILE BIO SYNC
    # ------------------------------------------------------------------
    elif step == "deploy_group_link":
        state["data"]["group_link"] = text
        state["step"] = "deploy_target_user"
        await api_send(
            event.chat_id,
            f"🔗 **Group Link:** `{text}`\n\n"
            f"🎯 Send the **Target Username / User ID** to auto-promote after joining "
            f"(or send `SKIP` to only join & start userbot workers):{hint}",
            buttons=[[btn("❌ Cancel", home_cb, "danger")]],
            reply_to=event.id,
        )

    elif step == "deploy_target_user":
        USER_STATES.pop(key, None)
        group_link = state["data"]["group_link"]
        target_user = None if text.upper() == "SKIP" else text
        await execute_group_join_and_deploy(event, event.sender_id, group_link, target_user)

    elif step == "leave_group_link":
        USER_STATES.pop(key, None)
        await execute_group_leave_all(event, event.sender_id, text)

    elif step == "profile_sync_bio":
        USER_STATES.pop(key, None)
        await execute_bio_sync_all(event, event.sender_id, text)

    # ------------------------------------------------------------------
    # FLOW E: PHOTO FRAME, OWNERS & PROMOTE
    # ------------------------------------------------------------------
    elif step == "set_banner":
        cfg = read_json(CONFIG_FILE)
        if event.message.photo or (
            event.message.document
            and getattr(event.message.document, "mime_type", "").startswith("image/")
        ):
            saved_path = await event.message.download_media(file="sakura_banner.jpg")
            cfg["banner_photo"] = str(saved_path)
            write_json(CONFIG_FILE, cfg)
            USER_STATES.pop(key, None)
            log_audit("SET_BANNER", event.sender_id, "Updated Sakura upper photo frame")
            if is_priv:
                await send_or_edit_sakura(
                    event,
                    build_main_menu_caption(),
                    build_main_menu_buttons(event.sender_id),
                    force_new=True,
                )
            else:
                chat = await event.get_chat()
                title = getattr(chat, "title", "Group Chat")
                await send_or_edit_sakura(
                    event,
                    build_group_menu_caption(title, event.chat_id),
                    build_group_menu_buttons(event.sender_id),
                    force_new=True,
                )
        elif text.startswith("http://") or text.startswith("https://"):
            cfg["banner_photo"] = text
            write_json(CONFIG_FILE, cfg)
            USER_STATES.pop(key, None)
            log_audit("SET_BANNER", event.sender_id, "Updated Sakura upper photo URL")
            if is_priv:
                await send_or_edit_sakura(
                    event,
                    build_main_menu_caption(),
                    build_main_menu_buttons(event.sender_id),
                    force_new=True,
                )
            else:
                chat = await event.get_chat()
                title = getattr(chat, "title", "Group Chat")
                await send_or_edit_sakura(
                    event,
                    build_group_menu_caption(title, event.chat_id),
                    build_group_menu_buttons(event.sender_id),
                    force_new=True,
                )
        else:
            await api_send(
                event.chat_id,
                "❌ Send an actual **Photo** or a valid `https://` image link.",
                reply_to=event.id,
            )

    elif step == "add_owner":
        if not is_main_owner(event.sender_id):
            USER_STATES.pop(key, None)
            return
        if not text.isdigit():
            await api_send(
                event.chat_id,
                "❌ Send a valid numeric **Telegram User ID**.",
                reply_to=event.id,
            )
            return
        new_id = int(text)
        owners = read_json(OWNERS_FILE)
        co_list = [int(x) for x in owners.get("co_owners", [])]
        if new_id == int(owners["main_owner"]) or new_id in co_list:
            await api_send(
                event.chat_id,
                "⚠️ This user is already an Owner.",
                reply_to=event.id,
            )
            return
        co_list.append(new_id)
        owners["co_owners"] = co_list
        write_json(OWNERS_FILE, owners)
        USER_STATES.pop(key, None)
        log_audit("ADD_OWNER", event.sender_id, f"Added Co-Owner ID: {new_id}")
        await api_send(
            event.chat_id,
            f"👑 **Co-Owner Added:** `{new_id}`",
            buttons=[[btn("👑 Owners Panel", "menu_owners", "primary")]],
            reply_to=event.id,
        )

    elif step == "promote_target_pm":
        state["data"]["target"] = text
        state["step"] = "promote_chat_pm"
        await api_send(
            event.chat_id,
            f"🎯 **Target Person:** `{text}`\n\n"
            f"📍 Send the **Group Username / Chat ID** (or `ALL` for all shared groups):{hint}",
            buttons=[[btn("❌ Cancel", home_cb, "danger")]],
            reply_to=event.id,
        )

    elif step == "promote_chat_pm":
        USER_STATES.pop(key, None)
        target = state["data"]["target"]
        chat_filter = None if text.upper() == "ALL" else text
        await execute_bulk_promotion(event, event.sender_id, target, chat_filter)

    elif step == "promote_target_group":
        USER_STATES.pop(key, None)
        await execute_bulk_promotion(event, event.sender_id, text, str(event.chat_id))


async def finalize_otp_login(
    event: events.NewMessage.Event,
    key: Tuple[int, int],
    state: Dict[str, Any],
    client: TelegramClient,
) -> None:
    USER_STATES.pop(key, None)
    PENDING_LOGINS.pop(key, None)

    slot_num = state["data"]["slot_num"]
    owner_name = state["data"]["owner_name"]
    home_cb = "menu_home" if event.is_private else "grp_refresh"

    t0 = time.perf_counter()
    me = await client.get_me()
    ping_ms = round((time.perf_counter() - t0) * 1000)
    dialogs = await client.get_dialogs(limit=100)
    session_str = client.session.save()
    await client.disconnect()

    full_name = f"{me.first_name or ''} {me.last_name or ''}".strip() or "Unnamed"
    username = f"@{me.username}" if me.username else "No Username"
    phone = getattr(me, "phone", "Hidden") or "Hidden"

    slots_db = read_json(SLOTS_FILE)
    slots_db["slots"][slot_num] = {
        "slot_id": int(slot_num),
        "owner_name": owner_name,
        "session": session_str,
        "status": "active",
        "added_by": event.sender_id,
        "added_at": now_str(),
        "last_checked": now_str(),
        "ping_ms": ping_ms,
        "user_id": me.id,
        "full_name": full_name,
        "username": username,
        "phone": phone,
        "dialogs_count": len(dialogs),
        "health_note": "Verified via OTP/2FA Login",
    }
    write_json(SLOTS_FILE, slots_db)

    await start_slot_worker(slot_num)
    log_audit(
        "OTP_LOGIN_SLOT",
        event.sender_id,
        f"Slot #{slot_num} | Owner: {owner_name} | Account: {full_name} ({me.id})",
    )

    await api_send(
        event.chat_id,
        f"🌸 **𝗢𝗧𝗣 / 𝟮𝗙𝗔 𝗟𝗢𝗚𝗜𝗡 & 𝗛𝗢𝗦𝗧 𝗖𝗢𝗠𝗣𝗟𝗘𝗧𝗘** 🌸\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎰 **Assigned Slot:** `#{slot_num}`\n"
        f"👤 **Slot Owner:** `{owner_name}`\n"
        f"🤖 **Account:** `{full_name}` ({username})\n"
        f"🆔 **User ID:** `{me.id}`\n"
        f"🟢 **Host Worker:** `RUNNING` (`{ping_ms} ms` | `{len(dialogs)} chats`)",
        buttons=[
            [
                btn("🚀 Open Host Panel", "menu_host_0", "primary"),
                btn("🌸 Sakura Menu", home_cb, "success"),
            ]
        ],
        reply_to=event.id,
    )


# ==============================================================================
# BULK PROMOTION, GROUP DEPLOY, GROUP LEAVE & BIO SYNC RUNNERS
# ==============================================================================
async def execute_bulk_promotion(
    event: Any,
    actor_id: int,
    target_user: str,
    chat_filter: Optional[str],
) -> None:
    slots = read_json(SLOTS_FILE).get("slots", {})
    active_slots = [
        (k, v)
        for k, v in sorted(slots.items(), key=lambda x: int(x[0]))
        if v.get("status") == "active"
    ]

    if not active_slots:
        await api_send(
            event.chat_id,
            "⚠️ **No Active Userbot Slots available!** Check Rest/Health panels.",
        )
        return

    scope_label = f"Chat `{chat_filter}`" if chat_filter else "All Shared Groups"
    status_id = await api_send(
        event.chat_id,
        f"⚡ **𝗦𝗨𝗗𝗢 𝗔𝗟𝗟 — 𝗔𝗨𝗧𝗢-𝗔𝗗𝗠𝗜𝗡 𝗣𝗥𝗢𝗠𝗢𝗧𝗜𝗢𝗡**\n"
        f"🎯 **Target:** `{target_user}`\n"
        f"📍 **Scope:** {scope_label}\n"
        f"🤖 **Active Slots:** `{len(active_slots)}`\n\n"
        f"⏳ *Granting admin privileges across all active userbots...*",
    )

    lines = []
    success_count = 0

    for slot_id, sdata in active_slots:
        ok, info = await promote_via_userbot(sdata["session"], target_user, chat_filter)
        owner_nm = sdata.get("owner_name", "Unknown")
        acc_nm = sdata.get("full_name", "Userbot")
        if ok:
            success_count += 1
            lines.append(f"🟢 **Slot #{slot_id}** ({owner_nm} • {acc_nm}): `{info}`")
        else:
            lines.append(f"🔸 **Slot #{slot_id}** ({owner_nm} • {acc_nm}): `{info}`")

    log_audit(
        "SUDO_ALL_PROMOTE",
        actor_id,
        f"Target: {target_user} | Scope: {scope_label} | Succeeded: {success_count}/{len(active_slots)}",
    )

    report = (
        f"🌸 **𝗦𝗨𝗗𝗢 𝗔𝗟𝗟 — 𝗔𝗗𝗠𝗜𝗡 𝗥𝗘𝗣𝗢𝗥𝗧** 🌸\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **Target User:** `{target_user}`\n"
        f"📍 **Scope:** {scope_label}\n"
        f"✅ **Promoted By:** `{success_count} / {len(active_slots)}` slots\n\n"
        + "\n".join(lines[:25])
    )
    home_cb = "menu_home" if event.is_private else "grp_refresh"
    back_btn = [[btn("🌸 Back to Menu", home_cb, "primary")]]
    if status_id:
        await api_edit(event.chat_id, status_id, report, buttons=back_btn)
    else:
        await api_send(event.chat_id, report, buttons=back_btn)


async def execute_group_join_and_deploy(
    event: Any, actor_id: int, group_link: str, target_user: Optional[str]
) -> None:
    cfg = read_json(CONFIG_FILE)
    delay_sec = int(cfg.get("join_delay_sec", 4))
    slots = read_json(SLOTS_FILE).get("slots", {})
    active_slots = [
        (k, v)
        for k, v in sorted(slots.items(), key=lambda x: int(x[0]))
        if v.get("status") == "active"
    ]
    home_cb = "menu_home" if event.is_private else "grp_refresh"

    if not active_slots:
        await api_send(event.chat_id, "⚠️ **No Active Userbot Slots available to deploy!**")
        return

    status_id = await api_send(
        event.chat_id,
        f"🔗 **𝗦𝗠𝗔𝗥𝗧 𝗚𝗥𝗢𝗨𝗣 𝗝𝗢𝗜𝗡 & 𝗨𝗦𝗘𝗥𝗕𝗢𝗧 𝗗𝗘𝗣𝗟𝗢𝗬**\n"
        f"📍 **Link:** `{group_link}`\n"
        f"🎯 **Auto-Admin Target:** `{target_user or 'None'}`\n"
        f"🤖 **Active Slots:** `{len(active_slots)}` (Staggered `{delay_sec}s` delay)\n\n"
        f"⏳ *Joining group and starting live userbot workers...*",
    )

    lines = []
    joined_count = 0
    resolved_chat_id: Optional[int] = None

    for idx, (slot_id, sdata) in enumerate(active_slots):
        ok, join_info, cid = await join_group_via_userbot(sdata["session"], group_link)
        if cid and not resolved_chat_id:
            resolved_chat_id = cid
        await start_slot_worker(slot_id)

        owner_nm = sdata.get("owner_name", "Owner")
        acc_nm = sdata.get("full_name", "Userbot")
        if ok:
            joined_count += 1
            lines.append(
                f"🟢 **Slot #{slot_id}** ({owner_nm} • {acc_nm}): `{join_info}` | `Worker ON`"
            )
        else:
            lines.append(f"🔴 **Slot #{slot_id}** ({owner_nm} • {acc_nm}): `{join_info}`")

        if idx < len(active_slots) - 1:
            await asyncio.sleep(delay_sec)

    promo_summary = ""
    if target_user:
        chat_target = str(resolved_chat_id) if resolved_chat_id else group_link
        promo_ok = 0
        for slot_id, sdata in active_slots:
            p_ok, _ = await promote_via_userbot(sdata["session"], target_user, chat_target)
            if p_ok:
                promo_ok += 1
        promo_summary = f"\n⚡ **Auto-Admin Target (`{target_user}`):** Promoted by `{promo_ok}/{len(active_slots)}` slots"

    log_audit(
        "GROUP_DEPLOY",
        actor_id,
        f"Link: {group_link} | Joined: {joined_count}/{len(active_slots)} | Target: {target_user}",
    )

    final_text = (
        f"🌸 **𝗚𝗥𝗢𝗨𝗣 𝗗𝗘𝗣𝗟𝗢𝗬𝗠𝗘𝗡𝗧 𝗥𝗘𝗣𝗢𝗥𝗧** 🌸\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🔗 **Group:** `{group_link}`\n"
        f"✅ **Joined & Active:** `{joined_count} / {len(active_slots)}`{promo_summary}\n\n"
        + "\n".join(lines[:25])
    )
    if status_id:
        await api_edit(
            event.chat_id,
            status_id,
            final_text,
            buttons=[[btn("🌸 Back to Menu", home_cb, "primary")]],
        )
    else:
        await api_send(
            event.chat_id,
            final_text,
            buttons=[[btn("🌸 Back to Menu", home_cb, "primary")]],
        )


async def execute_group_leave_all(event: Any, actor_id: int, group_link: str) -> None:
    slots = read_json(SLOTS_FILE).get("slots", {})
    active_slots = [
        (k, v)
        for k, v in sorted(slots.items(), key=lambda x: int(x[0]))
        if v.get("status") == "active"
    ]
    home_cb = "menu_home" if event.is_private else "grp_refresh"

    if not active_slots:
        await api_send(event.chat_id, "⚠️ **No Active Userbot Slots available!**")
        return

    status_id = await api_send(
        event.chat_id,
        f"🚪 **Exiting `{group_link}` across `{len(active_slots)}` active slots...**",
    )
    lines = []
    left_count = 0
    for slot_id, sdata in active_slots:
        ok, info = await leave_group_via_userbot(sdata["session"], group_link)
        if ok:
            left_count += 1
            lines.append(f"🟢 **Slot #{slot_id}** (`{sdata.get('owner_name')}`): `{info}`")
        else:
            lines.append(f"🔴 **Slot #{slot_id}** (`{sdata.get('owner_name')}`): `{info}`")

    log_audit(
        "GROUP_LEAVE_ALL", actor_id, f"Target: {group_link} | Left: {left_count}/{len(active_slots)}"
    )
    out_txt = (
        f"🌸 **𝗚𝗥𝗢𝗨𝗣 𝗟𝗘𝗔𝗩𝗘 𝗥𝗘𝗣𝗢𝗥𝗧** 🌸\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ **Exited:** `{left_count} / {len(active_slots)}`\n\n" + "\n".join(lines[:25])
    )
    if status_id:
        await api_edit(
            event.chat_id,
            status_id,
            out_txt,
            buttons=[[btn("🌸 Back to Menu", home_cb, "primary")]],
        )
    else:
        await api_send(
            event.chat_id,
            out_txt,
            buttons=[[btn("🌸 Back to Menu", home_cb, "primary")]],
        )


async def execute_bio_sync_all(event: Any, actor_id: int, new_bio: str) -> None:
    slots = read_json(SLOTS_FILE).get("slots", {})
    active_slots = [
        (k, v)
        for k, v in sorted(slots.items(), key=lambda x: int(x[0]))
        if v.get("status") == "active"
    ]
    home_cb = "menu_home" if event.is_private else "grp_refresh"

    if not active_slots:
        await api_send(event.chat_id, "⚠️ **No Active Userbot Slots available!**")
        return

    status_id = await api_send(
        event.chat_id, f"🪪 **Syncing Bio across `{len(active_slots)}` active slots...**"
    )
    updated = 0
    for slot_id, sdata in active_slots:
        ub = build_userbot_client(sdata["session"], timeout=12)
        try:
            await ub.connect()
            if await ub.is_user_authorized():
                bio_txt = (
                    new_bio.replace("{slot}", str(slot_id))
                    .replace("{owner}", sdata.get("owner_name", ""))[:70]
                )
                await ub(UpdateProfileRequest(about=bio_txt))
                updated += 1
        except Exception:
            pass
        finally:
            try:
                await ub.disconnect()
            except Exception:
                pass

    log_audit("BIO_SYNC", actor_id, f"Updated bio on {updated}/{len(active_slots)} slots")
    done_txt = f"✅ **Bio Updated on `{updated} / {len(active_slots)}` Active Slots!**"
    if status_id:
        await api_edit(
            event.chat_id,
            status_id,
            done_txt,
            buttons=[[btn("🌸 Back to Menu", home_cb, "primary")]],
        )
    else:
        await api_send(
            event.chat_id,
            done_txt,
            buttons=[[btn("🌸 Back to Menu", home_cb, "primary")]],
        )


# ==============================================================================
# BACKGROUND AUTO-HEALTH WATCHDOG
# ==============================================================================
async def background_health_watchdog() -> None:
    while True:
        cfg = read_json(CONFIG_FILE)
        interval_min = max(5, int(cfg.get("watchdog_interval_min", 30)))
        await asyncio.sleep(interval_min * 60)

        slots_db = read_json(SLOTS_FILE)
        slots = slots_db.get("slots", {})
        if not slots:
            continue

        dead_alerts = []
        for sid, info in list(slots.items()):
            if info.get("status") == "resting":
                continue
            res = await inspect_userbot_session(info["session"])
            prev_status = info.get("status")
            info.update(
                {
                    "status": res["status"],
                    "last_checked": now_str(),
                    "ping_ms": res.get("ping_ms", 0),
                    "dialogs_count": res.get("dialogs_count", 0),
                    "health_note": res.get("reason", "Watchdog check"),
                }
            )
            slots_db["slots"][sid] = info
            if not res["ok"] and prev_status == "active":
                await stop_slot_worker(sid)
                dead_alerts.append(
                    f"🔴 **Slot #{sid}** (`{info.get('owner_name')}` • `{info.get('full_name')}`): `{res['reason']}`"
                )

        write_json(SLOTS_FILE, slots_db)
        if dead_alerts:
            owners = read_json(OWNERS_FILE)
            main_id = int(owners.get("main_owner", 0))
            if main_id:
                await api_send(
                    main_id,
                    f"⚠️ **{BRAND} 𝗪𝗔𝗧𝗖𝗛𝗗𝗢𝗚 𝗦𝗘𝗦𝗦𝗜𝗢𝗡 𝗔𝗟𝗘𝗥𝗧**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(dead_alerts),
                )


# ==============================================================================
# INLINE CALLBACK ROUTER (WORKS SEAMLESSLY IN BOTH PM & GROUPS)
# ==============================================================================
@bot.on(events.CallbackQuery)
async def callback_router(event: events.CallbackQuery.Event) -> None:
    user_id = event.sender_id
    cb_id = str(event.id)
    if not user_id or not is_authorized(user_id):
        await api_answer_callback(cb_id, "🔒 Unauthorized! Owner access only.", show_alert=True)
        return

    data = event.data.decode("utf-8")
    key = (event.chat_id, user_id)
    is_priv = event.is_private
    home_cb = "menu_home" if is_priv else "grp_refresh"
    hint = prompt_hint(is_priv)

    # ---- HOME / CANCEL ----
    if data in ("menu_home", "otp_cancel"):
        USER_STATES.pop(key, None)
        await cleanup_pending_login(key)
        await api_answer_callback(cb_id)
        if is_priv:
            await send_or_edit_sakura(
                event, build_main_menu_caption(), build_main_menu_buttons(user_id)
            )
        else:
            chat = await event.get_chat()
            title = getattr(chat, "title", "Group Chat")
            await send_or_edit_sakura(
                event,
                build_group_menu_caption(title, event.chat_id),
                build_group_menu_buttons(user_id),
            )

    elif data == "grp_refresh":
        USER_STATES.pop(key, None)
        await api_answer_callback(cb_id)
        chat = await event.get_chat()
        title = getattr(chat, "title", "Group Chat")
        await send_or_edit_sakura(
            event,
            build_group_menu_caption(title, event.chat_id),
            build_group_menu_buttons(user_id),
        )

    # ---- OTP RESEND ----
    elif data == "otp_resend":
        pending = PENDING_LOGINS.get(key)
        if not pending:
            await api_answer_callback(cb_id, "⚠️ No active OTP login session.", show_alert=True)
            return
        client: TelegramClient = pending["client"]
        try:
            if not client.is_connected():
                await client.connect()
            sent = await client.send_code_request(pending["phone"], force_sms=True)
            pending["phone_code_hash"] = sent.phone_code_hash
            await api_answer_callback(
                cb_id, "📲 Re-sent OTP via SMS/next delivery channel!", show_alert=True
            )
        except Exception as exc:
            await api_answer_callback(cb_id, f"Resend failed: {str(exc)[:60]}", show_alert=True)

    # ---- HOST ALL BOTS PANEL ----
    elif data.startswith("menu_host_"):
        await api_answer_callback(cb_id)
        page = int(data.split("_")[-1])
        slots = read_json(SLOTS_FILE).get("slots", {})
        sorted_items = sorted(slots.items(), key=lambda x: int(x[0]))

        text = (
            sakura_header("𝗛𝗢𝗦𝗧 𝗔𝗟𝗟 𝗨𝗦𝗘𝗥𝗕𝗢𝗧𝗦 𝗘𝗡𝗚𝗜𝗡𝗘")
            + f"🚀 **Active Hosted Workers:** `{count_running_workers()} / {len(slots)}`\n"
            f"💡 *Built-in cmds:* `.ping`, `.alive`, `.stats`, `.cmds`, `.id`, `.leave`\n\n"
        )
        btn_rows = [
            [
                btn("▶️ Start All Active", "host_start_all", "success"),
                btn("⏹️ Stop All Workers", "host_stop_all", "danger"),
            ]
        ]

        per_page = 6
        start = page * per_page
        chunk = sorted_items[start : start + per_page]

        for sid, info in chunk:
            worker = ACTIVE_WORKERS.get(sid)
            is_running = bool(worker and worker.get("task") and not worker["task"].done())
            st_badge = (
                "🟢 RUNNING"
                if is_running
                else ("🌙 RESTING" if info.get("status") == "resting" else "⚪ STOPPED")
            )
            cmds_cnt = worker.get("cmds_handled", 0) if is_running else 0
            text += (
                f"• **Slot #{sid}** (`{info.get('owner_name')}` • `{info.get('full_name')}`): "
                f"`{st_badge}` | Cmds: `{cmds_cnt}`\n"
            )
            btn_rows.append(
                [
                    btn(
                        f"{'⏹️ Stop' if is_running else '▶️ Start'} Slot #{sid} ({info.get('owner_name')})",
                        f"host_toggle_{sid}_{page}",
                        "danger" if is_running else "success",
                    )
                ]
            )

        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", f"menu_host_{page - 1}", "primary"))
        if start + per_page < len(sorted_items):
            nav.append(btn("Next ➡️", f"menu_host_{page + 1}", "primary"))
        if nav:
            btn_rows.append(nav)
        btn_rows.append([btn("🌸 Back to Menu", home_cb, "primary")])

        await send_or_edit_sakura(event, text, btn_rows)

    elif data == "host_start_all":
        slots = read_json(SLOTS_FILE).get("slots", {})
        started = 0
        for sid, info in slots.items():
            if info.get("status") == "active":
                ok, _ = await start_slot_worker(sid)
                if ok:
                    started += 1
        log_audit("HOST_START_ALL", user_id, f"Started {started} userbot host workers")
        await api_answer_callback(cb_id, f"🚀 Started {started} userbot workers!", show_alert=True)
        event.data = b"menu_host_0"
        await callback_router(event)

    elif data == "host_stop_all":
        running_ids = list(ACTIVE_WORKERS.keys())
        stopped = 0
        for sid in running_ids:
            ok, _ = await stop_slot_worker(sid)
            if ok:
                stopped += 1
        log_audit("HOST_STOP_ALL", user_id, f"Stopped {stopped} userbot host workers")
        await api_answer_callback(cb_id, f"⏹️ Stopped {stopped} userbot workers!", show_alert=True)
        event.data = b"menu_host_0"
        await callback_router(event)

    elif data.startswith("host_toggle_"):
        parts = data.split("_")
        sid, page = parts[2], parts[3]
        worker = ACTIVE_WORKERS.get(sid)
        is_running = bool(worker and worker.get("task") and not worker["task"].done())
        if is_running:
            await stop_slot_worker(sid)
            await api_answer_callback(cb_id, f"⏹️ Stopped Slot #{sid}", show_alert=False)
        else:
            ok, msg = await start_slot_worker(sid)
            await api_answer_callback(cb_id, f"Slot #{sid}: {msg}", show_alert=not ok)
        event.data = f"menu_host_{page}".encode()
        await callback_router(event)

    # ---- OTP / 2FA LOGIN START ----
    elif data == "menu_otp_start":
        await api_answer_callback(cb_id)
        slots = read_json(SLOTS_FILE).get("slots", {})
        next_free = 1
        while str(next_free) in slots:
            next_free += 1
        USER_STATES[key] = {"step": "otp_slot_num", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗢𝗧𝗣 & 𝟮𝗙𝗔 𝗦𝗟𝗢𝗧 𝗟𝗢𝗚𝗜𝗡")
            + f"📲 Log in any Telegram number directly via OTP + 2FA to generate & host its session.\n\n"
            f"🔢 Send the **Slot Number** to assign (Suggested free slot: `{next_free}`):{hint}",
            [[btn("❌ Cancel", "otp_cancel", "danger")]],
        )

    # ---- ADD STRING SESSION ----
    elif data == "menu_add_slot":
        await api_answer_callback(cb_id)
        slots = read_json(SLOTS_FILE).get("slots", {})
        next_free = 1
        while str(next_free) in slots:
            next_free += 1
        USER_STATES[key] = {"step": "add_slot_num", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗔𝗗𝗗 𝗦𝗧𝗥𝗜𝗡𝗚 𝗦𝗘𝗦𝗦𝗜𝗢𝗡")
            + f"🔢 Send the **Slot Number** to assign (Suggested free slot: `{next_free}`):{hint}",
            [[btn("❌ Cancel", home_cb, "danger")]],
        )

    # ---- SMART GROUP JOIN, LEAVE & BIO SYNC ----
    elif data == "menu_deploy_group":
        await api_answer_callback(cb_id)
        USER_STATES[key] = {"step": "deploy_group_link", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗚𝗥𝗢𝗨𝗣 𝗝𝗢𝗜𝗡 & 𝗗𝗘𝗣𝗟𝗢𝗬")
            + f"🔗 Send the **Group Invite Link** (`https://t.me/+...` or `@groupusername`) "
            f"where all active userbot slots should join and activate:{hint}",
            [[btn("❌ Cancel", home_cb, "danger")]],
        )

    elif data == "menu_leave_group":
        await api_answer_callback(cb_id)
        USER_STATES[key] = {"step": "leave_group_link", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗚𝗥𝗢𝗨𝗣 𝗟𝗘𝗔𝗩𝗘 𝗔𝗟𝗟 𝗦𝗟𝗢𝗧𝗦")
            + f"🚪 Send the **Group Username or Chat ID** (`@groupusername` or `-100...`) that all active slots should leave:{hint}",
            [[btn("❌ Cancel", home_cb, "danger")]],
        )

    elif data == "menu_profile_sync":
        await api_answer_callback(cb_id)
        USER_STATES[key] = {"step": "profile_sync_bio", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗦𝗬𝗡𝗖 𝗦𝗟𝗢𝗧 𝗣𝗥𝗢𝗙𝗜𝗟𝗘 𝗕𝗜𝗢")
            + f"🪪 Send the new **Bio Text** to apply across all active userbot slots.\n"
            f"*(Placeholders supported: `{{slot}}` and `{{owner}}`, max 70 chars)*:{hint}",
            [[btn("❌ Cancel", home_cb, "danger")]],
        )

    # ---- PER-SLOT CUSTOM COMMANDS STUDIO ----
    elif data.startswith("menu_cmds_"):
        await api_answer_callback(cb_id)
        page = int(data.split("_")[-1])
        cmds_db = read_json(COMMANDS_FILE).get("commands", {})
        items = list(cmds_db.items())

        lines = [
            sakura_header("𝗣𝗘𝗥-𝗦𝗟𝗢𝗧 𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗦𝗧𝗨𝗗𝗜𝗢"),
            "⚙️ *Assign commands globally (`ALL`) or exclusively to a specific slot (`Slot #N`).*\n",
        ]
        btn_rows = [[btn("➕ Add Per-Slot Command", "cmd_new_scope", "success")]]

        per_page = 6
        start = page * per_page
        chunk = items[start : start + per_page]
        for idx, (comp_key, cfg) in enumerate(chunk, start=start):
            trig = cfg.get("trigger", comp_key)
            sc = str(cfg.get("scope", "ALL"))
            badge = "🌐 ALL" if sc == "ALL" else f"🎰 Slot #{sc}"
            preview = str(cfg.get("reply", ""))[:28]
            lines.append(f"• `{trig}` [{badge}] → `{preview}`")
            btn_rows.append(
                [btn(f"🗑️ Delete {trig} ({badge})", f"cmd_del_idx_{idx}", "danger")]
            )

        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", f"menu_cmds_{page - 1}", "primary"))
        if start + per_page < len(items):
            nav.append(btn("Next ➡️", f"menu_cmds_{page + 1}", "primary"))
        if nav:
            btn_rows.append(nav)
        btn_rows.append([btn("🌸 Back to Menu", home_cb, "primary")])

        await send_or_edit_sakura(event, "\n".join(lines), btn_rows)

    elif data == "cmd_new_scope":
        await api_answer_callback(cb_id)
        slots = read_json(SLOTS_FILE).get("slots", {})
        sorted_slots = sorted(slots.items(), key=lambda x: int(x[0]))
        btn_rows = [[btn("🌐 ALL Active Slots (Global)", "cmd_scope_ALL", "success")]]
        for sid, info in sorted_slots[:15]:
            btn_rows.append(
                [
                    btn(
                        f"🎰 Slot #{sid} Only ({info.get('owner_name')})",
                        f"cmd_scope_{sid}",
                        "primary",
                    )
                ]
            )
        btn_rows.append([btn("❌ Cancel", "menu_cmds_0", "danger")])

        await send_or_edit_sakura(
            event,
            sakura_header("𝗦𝗘𝗟𝗘𝗖𝗧 𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗧𝗔𝗥𝗚𝗘𝗧 𝗦𝗟𝗢𝗧")
            + "🎯 Choose whether this custom command should run on **ALL slots** or on **one specific slot**:",
            btn_rows,
        )

    elif data.startswith("cmd_scope_"):
        await api_answer_callback(cb_id)
        chosen_scope = data.split("cmd_scope_", 1)[1]
        USER_STATES[key] = {"step": "cmd_add_trigger", "data": {"scope": chosen_scope}}
        scope_label = "ALL Slots" if chosen_scope == "ALL" else f"Slot #{chosen_scope}"
        await send_or_edit_sakura(
            event,
            sakura_header("𝗡𝗘𝗪 𝗖𝗢𝗠𝗠𝗔𝗡𝗗 𝗧𝗥𝗜𝗚𝗚𝗘𝗥")
            + f"🎯 **Target Scope:** `{scope_label}`\n\n"
            f"⌨️ Send the **Command Trigger** (e.g., `.intro`, `.rules`, `.tag`):{hint}",
            [[btn("❌ Cancel", "menu_cmds_0", "danger")]],
        )

    elif data.startswith("cmd_del_idx_"):
        del_idx = int(data.split("_")[-1])
        cmds_db = read_json(COMMANDS_FILE)
        keys_list = list(cmds_db.get("commands", {}).keys())
        if 0 <= del_idx < len(keys_list):
            removed_key = keys_list[del_idx]
            del cmds_db["commands"][removed_key]
            write_json(COMMANDS_FILE, cmds_db)
            log_audit("DEL_COMMAND", user_id, f"Deleted custom command `{removed_key}`")
            await api_answer_callback(cb_id, f"🗑️ Deleted {removed_key}", show_alert=True)
        event.data = b"menu_cmds_0"
        await callback_router(event)

    # ---- MANAGE SLOTS (PAGINATED) ----
    elif data.startswith("menu_slots_"):
        await api_answer_callback(cb_id)
        page = int(data.split("_")[-1])
        slots = read_json(SLOTS_FILE).get("slots", {})
        sorted_items = sorted(slots.items(), key=lambda x: int(x[0]))

        if not sorted_items:
            await send_or_edit_sakura(
                event,
                sakura_header("𝗦𝗟𝗢𝗧 𝗠𝗔𝗡𝗔𝗚𝗘𝗥") + "📭 *No userbot slots configured yet.*",
                [
                    [btn("📲 Add via OTP / 2FA", "menu_otp_start", "success")],
                    [btn("🌸 Back to Menu", home_cb, "primary")],
                ],
            )
            return

        per_page = 6
        start = page * per_page
        chunk = sorted_items[start : start + per_page]

        lines = [sakura_header("𝗦𝗟𝗢𝗧 𝗠𝗔𝗡𝗔𝗚𝗘𝗥 & 𝗟𝗢𝗔𝗗𝗦")]
        btn_rows = []
        for sid, info in chunk:
            st = info.get("status", "unknown")
            icon = "🟢" if st == "active" else ("🌙" if st == "resting" else "🔴")
            b_style = "success" if st == "active" else ("primary" if st == "resting" else "danger")
            ping = info.get("ping_ms", 0)
            d_load = info.get("dialogs_count", 0)
            lines.append(
                f"{icon} **Slot #{sid}** — `{info.get('owner_name')}`\n"
                f"   └ `{info.get('full_name', 'N/A')}` | `{ping}ms` | `{d_load} chats`"
            )
            btn_rows.append(
                [
                    btn(
                        f"{icon} Inspect Slot #{sid} ({info.get('owner_name')})",
                        f"slot_view_{sid}",
                        b_style,
                    )
                ]
            )

        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", f"menu_slots_{page - 1}", "primary"))
        if start + per_page < len(sorted_items):
            nav.append(btn("Next ➡️", f"menu_slots_{page + 1}", "primary"))
        if nav:
            btn_rows.append(nav)
        btn_rows.append([btn("🌸 Back to Menu", home_cb, "primary")])

        await send_or_edit_sakura(event, "\n".join(lines), btn_rows)

    elif data.startswith("slot_view_"):
        await api_answer_callback(cb_id)
        sid = data.split("_")[-1]
        slots = read_json(SLOTS_FILE).get("slots", {})
        info = slots.get(sid)
        if not info:
            await api_answer_callback(cb_id, "Slot not found!", show_alert=True)
            return

        st = info.get("status", "unknown")
        icon = "🟢" if st == "active" else ("🌙" if st == "resting" else "🔴")
        worker = ACTIVE_WORKERS.get(sid)
        is_hosted = bool(worker and worker.get("task") and not worker["task"].done())

        cmds_db = read_json(COMMANDS_FILE).get("commands", {})
        slot_cmds = [
            v["trigger"] for v in cmds_db.values() if str(v.get("scope")) == str(sid)
        ]

        text = (
            sakura_header(f"𝗦𝗟𝗢𝗧 #{sid} 𝗧𝗘𝗟𝗘𝗠𝗘𝗧𝗥𝗬")
            + f"👤 **Slot Owner:** `{info.get('owner_name')}`\n"
            f"🤖 **Bot Account:** `{info.get('full_name')}` ({info.get('username')})\n"
            f"🆔 **Account ID:** `{info.get('user_id')}`\n"
            f"{icon} **Account State:** `{st.upper()}`\n"
            f"🚀 **Host Worker:** `{'RUNNING' if is_hosted else 'STOPPED'}`\n"
            f"⚙️ **Slot-Only Cmds:** `{', '.join(slot_cmds) or 'None'}`\n"
            f"🩺 **Health Note:** `{info.get('health_note', 'N/A')}`\n"
            f"⚡ **Ping:** `{info.get('ping_ms', 0)} ms` | 💬 **Dialogs:** `{info.get('dialogs_count', 0)}`\n"
            f"🕒 **Last Checked:** `{info.get('last_checked', 'N/A')}`"
        )

        toggle_label = "🌙 Put on Rest" if st == "active" else "☀️ Wake / Activate"
        toggle_style = "danger" if st == "active" else "success"
        buttons = [
            [
                btn("🩺 Ping & Check", f"slot_check_{sid}", "primary"),
                btn(toggle_label, f"slot_toggle_{sid}", toggle_style),
            ],
            [
                btn("➕ Add Slot Command", f"cmd_scope_{sid}", "success"),
                btn("🗑️ Remove Slot", f"slot_del_{sid}", "danger"),
            ],
            [
                btn("⬅️ All Slots", "menu_slots_0", "primary"),
            ],
        ]
        await send_or_edit_sakura(event, text, buttons)

    elif data.startswith("slot_check_"):
        sid = data.split("_")[-1]
        slots_db = read_json(SLOTS_FILE)
        info = slots_db.get("slots", {}).get(sid)
        if not info:
            return
        await api_answer_callback(cb_id, "🩺 Checking slot session...", show_alert=False)
        health = await inspect_userbot_session(info["session"])
        new_status = (
            "resting"
            if (info.get("status") == "resting" and health["ok"])
            else health["status"]
        )
        info.update(
            {
                "status": new_status,
                "last_checked": now_str(),
                "ping_ms": health.get("ping_ms", 0),
                "user_id": health.get("user_id", info.get("user_id", 0)),
                "full_name": health.get("full_name", info.get("full_name", "Unknown")),
                "username": health.get("username", info.get("username", "N/A")),
                "dialogs_count": health.get("dialogs_count", 0),
                "health_note": health.get("reason", "Checked"),
            }
        )
        slots_db["slots"][sid] = info
        write_json(SLOTS_FILE, slots_db)
        event.data = f"slot_view_{sid}".encode()
        await callback_router(event)

    elif data.startswith("slot_toggle_"):
        sid = data.split("_")[-1]
        slots_db = read_json(SLOTS_FILE)
        info = slots_db.get("slots", {}).get(sid)
        if not info:
            return
        old_st = info.get("status", "active")
        new_st = "resting" if old_st == "active" else "active"
        info["status"] = new_st
        slots_db["slots"][sid] = info
        write_json(SLOTS_FILE, slots_db)
        if new_st == "resting":
            await stop_slot_worker(sid)
        log_audit("SLOT_REST_TOGGLE", user_id, f"Slot #{sid} -> {new_st.upper()}")
        event.data = f"slot_view_{sid}".encode()
        await callback_router(event)

    elif data.startswith("slot_del_"):
        sid = data.split("_")[-1]
        await stop_slot_worker(sid)
        slots_db = read_json(SLOTS_FILE)
        removed = slots_db.get("slots", {}).pop(sid, None)
        write_json(SLOTS_FILE, slots_db)
        if removed:
            log_audit(
                "REMOVE_SLOT",
                user_id,
                f"Removed Slot #{sid} | Owner: {removed.get('owner_name')}",
            )
            await api_answer_callback(cb_id, f"🗑️ Slot #{sid} removed.", show_alert=True)
        event.data = b"menu_slots_0"
        await callback_router(event)

    # ---- REST PANEL ----
    elif data.startswith("menu_rest_"):
        await api_answer_callback(cb_id)
        page = int(data.split("_")[-1])
        slots = read_json(SLOTS_FILE).get("slots", {})
        sorted_items = sorted(slots.items(), key=lambda x: int(x[0]))

        text = (
            sakura_header("𝗥𝗘𝗦𝗧 & 𝗖𝗢𝗢𝗟𝗗𝗢𝗪𝗡 𝗠𝗔𝗡𝗔𝗚𝗘𝗥")
            + "🌙 *Rest Mode pauses host workers and auto-admin promotions for selected slots.*\n\n"
        )
        btn_rows = [
            [
                btn("🌙 Rest ALL Active", "rest_all_on", "danger"),
                btn("☀️ Wake ALL Resting", "rest_all_off", "success"),
            ]
        ]

        per_page = 6
        start = page * per_page
        chunk = sorted_items[start : start + per_page]
        for sid, info in chunk:
            st = info.get("status", "unknown")
            badge = (
                "🌙 RESTING"
                if st == "resting"
                else ("🟢 ACTIVE" if st == "active" else "🔴 DEAD")
            )
            b_style = "primary" if st == "resting" else ("success" if st == "active" else "danger")
            btn_rows.append(
                [
                    btn(
                        f"Slot #{sid} ({info.get('owner_name')}) — {badge}",
                        f"rest_flip_{sid}_{page}",
                        b_style,
                    )
                ]
            )

        nav = []
        if page > 0:
            nav.append(btn("⬅️ Prev", f"menu_rest_{page - 1}", "primary"))
        if start + per_page < len(sorted_items):
            nav.append(btn("Next ➡️", f"menu_rest_{page + 1}", "primary"))
        if nav:
            btn_rows.append(nav)
        btn_rows.append([btn("🌸 Back to Menu", home_cb, "primary")])

        await send_or_edit_sakura(event, text, btn_rows)

    elif data.startswith("rest_flip_"):
        parts = data.split("_")
        sid, page = parts[2], parts[3]
        slots_db = read_json(SLOTS_FILE)
        info = slots_db.get("slots", {}).get(sid)
        if info:
            info["status"] = "resting" if info.get("status") == "active" else "active"
            if info["status"] == "resting":
                await stop_slot_worker(sid)
            slots_db["slots"][sid] = info
            write_json(SLOTS_FILE, slots_db)
        event.data = f"menu_rest_{page}".encode()
        await callback_router(event)

    elif data in ("rest_all_on", "rest_all_off"):
        target_st = "resting" if data == "rest_all_on" else "active"
        from_st = "active" if data == "rest_all_on" else "resting"
        slots_db = read_json(SLOTS_FILE)
        count = 0
        for sid, info in slots_db.get("slots", {}).items():
            if info.get("status") == from_st:
                info["status"] = target_st
                if target_st == "resting":
                    await stop_slot_worker(sid)
                count += 1
        write_json(SLOTS_FILE, slots_db)
        log_audit("BULK_REST", user_id, f"Set {count} slots to {target_st.upper()}")
        await api_answer_callback(
            cb_id, f"Updated {count} slots to {target_st.upper()}!", show_alert=True
        )
        event.data = b"menu_rest_0"
        await callback_router(event)

    # ---- FULL HEALTH CHECK ----
    elif data in ("menu_health_all", "grp_health"):
        slots_db = read_json(SLOTS_FILE)
        slots = slots_db.get("slots", {})
        if not slots:
            await api_answer_callback(cb_id, "No slots added yet!", show_alert=True)
            return

        await api_answer_callback(cb_id, "🩺 Scanning all userbot sessions...", show_alert=False)
        await send_or_edit_sakura(
            event,
            sakura_header("𝗟𝗜𝗩𝗘 𝗛𝗘𝗔𝗟𝗧𝗛 & 𝗟𝗢𝗔𝗗 𝗦𝗖𝗔𝗡")
            + f"⏳ *Inspecting `{len(slots)}` sessions for validity, ping, and chat load...*",
            [],
        )

        report_lines = []
        healthy_c = resting_c = dead_c = 0

        for sid, info in sorted(slots.items(), key=lambda x: int(x[0])):
            res = await inspect_userbot_session(info["session"])
            was_resting = info.get("status") == "resting"

            if res["ok"] and was_resting:
                final_st = "resting"
                resting_c += 1
                icon = "🌙"
            elif res["ok"]:
                final_st = "active"
                healthy_c += 1
                icon = "🟢"
            else:
                final_st = res["status"]
                dead_c += 1
                icon = "🔴"
                await stop_slot_worker(sid)

            info.update(
                {
                    "status": final_st,
                    "last_checked": now_str(),
                    "ping_ms": res.get("ping_ms", 0),
                    "user_id": res.get("user_id", info.get("user_id", 0)),
                    "full_name": res.get("full_name", info.get("full_name", "Unknown")),
                    "username": res.get("username", info.get("username", "N/A")),
                    "dialogs_count": res.get("dialogs_count", 0),
                    "health_note": res.get("reason", "Checked"),
                }
            )
            slots_db["slots"][sid] = info
            report_lines.append(
                f"{icon} **Slot #{sid}** (`{info.get('owner_name')}`): "
                f"`{info.get('full_name')}` • `{res.get('ping_ms', 0)}ms` • `{res.get('dialogs_count', 0)} chats`"
            )

        write_json(SLOTS_FILE, slots_db)
        log_audit("FULL_HEALTH_SCAN", user_id, f"OK: {healthy_c} | Rest: {resting_c} | Dead: {dead_c}")

        summary = (
            sakura_header("𝗛𝗘𝗔𝗟𝗧𝗛 & 𝗦𝗘𝗦𝗦𝗜𝗢𝗡 𝗥𝗘𝗣𝗢𝗥𝗧")
            + f"🟢 **Working:** `{healthy_c}` | 🌙 **Resting:** `{resting_c}` | 🔴 **Dead:** `{dead_c}`\n"
            f"🖥️ **Host Load:** CPU `{psutil.cpu_percent()}%` • RAM `{psutil.virtual_memory().percent}%`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(report_lines[:25])
        )
        await send_or_edit_sakura(
            event, summary, [[btn("🌸 Back to Menu", home_cb, "primary")]]
        )

    # ---- PROMOTE & GROUP SCAN ----
    elif data == "menu_promote_pm":
        await api_answer_callback(cb_id)
        USER_STATES[key] = {"step": "promote_target_pm", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗦𝗨𝗗𝗢 𝗔𝗟𝗟 — 𝗔𝗨𝗧𝗢-𝗔𝗗𝗠𝗜𝗡")
            + f"🎯 Send the **@username** or **User ID** to promote via all active userbot slots:{hint}",
            [[btn("❌ Cancel", home_cb, "danger")]],
        )

    elif data == "grp_promote":
        await api_answer_callback(cb_id)
        USER_STATES[key] = {"step": "promote_target_group", "data": {}}
        await api_send(
            event.chat_id,
            f"🌸 **{BRAND} 𝗚𝗥𝗢𝗨𝗣 𝗦𝗨𝗗𝗢 𝗔𝗟𝗟**\n"
            f"🎯 Reply to this message with the **@username** or **User ID** to grant admin in this group:",
            buttons=[[btn("❌ Cancel", "grp_refresh", "danger")]],
        )

    elif data == "grp_scan":
        slots = read_json(SLOTS_FILE).get("slots", {})
        if not slots:
            await api_answer_callback(cb_id, "No slots configured!", show_alert=True)
            return
        await api_answer_callback(cb_id, "🔍 Scanning slots in group...", show_alert=False)
        scan_lines = []
        for sid, info in sorted(slots.items(), key=lambda x: int(x[0])):
            if info.get("status") == "resting":
                scan_lines.append(f"🌙 **Slot #{sid}** (`{info.get('owner_name')}`): `On Rest`")
                continue
            present, note = await check_userbot_in_chat(info["session"], event.chat_id)
            icon = "🟢" if present else "🔸"
            scan_lines.append(
                f"{icon} **Slot #{sid}** (`{info.get('owner_name')}` • `{info.get('full_name')}`): `{note}`"
            )
        await send_or_edit_sakura(
            event,
            sakura_header("𝗚𝗥𝗢𝗨𝗣 𝗦𝗟𝗢𝗧 𝗣𝗥𝗘𝗦𝗘𝗡𝗖𝗘") + "\n".join(scan_lines[:25]),
            [[btn("🌸 Back to Group Menu", "grp_refresh", "primary")]],
        )

    # ---- PHOTO FRAME, REPORTS & OWNERS ----
    elif data == "menu_set_frame":
        await api_answer_callback(cb_id)
        USER_STATES[key] = {"step": "set_banner", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗦𝗔𝗞𝗨𝗥𝗔 𝗣𝗛𝗢𝗧𝗢 𝗙𝗥𝗔𝗠𝗘 𝗦𝗘𝗧")
            + f"🖼️ Send the **Photo** (or direct `https://` image link) for the upper menu frame:{hint}",
            [
                [btn("🗑️ Clear Frame", "clear_frame", "danger")],
                [btn("❌ Cancel", home_cb, "danger")],
            ],
        )

    elif data == "clear_frame":
        USER_STATES.pop(key, None)
        cfg = read_json(CONFIG_FILE)
        cfg["banner_photo"] = ""
        write_json(CONFIG_FILE, cfg)
        await api_answer_callback(cb_id, "🖼️ Photo frame cleared!", show_alert=True)
        if is_priv:
            await send_or_edit_sakura(
                event, build_main_menu_caption(), build_main_menu_buttons(user_id)
            )
        else:
            chat = await event.get_chat()
            title = getattr(chat, "title", "Group Chat")
            await send_or_edit_sakura(
                event,
                build_group_menu_caption(title, event.chat_id),
                build_group_menu_buttons(user_id),
            )

    elif data == "menu_reports":
        await api_answer_callback(cb_id)
        logs = read_json(LOGS_FILE).get("logs", [])[:12]
        slots = read_json(SLOTS_FILE).get("slots", {})
        log_text = (
            "\n".join(f"• `[{l['timestamp']}]` **{l['action']}**: {l['details']}" for l in logs)
            if logs
            else "*No events logged yet.*"
        )
        await send_or_edit_sakura(
            event,
            sakura_header("𝗥𝗘𝗣𝗢𝗥𝗧𝗦 & 𝗔𝗨𝗗𝗜𝗧 𝗟𝗢𝗚𝗦")
            + f"📦 **Total Slots:** `{len(slots)}` | 🚀 **Running Hosts:** `{count_running_workers()}`\n\n{log_text}",
            [
                [btn("📥 Export Full Report (.txt + .json)", "export_report", "success")],
                [btn("🌸 Back to Menu", home_cb, "primary")],
            ],
        )

    elif data == "export_report":
        await api_answer_callback(cb_id, "📥 Exporting report...", show_alert=False)
        slots = read_json(SLOTS_FILE).get("slots", {})
        logs = read_json(LOGS_FILE).get("logs", [])
        report_filename = f"sid_userbot_report_{int(time.time())}.txt"

        with open(report_filename, "w", encoding="utf-8") as f:
            f.write(f"=== {BRAND} USERBOT HANDLER - FULL SYSTEM REPORT ===\n")
            f.write(f"Generated At: {now_str()}\nTotal Slots: {len(slots)}\n\n")
            for sid, s in sorted(slots.items(), key=lambda x: int(x[0])):
                f.write(
                    f"Slot #{sid} | Owner: {s.get('owner_name')} | Account: {s.get('full_name')} "
                    f"({s.get('username')}, ID: {s.get('user_id')}) | Status: {s.get('status')} | "
                    f"Ping: {s.get('ping_ms')}ms | Dialogs: {s.get('dialogs_count')} | "
                    f"Note: {s.get('health_note')}\n"
                )
            f.write("\n--- AUDIT LOGS ---\n")
            for entry in logs:
                f.write(f"[{entry['timestamp']}] [{entry['action']}] -> {entry['details']}\n")

        await event.respond(
            f"📊 **Full Telemetry Report & Slots Database** (`{now_str()}`)",
            file=[report_filename, SLOTS_FILE],
        )
        if os.path.exists(report_filename):
            os.remove(report_filename)

    elif data == "menu_owners":
        if not is_main_owner(user_id):
            await api_answer_callback(cb_id, "🔒 Main Owner only.", show_alert=True)
            return
        await api_answer_callback(cb_id)
        owners = read_json(OWNERS_FILE)
        co_list = owners.get("co_owners", [])
        co_str = "\n".join(f"• `{cid}`" for cid in co_list) if co_list else "*No co-owners added.*"

        btn_rows = [[btn("➕ Add Co-Owner", "owner_add", "success")]]
        for cid in co_list:
            btn_rows.append([btn(f"🗑️ Remove Co-Owner {cid}", f"owner_del_{cid}", "danger")])
        btn_rows.append([btn("🌸 Back to Menu", home_cb, "primary")])

        await send_or_edit_sakura(
            event,
            sakura_header("𝗢𝗪𝗡𝗘𝗥 𝗛𝗜𝗘𝗥𝗔𝗥𝗖𝗛𝗬")
            + f"👑 **Main Owner ID:** `{owners.get('main_owner')}`\n\n🛡️ **Co-Owners:**\n{co_str}",
            btn_rows,
        )

    elif data == "owner_add":
        if not is_main_owner(user_id):
            return
        await api_answer_callback(cb_id)
        USER_STATES[key] = {"step": "add_owner", "data": {}}
        await send_or_edit_sakura(
            event,
            sakura_header("𝗔𝗗𝗗 𝗖𝗢-𝗢𝗪𝗡𝗘𝗥")
            + f"🆔 Send the numeric **Telegram User ID** to add as Co-Owner:{hint}",
            [[btn("❌ Cancel", "menu_owners", "danger")]],
        )

    elif data.startswith("owner_del_"):
        if not is_main_owner(user_id):
            return
        rem_id = int(data.split("_")[-1])
        owners = read_json(OWNERS_FILE)
        owners["co_owners"] = [int(x) for x in owners.get("co_owners", []) if int(x) != rem_id]
        write_json(OWNERS_FILE, owners)
        log_audit("REMOVE_OWNER", user_id, f"Removed Co-Owner ID: {rem_id}")
        event.data = b"menu_owners"
        await callback_router(event)


# ==============================================================================
# HTTP HEALTH & PORT BINDING SERVER (FOR RENDER / WEB SERVICES)
# ==============================================================================
async def start_render_health_server() -> None:
    port = int(os.environ.get("PORT", "10000"))

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            await reader.read(1024)
            body = (
                f"{BRAND} Userbot Handler Online | Workers: {count_running_workers()}"
            ).encode("utf-8")
            response = (
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("utf-8")
                + b"Connection: close\r\n\r\n"
                + body
            )
            writer.write(response)
            await writer.drain()
        except Exception:
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "0.0.0.0", port)
    print(f"🌐 Health server listening on 0.0.0.0:{port}")
    async with server:
        await server.serve_forever()


# ==============================================================================
# ENTRYPOINT (PORT SERVER + AUTO-BOOT WORKERS + BACKGROUND WATCHDOG)
# ==============================================================================
async def main() -> None:
    asyncio.create_task(start_render_health_server())
    print(f"🌸 Starting {BRAND} Userbot Handler...")
    await bot.start(bot_token=BOT_TOKEN)
    me = await bot.get_me()

    slots = read_json(SLOTS_FILE).get("slots", {})
    booted = 0
    for sid, info in sorted(slots.items(), key=lambda x: int(x[0])):
        if info.get("status") == "active":
            ok, _ = await start_slot_worker(sid)
            if ok:
                booted += 1

    asyncio.create_task(background_health_watchdog())
    print(f"✅ Online as @{me.username} (ID: {me.id}) | Auto-Hosted Workers: {booted}")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    LOOP.run_until_complete(main())
