#!/usr/bin/env python3
# bot.py

import os
import threading
import logging
import asyncio

from flask import Flask, request
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import Dispatcher, CommandHandler, CallbackQueryHandler, CallbackContext

from telethon import TelegramClient
from telethon.errors import TelegramError

from hianimez_scraper import (
    search_anime,
    get_episodes_list,
    extract_episode_stream_and_subtitle,
)
from utils import (
    download_and_rename_subtitle,
    download_and_rename_video,
    transcode_to_telegram_friendly,
)

# ——————————————————————————————————————————————————————————————
# 1) Load environment variables
# ——————————————————————————————————————————————————————————————
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is not set")

KOYEB_APP_URL = os.getenv("KOYEB_APP_URL")
if not KOYEB_APP_URL:
    raise RuntimeError(
        "KOYEB_APP_URL environment variable is not set. It must be your bot’s public HTTPS URL (no trailing slash)."
    )

ANIWATCH_API_BASE = os.getenv("ANIWATCH_API_BASE")
if not ANIWATCH_API_BASE:
    raise RuntimeError(
        "ANIWATCH_API_BASE environment variable is not set. It should be your AniWatch API URL."
    )

TELETHON_API_ID = os.getenv("TELETHON_API_ID")
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH")
if not TELETHON_API_ID or not TELETHON_API_HASH:
    raise RuntimeError(
        "TELETHON_API_ID and TELETHON_API_HASH environment variables must be set for Telethon."
    )

# ——————————————————————————————————————————————————————————————
# 2) Initialize Bot API + Dispatcher
# ——————————————————————————————————————————————————————————————
bot = Bot(token=BOT_TOKEN)
dispatcher = Dispatcher(bot, None, workers=4, use_context=True)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ——————————————————————————————————————————————————————————————
# 3) In‐memory caches
# ——————————————————————————————————————————————————————————————
search_cache = {}    # chat_id → [ (title, slug), … ]
episode_cache = {}   # chat_id → [ (ep_num, episode_id), … ]

# ——————————————————————————————————————————————————————————————
# 4) /start handler
# ——————————————————————————————————————————————————————————————
def start(update: Update, context: CallbackContext):
    update.message.reply_text(
        "👋 Hello! I can help you search for anime on hianimez.to and\n"
        " extract the SUB-HD2 Video (as an MP4) + English subtitles.\n\n"
        "Use /search <anime name> to begin."
    )


# ——————————————————————————————————————————————————————————————
# 5) /search handler
# ——————————————————————————————————————————————————————————————
def search_command(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id

    if len(context.args) == 0:
        update.message.reply_text("Please provide an anime name. Example: /search Naruto")
        return

    query_text = " ".join(context.args).strip()
    msg = update.message.reply_text(f"🔍 Searching for \"{query_text}\"…")

    try:
        results = search_anime(query_text)
    except Exception as e:
        logger.error(f"Search error: {e}", exc_info=True)
        msg.edit_text("❌ Search error; please try again later.")
        return

    if not results:
        msg.edit_text(f"No anime found matching \"{query_text}\".")
        return

    # Store (title, slug) in search_cache
    search_cache[chat_id] = [(title, slug) for title, anime_url, slug in results]

    buttons = []
    for idx, (title, slug) in enumerate(search_cache[chat_id]):
        buttons.append([InlineKeyboardButton(title, callback_data=f"anime_idx:{idx}")])

    reply_markup = InlineKeyboardMarkup(buttons)
    try:
        msg.edit_text("Select the anime you want:", reply_markup=reply_markup)
    except Exception:
        # If the text/markup is identical, ignore the “BadRequest: message is not modified”
        pass


# ——————————————————————————————————————————————————————————————
# 6) Callback when user taps an anime button (anime_idx)
# ——————————————————————————————————————————————————————————————
def anime_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    chat_id = query.message.chat.id

    # Acknowledge immediately
    try:
        query.answer()
    except Exception:
        pass

    data = query.data  # e.g. "anime_idx:3"
    try:
        _, idx_str = data.split(":", maxsplit=1)
        idx = int(idx_str)
    except Exception:
        try:
            query.edit_message_text("❌ Internal error: invalid anime selection.")
        except Exception:
            pass
        return

    anime_list = search_cache.get(chat_id, [])
    if idx < 0 or idx >= len(anime_list):
        try:
            query.edit_message_text("❌ Internal error: anime index out of range.")
        except Exception:
            pass
        return

    title, slug = anime_list[idx]
    anime_url = f"https://hianimez.to/watch/{slug}"

    # Let the user know we’re fetching episodes
    try:
        query.edit_message_text(
            f"🔍 Fetching episodes for *{title}*…", parse_mode="MarkdownV2"
        )
    except Exception:
        pass

    try:
        episodes = get_episodes_list(anime_url)
    except Exception as e:
        logger.error(f"Error fetching episodes: {e}", exc_info=True)
        try:
            query.edit_message_text("❌ Failed to retrieve episodes for that anime.")
        except Exception:
            pass
        return

    if not episodes:
        try:
            query.edit_message_text("No episodes found for that anime.")
        except Exception:
            pass
        return

    # Store episodes in cache
    episode_cache[chat_id] = [(ep_num, ep_id) for ep_num, ep_id in episodes]

    # Create buttons: “Episode 1”, “Episode 2”, … + “Download All”
    buttons = []
    for i, (ep_num, ep_id) in enumerate(episode_cache[chat_id]):
        buttons.append([InlineKeyboardButton(f"Episode {ep_num}", callback_data=f"episode_idx:{i}")])
    buttons.append([InlineKeyboardButton("Download All", callback_data="episode_all")])

    reply_markup = InlineKeyboardMarkup(buttons)
    try:
        query.edit_message_text("Select an episode (or Download All):", reply_markup=reply_markup)
    except Exception:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# 7a) Callback when user taps a single episode button (episode_idx)
#     We spin off a background thread to handle the heavy work.
# ──────────────────────────────────────────────────────────────────────────────
def episode_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    chat_id = query.message.chat.id

    # Acknowledge immediately
    try:
        query.answer()
    except Exception:
        pass

    data = query.data  # e.g. "episode_idx:5"
    try:
        _, idx_str = data.split(":", maxsplit=1)
        idx = int(idx_str)
    except Exception:
        try:
            query.edit_message_text("❌ Invalid episode selection.")
        except Exception:
            pass
        return

    ep_list = episode_cache.get(chat_id, [])
    if idx < 0 or idx >= len(ep_list):
        try:
            query.edit_message_text("❌ Episode index out of range.")
        except Exception:
            pass
        return

    ep_num, episode_id = ep_list[idx]

    # Let the user know we queued their request
    try:
        query.edit_message_text(
            f"⏳ Episode {ep_num} queued for download… You will receive it shortly."
        )
    except Exception:
        pass

    # Start a background thread for the heavy work
    thread = threading.Thread(
        target=download_and_send_episode,
        args=(chat_id, ep_num, episode_id),
        daemon=True
    )
    thread.start()

    # Return immediately
    return


# ──────────────────────────────────────────────────────────────────────────────
# 7b) Callback when user taps “Download All” (episode_all)
# ──────────────────────────────────────────────────────────────────────────────
def episodes_all_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    chat_id = query.message.chat.id

    # Acknowledge right away
    try:
        query.answer()
    except Exception:
        pass

    ep_list = episode_cache.get(chat_id, [])
    if not ep_list:
        try:
            query.edit_message_text("❌ No episodes available to download.")
        except Exception:
            pass
        return

    # Inform user that all episodes are queued
    try:
        query.edit_message_text("⏳ Queued all episodes for download… You will receive them one by one.")
    except Exception:
        pass

    # Spawn a thread to handle “all episodes”
    thread = threading.Thread(
        target=download_and_send_all_episodes,
        args=(chat_id, ep_list),
        daemon=True
    )
    thread.start()

    return


# ──────────────────────────────────────────────────────────────────────────────
# 8) Helper: Telethon “bot” upload (MTProto) for large files
# ──────────────────────────────────────────────────────────────────────────────
async def telethon_send_file(chat_id: int, file_path: str, caption: str = None):
    """
    Uses Telethon (as a bot via bot_token) to send a single file (up to 2 GB) to `chat_id`.
    """
    client = TelegramClient("telethon_bot_session", int(TELETHON_API_ID), TELETHON_API_HASH)
    try:
        # Log in as a bot via MTProto—no interactive prompt.
        await client.start(bot_token=BOT_TOKEN)
        await client.send_file(entity=chat_id, file=file_path, caption=caption)
    except TelegramError as e:
        logger.error(f"[Telethon] Failed to send {file_path} to chat {chat_id}: {e}", exc_info=True)
    finally:
        await client.disconnect()


def send_file_via_telethon(chat_id: int, file_path: str, caption: str = None):
    """
    Synchronous wrapper that runs the `telethon_send_file()` coroutine.
    """
    try:
        asyncio.run(telethon_send_file(chat_id=chat_id, file_path=file_path, caption=caption))
    except Exception as e:
        logger.error(f"[Telethon sync] Exception while sending {file_path} to chat {chat_id}: {e}", exc_info=True)


# ──────────────────────────────────────────────────────────────────────────────
# 9) Background task for sending a single episode
# ──────────────────────────────────────────────────────────────────────────────
def download_and_send_episode(chat_id: int, ep_num: str, episode_id: str):
    """
    1) Extract HLS link + subtitle URL.
    2) Use ffmpeg to download the raw MP4.
    3) If MP4 ≤ 50 MB → Bot API; else → Telethon (MTProto).
    4) Always send subtitles via Bot API (they’re small).
    5) If anything fails, fallback to sending the HLS link + subtitle.
    """
    # ------------------------
    # (a) Step 1: Extract HLS + subtitle URL
    # ------------------------
    try:
        hls_link, subtitle_url = extract_episode_stream_and_subtitle(episode_id)
    except Exception as e:
        logger.error(f"[Thread] Error extracting Episode {ep_num}: {e}", exc_info=True)
        bot.send_message(chat_id, f"❌ Failed to extract data for Episode {ep_num}.")
        return

    if not hls_link:
        bot.send_message(chat_id, f"😔 Could not find a SUB-HD2 Video stream for Episode {ep_num}.")
        return

    # ------------------------
    # (b) Step 2: Download raw MP4 via ffmpeg
    # ------------------------
    try:
        raw_mp4 = download_and_rename_video(hls_link, ep_num, cache_dir="videos_cache")
    except Exception as e:
        logger.error(f"[Thread] Error downloading video (Episode {ep_num}): {e}", exc_info=True)
        bot.send_message(
            chat_id,
            f"⚠️ Failed to convert Episode {ep_num} to MP4. Here’s the HLS link instead:\n\n{hls_link}"
        )
        # Try sending subtitle if it exists
        if subtitle_url:
            try:
                local_vtt = download_and_rename_subtitle(subtitle_url, ep_num, cache_dir="subtitles_cache")
                bot.send_message(chat_id, f"✅ Subtitle downloaded as “Episode {ep_num}.vtt”.")
                with open(local_vtt, "rb") as f:
                    bot.send_document(
                        chat_id=chat_id,
                        document=InputFile(f, filename=f"Episode {ep_num}.vtt"),
                        caption=f"Here is the subtitle for Episode {ep_num}.",
                    )
                os.remove(local_vtt)
            except Exception as se:
                logger.error(f"[Thread] Error sending subtitle (Episode {ep_num}): {se}", exc_info=True)
                bot.send_message(chat_id, f"⚠️ Could not download/send subtitle for Episode {ep_num}.")
        return

    # ------------------------
    # (c) Step 3: Decide whether to send via Bot API (≤50 MB) or Telethon (MTProto)
    # ------------------------
    try:
        file_size = os.path.getsize(raw_mp4)
    except OSError:
        bot.send_message(chat_id, f"⚠️ Could not access the MP4 for Episode {ep_num}.")
        return

    if file_size <= 50 * 1024 * 1024:
        # ———————————————
        # (c.1) ≤50 MB: use Bot API
        # ———————————————
        bot.send_message(chat_id, f"✅ Episode {ep_num} is ready (≤50 MB). Sending via Bot API…")
        try:
            with open(raw_mp4, "rb") as vid_f:
                bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(vid_f, filename=os.path.basename(raw_mp4)),
                    caption=f"Episode {ep_num}.mp4 (SUB-HD2)."
                )
        except Exception as e:
            logger.error(f"[Thread] Error sending MP4 (Episode {ep_num}) via Bot API: {e}", exc_info=True)
            bot.send_message(
                chat_id,
                f"⚠️ Could not send Episode {ep_num}.mp4 via Bot API. Here’s the HLS link:\n\n{hls_link}"
            )
        finally:
            try:
                os.remove(raw_mp4)
            except OSError:
                pass

    else:
        # ———————————————
        # (c.2) >50 MB: use Telethon as a bot (MTProto)
        # ———————————————
        bot.send_message(chat_id, f"📦 Episode {ep_num} is >50 MB. Sending full quality via Telethon…")

        # No re-encode: telethon can handle up to 2 GB via MTProto as a bot
        thread = threading.Thread(
            target=send_file_via_telethon,
            args=(chat_id, raw_mp4, f"Episode {ep_num}.mp4 (Full quality)"),
            daemon=True
        )
        thread.start()

    # ------------------------
    # (d) Step 4: Always send subtitle (if present) via Bot API
    # ------------------------
    if not subtitle_url:
        bot.send_message(chat_id, "❗ No English subtitle (.vtt) found.")
        return

    try:
        local_vtt = download_and_rename_subtitle(subtitle_url, ep_num, cache_dir="subtitles_cache")
    except Exception as e:
        logger.error(f"[Thread] Error downloading subtitle (Episode {ep_num}): {e}", exc_info=True)
        bot.send_message(chat_id, f"⚠️ Found a subtitle URL, but failed to download for Episode {ep_num}.")
        return

    bot.send_message(chat_id, f"✅ Subtitle downloaded as “Episode {ep_num}.vtt”.")
    try:
        with open(local_vtt, "rb") as f:
            bot.send_document(
                chat_id=chat_id,
                document=InputFile(f, filename=f"Episode {ep_num}.vtt"),
                caption=f"Here is the subtitle for Episode {ep_num}.",
            )
    except Exception as e:
        logger.error(f"[Thread] Error sending subtitle (Episode {ep_num}): {e}", exc_info=True)
        bot.send_message(chat_id, f"⚠️ Could not send subtitle for Episode {ep_num}.")
    finally:
        try:
            os.remove(local_vtt)
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# 10) Background task for “Download All” episodes
# ──────────────────────────────────────────────────────────────────────────────
def download_and_send_all_episodes(chat_id: int, ep_list: list):
    """
    Loops through each (ep_num, episode_id):
      1) Extract HLS + subtitle
      2) Download raw MP4
      3) If ≤50 MB → Bot API; else → Telethon
      4) Send subtitle via Bot API
      5) If any step fails, fall back to HLS link + subtitle
    """
    for ep_num, episode_id in ep_list:
        # (a) Extract HLS + subtitle
        try:
            hls_link, subtitle_url = extract_episode_stream_and_subtitle(episode_id)
        except Exception as e:
            logger.error(f"[Thread] Error extracting Episode {ep_num}: {e}", exc_info=True)
            bot.send_message(chat_id, f"❌ Failed to extract data for Episode {ep_num}. Skipping.")
            continue

        if not hls_link:
            bot.send_message(chat_id, f"😔 Episode {ep_num}: No SUB-HD2 stream found. Skipping.")
            continue

        # (b) Download raw MP4
        try:
            raw_mp4 = download_and_rename_video(hls_link, ep_num, cache_dir="videos_cache")
        except Exception as e:
            logger.error(f"[Thread] Error downloading Episode {ep_num}: {e}", exc_info=True)
            bot.send_message(
                chat_id,
                f"⚠️ Could not convert Episode {ep_num} to MP4. Here’s the HLS link instead:\n\n{hls_link}"
            )
            # Send subtitle if present
            if subtitle_url:
                try:
                    local_vtt = download_and_rename_subtitle(subtitle_url, ep_num, cache_dir="subtitles_cache")
                    bot.send_message(chat_id, f"✅ Subtitle downloaded as “Episode {ep_num}.vtt”.")
                    bot.send_document(
                        chat_id=chat_id,
                        document=InputFile(open(local_vtt, "rb"), filename=f"Episode {ep_num}.vtt"),
                        caption=f"Here is the subtitle for Episode {ep_num}."
                    )
                    os.remove(local_vtt)
                except Exception as se:
                    logger.error(f"[Thread] Error sending subtitle (Episode {ep_num}): {se}", exc_info=True)
                    bot.send_message(chat_id, f"⚠️ Could not send subtitle for Episode {ep_num}.")
            continue

        # (c) If raw_mp4 is >50 MB → Telethon; else → Bot API
        try:
            file_size = os.path.getsize(raw_mp4)
        except OSError:
            bot.send_message(chat_id, f"⚠️ Could not access MP4 for Episode {ep_num}.")
            continue

        if file_size <= 50 * 1024 * 1024:
            # Use Bot API
            bot.send_message(chat_id, f"✅ Episode {ep_num} ≤50 MB. Sending via Bot API…")
            try:
                with open(raw_mp4, "rb") as vid_f:
                    bot.send_document(
                        chat_id=chat_id,
                        document=InputFile(vid_f, filename=os.path.basename(raw_mp4)),
                        caption=f"Episode {ep_num}.mp4 (SUB-HD2)."
                    )
            except Exception as e:
                logger.error(f"[Thread] Error sending via Bot API (Episode {ep_num}): {e}", exc_info=True)
                bot.send_message(chat_id, f"⚠️ Could not send Episode {ep_num}.mp4. Here’s the HLS link:\n\n{hls_link}")
            finally:
                try:
                    os.remove(raw_mp4)
                except OSError:
                    pass
        else:
            # Use Telethon
            bot.send_message(chat_id, f"📦 Episode {ep_num} >50 MB. Sending via Telethon…")
            thread = threading.Thread(
                target=send_file_via_telethon,
                args=(chat_id, raw_mp4, f"Episode {ep_num}.mp4 (Full quality)"),
                daemon=True
            )
            thread.start()

        # (d) Send subtitle
        if not subtitle_url:
            bot.send_message(chat_id, f"❗ No English subtitle found for Episode {ep_num}.")
            continue

        try:
            local_vtt = download_and_rename_subtitle(subtitle_url, ep_num, cache_dir="subtitles_cache")
        except Exception as e:
            logger.error(f"[Thread] Error downloading subtitle (Episode {ep_num}): {e}", exc_info=True)
            bot.send_message(chat_id, f"⚠️ Could not download subtitle for Episode {ep_num}.")
            continue

        bot.send_message(chat_id, f"✅ Subtitle downloaded as “Episode {ep_num}.vtt”.")
        try:
            with open(local_vtt, "rb") as sub_f:
                bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(sub_f, filename=f"Episode {ep_num}.vtt"),
                    caption=f"Here is the subtitle for Episode {ep_num}."
                )
        except Exception as e:
            logger.error(f"[Thread] Error sending subtitle (Episode {ep_num}): {e}", exc_info=True)
            bot.send_message(chat_id, f"⚠️ Could not send subtitle for Episode {ep_num}.")
        finally:
            try:
                os.remove(local_vtt)
            except OSError:
                pass


# ──────────────────────────────────────────────────────────────────────────────
# 11) Error handler
# ──────────────────────────────────────────────────────────────────────────────
def error_handler(update: object, context: CallbackContext):
    logger.error("Exception while handling an update:", exc_info=context.error)
    if isinstance(update, Update) and update.callback_query:
        try:
            update.callback_query.message.reply_text("⚠️ Oops, something went wrong.")
        except Exception:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# 12) Register handlers with the dispatcher
# ──────────────────────────────────────────────────────────────────────────────
dispatcher.add_handler(CommandHandler("start", start))
dispatcher.add_handler(CommandHandler("search", search_command))
dispatcher.add_handler(CallbackQueryHandler(anime_callback, pattern=r"^anime_idx:"))
dispatcher.add_handler(CallbackQueryHandler(episode_callback, pattern=r"^episode_idx:"))
dispatcher.add_handler(CallbackQueryHandler(episodes_all_callback, pattern=r"^episode_all$"))
dispatcher.add_error_handler(error_handler)


# ──────────────────────────────────────────────────────────────────────────────
# 13) Flask app for webhook + health check
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/webhook", methods=["POST"])
def webhook_handler():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    dispatcher.process_update(update)
    return "OK", 200

@app.route("/", methods=["GET"])
def health_check():
    return "OK", 200


# ──────────────────────────────────────────────────────────────────────────────
# 14) On startup, set Telegram webhook to <KOYEB_APP_URL>/webhook
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    webhook_url = f"{KOYEB_APP_URL}/webhook"
    try:
        bot.set_webhook(webhook_url)
        logger.info(f"Successfully set webhook to {webhook_url}")
    except Exception as ex:
        logger.error(f"Failed to set webhook: {ex}", exc_info=True)
        raise

    os.makedirs("subtitles_cache", exist_ok=True)
    os.makedirs("videos_cache", exist_ok=True)
    logger.info("Starting Flask server on port 8080…")
    app.run(host="0.0.0.0", port=8080)
