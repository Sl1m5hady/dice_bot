"""Send the durable chat outbox through the existing Telegram bot."""

from html import escape
from io import BytesIO
import logging
import sqlite3
from time import time

from telebot.apihelper import ApiTelegramException
from telebot.types import InputFile, ReplyParameters

from service import MAX_REPLY_LENGTH, make_roll_document
from telegram_replies import GROUP_INTERVAL


LOGGER = logging.getLogger(__name__)


def deliver_once(bot, store):
    entry = store.claim_delivery()
    if entry is None:
        return False
    identity = entry["id"]
    group_id = entry["group_id"]
    try:
        if entry["total"] is None:
            message = bot.send_message(group_id, f'{entry["name"]} · веб\n{entry["text"]}', timeout=20)
        else:
            message = _deliver_roll(bot, group_id, entry)
    except ApiTelegramException as error:
        if error.error_code == 429:
            parameters = error.result_json.get("parameters") or {}
            delay = parameters.get("retry_after")
            retry_at = time() + (max(delay, GROUP_INTERVAL) if isinstance(delay, int) else GROUP_INTERVAL)
            store.finish_delivery(identity, "queued", retry_at=retry_at)
        else:
            store.finish_delivery(identity, "failed")
            LOGGER.warning("Telegram rejected chat entry %s (code %s).", identity, error.error_code)
    except Exception as error:
        # A timeout can happen after Telegram accepted the message. Do not resend blindly.
        store.finish_delivery(identity, "uncertain")
        LOGGER.warning("Delivery of chat entry %s could not be confirmed (%s).", identity, type(error).__name__)
    else:
        store.finish_delivery(identity, "sent", telegram_id=message.message_id)
    return True


def _deliver_roll(bot, group_id, entry):
    from_web = entry["source"] == "web"
    result = entry["rendered"]
    prefix = f'{entry["name"]} · веб'
    original = escape(entry["text"])
    text = f"{escape(prefix)}\n{original}\n{result}" if from_web else result
    reply = ReplyParameters(entry["incoming_message_id"]) if not from_web else None
    thread_id = entry["incoming_thread_id"]
    if len(text.encode("utf-16-le")) // 2 <= MAX_REPLY_LENGTH:
        return bot.send_message(
            group_id, text, parse_mode="HTML", reply_parameters=reply, message_thread_id=thread_id, timeout=20,
        )

    body = text if from_web else f"{original}\n\n{result}"
    caption = f'Итог броска: {entry["total"]}'
    if from_web:
        caption = f"{prefix}\n{caption}"
    with BytesIO(make_roll_document(body).content) as file:
        return bot.send_document(
            group_id, InputFile(file, file_name="roll-results.html"),
            caption=caption, reply_parameters=reply, message_thread_id=thread_id, timeout=20,
        )


def run_bridge(bot, store, stop):
    store.recover_delivery()
    while not stop.is_set():
        try:
            sent = store.active_target() is not None and deliver_once(bot, store)
        except sqlite3.Error:
            LOGGER.exception("The shared chat database is unavailable.")
            sent = False
        stop.wait(GROUP_INTERVAL if sent else 0.5)
