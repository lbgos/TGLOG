import json
import os
import asyncio
import logging
import sqlite3
import base64
from datetime import datetime

import qrcode

from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.functions.auth import ExportLoginTokenRequest, AcceptLoginTokenRequest
from telethon.tl.types import (
    auth,
    MessageMediaPhoto,
    MessageMediaDocument,
    MessageMediaWebPage,
    MessageMediaUnsupported,
    Document,
    DocumentAttributeVideo,
    DocumentAttributeAnimated,
    UpdateReadMessagesContents,
    User,
    Channel,
    Chat,
    Photo,
)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
MEDIA_DIR = os.path.join(DATA_DIR, "media_cache")
os.makedirs(MEDIA_DIR, exist_ok=True)

# Config: check data dir first, fall back to script dir
_config_path = os.path.join(DATA_DIR, "config.json")
if not os.path.exists(_config_path):
    _config_path = os.path.join(SCRIPT_DIR, "config.json")

with open(_config_path, "r", encoding="utf-8") as f:
    cfg = json.load(f)

API_ID = cfg["api_id"]
API_HASH = cfg["api_hash"]
PHONE = cfg["phone"]
LOG_CHANNEL = cfg["log_channel_id"]

ENABLED = cfg.get("enabled", True)
LISTEN_OUTGOING = cfg.get("listen_outgoing", False)
LOG_DELETED = cfg.get("log_deleted", True)
LOG_EDITED = cfg.get("log_edited", True)
LOG_TEXT = cfg.get("log_text", True)
LOG_PHOTOS = cfg.get("log_photos", True)
LOG_VIDEOS = cfg.get("log_videos", True)
LOG_DOCUMENTS = cfg.get("log_documents", True)
LOG_SELF_DESTRUCT = cfg.get("log_self_destruct", True)
LOG_LINKS = cfg.get("log_links", True)

LOG_GROUPS = cfg.get("log_groups", True)
LOG_CHANNELS = cfg.get("log_channels", True)
LOG_PRIVATE = cfg.get("log_private", True)
MAX_VIDEO_SIZE_MB = cfg.get("max_video_size_mb", 10)
MAX_PHOTO_SIZE_MB = cfg.get("max_photo_size_mb", 10)

MODE = cfg.get("mode", "all")
WHITELIST = set(cfg.get("whitelist", []))
BLACKLIST = set(cfg.get("blacklist", []))
IGNORED_IDS = set(cfg.get("ignored_ids", []))
IGNORED_IDS.add(LOG_CHANNEL)
CACHE_SIZE = cfg.get("cache_size", 50000)
LOGIN_METHOD = cfg.get("login_method", "phone")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("tg-logger")

client = TelegramClient(os.path.join(DATA_DIR, "session"), API_ID, API_HASH)

# ---------------------------------------------------------------------------
# SQLite persistent message cache
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(DATA_DIR, "cache.db")
db = sqlite3.connect(DB_PATH)
db.execute("PRAGMA journal_mode=WAL")
db.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        chat_id INTEGER,
        msg_id INTEGER,
        sender_name TEXT,
        chat_name TEXT,
        text TEXT,
        media_type TEXT,
        media_path TEXT,
        media_size INTEGER DEFAULT 0,
        edit_date TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (chat_id, msg_id)
    )
""")
db.execute("""
    CREATE INDEX IF NOT EXISTS idx_msg_id ON messages (msg_id)
""")
db.commit()


def db_store(chat_id, msg_id, sender_name, chat_name, text, mtype, media_path=None, media_size=0, edit_date=None):
    db.execute(
        "INSERT OR REPLACE INTO messages (chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size, edit_date) VALUES (?,?,?,?,?,?,?,?,?)",
        (chat_id, msg_id, sender_name, chat_name, text, mtype, media_path, media_size, str(edit_date) if edit_date else None),
    )
    db.commit()
    # Trim old entries
    count = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if count > CACHE_SIZE:
        overflow = count - CACHE_SIZE
        # Remove cached media files of the rows we're about to drop, so the
        # media_cache directory doesn't leak files for trimmed entries.
        rows = db.execute(
            "SELECT rowid, media_path FROM messages ORDER BY timestamp ASC LIMIT ?",
            (overflow,),
        ).fetchall()
        for _rowid, media_path in rows:
            if media_path:
                try:
                    os.remove(media_path)
                except OSError:
                    pass
        db.execute(
            "DELETE FROM messages WHERE rowid IN (%s)" % ",".join("?" * len(rows)),
            [r[0] for r in rows],
        )
        db.commit()


def db_get(chat_id, msg_id):
    return db.execute(
        "SELECT chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size, edit_date FROM messages WHERE chat_id=? AND msg_id=?",
        (chat_id, msg_id),
    ).fetchone()


def db_get_by_msg_id(msg_id):
    """Fallback search by msg_id only (for deletions where chat_id is missing).

    msg_id is only unique per chat, so several chats can share one. If more than
    one row matches we can't tell which chat it belongs to, so we return None
    rather than risk attributing (and leaking) the wrong chat's message.
    """
    rows = db.execute(
        "SELECT chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size, edit_date FROM messages WHERE msg_id=?",
        (msg_id,),
    ).fetchall()
    if len(rows) == 1:
        return rows[0]
    if len(rows) > 1:
        log.warning("Ambiguous msg_id %s matches %d chats; skipping fallback", msg_id, len(rows))
    return None


def db_remove(chat_id, msg_id):
    row = db_get(chat_id, msg_id)
    if row and row[6]:  # media_path
        try:
            os.remove(row[6])
        except OSError:
            pass
    db.execute("DELETE FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id))
    db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Cache the resolved kind ("private" | "group" | "channel") per chat_id so we
# don't hit get_entity on every single message when type filters are enabled.
_chat_kind_cache = {}


async def _chat_kind(chat_id):
    kind = _chat_kind_cache.get(chat_id)
    if kind is not None:
        return kind
    try:
        entity = await client.get_entity(chat_id)
        if isinstance(entity, User):
            kind = "private"
        elif isinstance(entity, Chat):
            kind = "group"
        elif isinstance(entity, Channel):
            kind = "group" if (entity.megagroup or entity.gigagroup) else "channel"
        else:
            kind = "unknown"
    except Exception:
        kind = "unknown"
    _chat_kind_cache[chat_id] = kind
    return kind


async def should_log_chat(chat_id):
    if not ENABLED:
        return False
    if chat_id in IGNORED_IDS:
        return False
    if MODE == "whitelist" and chat_id not in WHITELIST:
        return False
    if MODE == "blacklist" and chat_id in BLACKLIST:
        return False

    if not LOG_GROUPS or not LOG_CHANNELS or not LOG_PRIVATE:
        kind = await _chat_kind(chat_id)
        if kind == "private" and not LOG_PRIVATE:
            return False
        if kind == "group" and not LOG_GROUPS:
            return False
        if kind == "channel" and not LOG_CHANNELS:
            return False

    return True


def is_self_destruct(msg):
    """Check all known TTL fields across Telegram API versions."""
    if getattr(msg, "ttl_period", None):
        return True
    media = msg.media
    if media is None:
        return False
    if getattr(media, "ttl_seconds", None):
        return True
    # Some versions put ttl_period on media itself
    if getattr(media, "ttl_period", None):
        return True
    return False


def media_type(msg):
    media = msg.media
    if media is None:
        return "text" if msg.raw_text else None
    if is_self_destruct(msg):
        return "self_destruct"
    if isinstance(media, MessageMediaPhoto):
        return "photo"
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if isinstance(doc, Document):
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    return "video"
                if isinstance(attr, DocumentAttributeAnimated):
                    return "document"
            return "document"
    if isinstance(media, MessageMediaWebPage):
        return "link"
    return "document"


def get_media_size(msg):
    """Get media size in bytes."""
    media = msg.media
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if isinstance(doc, Document):
            return doc.size
    if isinstance(media, MessageMediaPhoto):
        photo = media.photo
        if isinstance(photo, Photo) and photo.sizes:
            # Estimate from largest size
            for size in reversed(photo.sizes):
                if hasattr(size, "size"):
                    return size.size
            return 0
    return 0


def should_log_media(mtype):
    if mtype is None:
        return False
    return {
        "text": LOG_TEXT, "photo": LOG_PHOTOS, "video": LOG_VIDEOS,
        "document": LOG_DOCUMENTS, "self_destruct": LOG_SELF_DESTRUCT, "link": LOG_LINKS,
    }.get(mtype, True)


def media_within_size_limit(msg, mtype):
    """Check if media is within the configured size limit."""
    size = get_media_size(msg)
    if mtype == "video":
        return size <= MAX_VIDEO_SIZE_MB * 1024 * 1024
    if mtype == "photo":
        return size <= MAX_PHOTO_SIZE_MB * 1024 * 1024
    return True


async def get_sender_name(msg):
    sender = await msg.get_sender()
    if not sender:
        return "Unknown"
    parts = []
    if getattr(sender, "first_name", None):
        parts.append(sender.first_name)
    if getattr(sender, "last_name", None):
        parts.append(sender.last_name)
    if parts:
        return " ".join(parts)
    return getattr(sender, "title", None) or getattr(sender, "username", None) or "Unknown"


async def get_chat_name(msg):
    chat = await msg.get_chat()
    if not chat:
        return "Unknown"
    if getattr(chat, "title", None):
        return chat.title
    parts = []
    if getattr(chat, "first_name", None):
        parts.append(chat.first_name)
    if getattr(chat, "last_name", None):
        parts.append(chat.last_name)
    return " ".join(parts) if parts else "PM"


async def cache_media(msg, mtype):
    """Download and cache media to disk if within size limit. Returns path or None."""
    if mtype in ("text", "link", None):
        return None
    if not media_within_size_limit(msg, mtype):
        return None

    try:
        path = await client.download_media(msg, file=MEDIA_DIR)
        if path:
            log.info("Cached media %s/%s -> %s", msg.chat_id, msg.id, os.path.basename(path))
        return path
    except Exception as e:
        log.warning("Failed to cache media: %s", e)
        return None


async def send_log_from_db(header, row, extra_text=None):
    """Send log entry using data from the DB row."""
    # row: (chat_id, msg_id, sender_name, chat_name, text, media_type, media_path, media_size)
    text = header
    if extra_text:
        text += extra_text

    media_path = row[6]
    mtype = row[5]

    if media_path and os.path.exists(media_path):
        try:
            await client.send_file(LOG_CHANNEL, media_path, caption=text, parse_mode="md")
            return
        except Exception as e:
            log.warning("Could not send cached media: %s", e)
            text += "\n_(media could not be forwarded)_"

    # Media existed but was too large or not cached
    if mtype in ("video", "photo") and not media_path:
        size_mb = row[7] / (1024 * 1024) if row[7] else 0
        limit = MAX_VIDEO_SIZE_MB if mtype == "video" else MAX_PHOTO_SIZE_MB
        if size_mb > limit:
            text += f"\n_({mtype} {size_mb:.1f}MB, not saved)_"
        elif size_mb > 0:
            text += f"\n_({mtype} {size_mb:.1f}MB, not cached)_"

    await client.send_message(LOG_CHANNEL, text, parse_mode="md", link_preview=False)


async def send_log_from_msg(header, msg, extra_text=None):
    """Send log entry using a live Telethon message object (for edits)."""
    text = header
    if extra_text:
        text += extra_text

    media = msg.media if msg else None

    if isinstance(media, str):
        await client.send_file(LOG_CHANNEL, media, caption=text, parse_mode="md")
        return

    if media and not isinstance(media, (MessageMediaWebPage, MessageMediaUnsupported)):
        mtype = media_type(msg)
        if not media_within_size_limit(msg, mtype):
            size_mb = get_media_size(msg) / (1024 * 1024)
            text += f"\n_({mtype} {size_mb:.1f}MB, not saved)_"
            await client.send_message(LOG_CHANNEL, text, parse_mode="md", link_preview=False)
            return

        try:
            dl_dir = os.path.join(DATA_DIR, "tmp")
            os.makedirs(dl_dir, exist_ok=True)
            path = await client.download_media(msg, file=dl_dir)
            if path:
                await client.send_file(LOG_CHANNEL, path, caption=text, parse_mode="md")
                try:
                    os.remove(path)
                except OSError:
                    pass
                return
        except Exception as e:
            log.warning("Could not forward media: %s", e)
            text += "\n_(media could not be forwarded)_"

    await client.send_message(LOG_CHANNEL, text, parse_mode="md", link_preview=False)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def on_new_message(event):
    msg = event.message
    if not await should_log_chat(msg.chat_id):
        return

    mtype = media_type(msg)
    sender = await get_sender_name(msg)
    chat_name = await get_chat_name(msg)

    # Cache media to disk
    media_path = None
    media_size = get_media_size(msg)
    if mtype == "self_destruct" and LOG_SELF_DESTRUCT:
        # Immediately download and send to log — don't wait for deletion
        try:
            media_path = await client.download_media(msg, file=MEDIA_DIR)
            if media_path:
                log.info("Saved self-destruct media %s/%s -> %s", msg.chat_id, msg.id, os.path.basename(media_path))
            else:
                log.warning("Self-destruct %s/%s: download returned None", msg.chat_id, msg.id)
        except Exception as e:
            log.warning("Self-destruct %s/%s download failed: %s", msg.chat_id, msg.id, e)

        # Send to log channel right now
        ttl = getattr(msg, "ttl_period", None) or getattr(getattr(msg, "media", None), "ttl_seconds", None) or "?"
        header = f"\u2702\ufe0f **Self-destruct from:** {sender}\nin **{chat_name}**  `{msg.chat_id}`\n"
        body = f"\n_Self-destruct message (TTL: {ttl}s)_\n"
        if msg.raw_text:
            body += f"\n**Text:**\n{msg.raw_text}\n"
        if media_path and os.path.exists(media_path):
            try:
                await client.send_file(LOG_CHANNEL, media_path, caption=header + body, parse_mode="md")
            except Exception:
                body += "\n_(media could not be forwarded)_"
                await client.send_message(LOG_CHANNEL, header + body, parse_mode="md", link_preview=False)
        else:
            body += "\n_(media could not be saved)_"
            await client.send_message(LOG_CHANNEL, header + body, parse_mode="md", link_preview=False)

    elif mtype in ("photo", "video", "document"):
        media_path = await cache_media(msg, mtype)

    db_store(msg.chat_id, msg.id, sender, chat_name, msg.raw_text, mtype, media_path, media_size, msg.edit_date)


async def on_edited(event):
    if not LOG_EDITED:
        return
    msg = event.message
    if not await should_log_chat(msg.chat_id):
        return
    mtype = media_type(msg)
    if not should_log_media(mtype) and not msg.raw_text:
        return

    # Only log if we have a previous version — skip first-time channel posts
    old = db_get(msg.chat_id, msg.id)
    if old is None:
        # First time seeing this message, just cache it
        sender = await get_sender_name(msg)
        chat_name = await get_chat_name(msg)
        media_path = None
        media_size = get_media_size(msg)
        if mtype in ("photo", "video", "document"):
            media_path = await cache_media(msg, mtype)
        db_store(msg.chat_id, msg.id, sender, chat_name, msg.raw_text, mtype, media_path, media_size, msg.edit_date)
        return

    # Skip if edit_date hasn't changed (reactions, pin, etc.)
    current_edit = str(msg.edit_date) if msg.edit_date else None
    stored_edit = old[8]  # edit_date column
    if current_edit == stored_edit:
        return

    # Skip if text hasn't actually changed
    if old[4] == msg.raw_text:
        # Update stored edit_date so we don't re-check
        db.execute("UPDATE messages SET edit_date=? WHERE chat_id=? AND msg_id=?", (current_edit, msg.chat_id, msg.id))
        db.commit()
        return

    sender = await get_sender_name(msg)
    chat_name = await get_chat_name(msg)

    header = f"\u270f\ufe0f **Edited message from:** {sender}\nin **{chat_name}**  `{msg.chat_id}`\n"
    body = ""
    if old[4]:  # old text
        body += f"\n**Original message:**\n{old[4]}\n"
    if msg.raw_text:
        body += f"\n**Edited message:**\n{msg.raw_text}\n"

    await send_log_from_msg(header, msg, body)

    # Update cache with new version
    media_path = None
    media_size = get_media_size(msg)
    if mtype in ("photo", "video", "document"):
        media_path = await cache_media(msg, mtype)
    db_store(msg.chat_id, msg.id, sender, chat_name, msg.raw_text, mtype, media_path, media_size, msg.edit_date)


async def on_deleted(event):
    if not LOG_DELETED:
        return
    for msg_id in event.deleted_ids:
        chat_id = event.chat_id or 0

        row = db_get(chat_id, msg_id) if chat_id else None
        if row is None:
            row = db_get_by_msg_id(msg_id)
            if row:
                chat_id = row[0]

        # Not in DB — try to fetch from Telegram (old messages from before bot started)
        if row is None and chat_id:
            try:
                msgs = await client.get_messages(chat_id, ids=msg_id)
                msg = msgs if not isinstance(msgs, list) else (msgs[0] if msgs else None)
                if msg and msg.id:
                    sender = await get_sender_name(msg)
                    chat_name = await get_chat_name(msg)
                    mtype = media_type(msg)

                    if not await should_log_chat(chat_id):
                        continue
                    if not should_log_media(mtype):
                        continue

                    header = f"\u2702\ufe0f **Deleted message from:** {sender}\nin **{chat_name}**  `{chat_id}`\n"
                    body = ""
                    if msg.raw_text:
                        body += f"\n**Original message:**\n{msg.raw_text}\n"
                    await send_log_from_msg(header, msg, body)
                    continue
            except Exception as e:
                log.warning("Could not fetch deleted msg %s from chat %s: %s", msg_id, chat_id, e)

        if row is None:
            continue
        if not await should_log_chat(chat_id):
            continue

        mtype = row[5]
        if not should_log_media(mtype):
            continue

        sender = row[2] or "Unknown"
        chat_name = row[3] or "Unknown"
        header = f"\u2702\ufe0f **Deleted message from:** {sender}\nin **{chat_name}**  `{chat_id}`\n"

        body = ""
        if row[4]:  # text
            body += f"\n**Original message:**\n{row[4]}\n"

        await send_log_from_db(header, row, body)
        db_remove(chat_id, msg_id)


async def on_read_contents(event):
    """Handle self-destruct messages being read/opened."""
    if not LOG_SELF_DESTRUCT:
        return
    update = event.original_update
    if not isinstance(update, UpdateReadMessagesContents):
        return
    for msg_id in update.messages:
        row = db_get_by_msg_id(msg_id)
        if row is None:
            continue
        chat_id = row[0]
        media_path = row[6]

        sender = row[2] or "Unknown"
        chat_name = row[3] or "Unknown"
        header = f"\u2702\ufe0f **Self-destruct from:** {sender}\nin **{chat_name}**  `{chat_id}`\n"
        body = "\n_Self-destruct message viewed_\n"
        if row[4]:
            body += f"\n**Text:**\n{row[4]}\n"
        if not media_path or not os.path.exists(media_path):
            body += "\n_(media could not be saved — Telegram blocks TTL media downloads)_\n"

        await send_log_from_db(header, row, body)
        db_remove(chat_id, msg_id)




# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------
async def login_phone():
    await client.start(
        phone=PHONE,
        password=lambda: input("Enter 2FA password: "),
    )


async def login_qr():
    await client.connect()
    if await client.is_user_authorized():
        return

    while True:
        result = await client(ExportLoginTokenRequest(
            api_id=API_ID,
            api_hash=API_HASH,
            except_ids=[],
        ))

        if isinstance(result, auth.LoginTokenSuccess):
            break

        if isinstance(result, auth.LoginTokenMigrateTo):
            await client._switch_dc(result.dc_id)
            result = await client(ExportLoginTokenRequest(
                api_id=API_ID,
                api_hash=API_HASH,
                except_ids=[],
            ))
            if isinstance(result, auth.LoginTokenSuccess):
                break

        token_b64 = base64.urlsafe_b64encode(result.token).decode("ascii")
        url = f"tg://login?token={token_b64}"

        qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(url)
        qr.make(fit=True)

        print("\n\033[2J\033[H")
        print("Scan this QR code in Telegram > Settings > Devices > Link Desktop Device\n")
        qr.print_ascii(invert=True)
        print()

        try:
            await asyncio.sleep(5)
            try:
                result = await client(ExportLoginTokenRequest(
                    api_id=API_ID,
                    api_hash=API_HASH,
                    except_ids=[],
                ))
                if isinstance(result, auth.LoginTokenSuccess):
                    break
            except SessionPasswordNeededError:
                break
            except Exception:
                pass
        except asyncio.CancelledError:
            raise

    if not await client.is_user_authorized():
        from telethon.tl.functions.account import GetPasswordRequest
        from telethon.tl.functions.auth import CheckPasswordRequest
        from telethon.password import compute_check
        password = input("Enter 2FA password: ")
        pwd = await client(GetPasswordRequest())
        await client(CheckPasswordRequest(compute_check(pwd, password)))

    print("QR login successful!")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    if LOGIN_METHOD == "qr":
        await login_qr()
    else:
        await login_phone()

    me = await client.get_me()
    log.info("Logged in as %s (id=%s)", me.first_name, me.id)

    try:
        entity = await client.get_entity(LOG_CHANNEL)
        log.info("Log channel: %s", getattr(entity, "title", entity))
    except Exception as e:
        log.error("Cannot access log channel %s: %s", LOG_CHANNEL, e)
        return

    cached = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    log.info("Persistent cache: %d messages", cached)

    # Always cache both incoming AND outgoing so we can log any deletion
    client.add_event_handler(on_new_message, events.NewMessage(incoming=True, outgoing=True))
    client.add_event_handler(on_edited, events.MessageEdited(incoming=True, outgoing=True))
    client.add_event_handler(on_deleted, events.MessageDeleted())
    client.add_event_handler(on_read_contents, events.Raw(UpdateReadMessagesContents))

    log.info("Mode: %s | Enabled: %s | Listening...", MODE, ENABLED)
    await client.run_until_disconnected()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        db.close()
