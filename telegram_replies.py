"""Deliver replies in chat order without blocking the polling handlers."""

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from logging import getLogger
from threading import Condition, Thread
from time import monotonic

from telebot import TeleBot
from telebot.apihelper import ApiTelegramException
from telebot.types import Message


CHAT_INTERVAL = 1.0
GROUP_INTERVAL = 3.1
MAX_PENDING_PER_CHAT = 100
BUSY_REPLY = "В чате накопилось много бросков. Попробуй чуть позже."
LOGGER = getLogger(__name__)


@dataclass
class ChatOutbox:
    interval: float
    pending: deque[tuple[Message, str]] = field(default_factory=deque)
    ready_at: float = 0.0
    busy_notice_queued: bool = False


class ReplySender:
    def __init__(self, bot: TeleBot, *, threaded: bool):
        self.bot = bot
        self.threaded = threaded
        self.chats: dict[int, ChatOutbox] = {}
        self.condition = Condition()
        self.running = False

    def submit(self, message: Message, replies: Sequence[str]) -> None:
        if not replies:
            return

        interval = GROUP_INTERVAL if message.chat.type in {"group", "supergroup"} else CHAT_INTERVAL
        with self.condition:
            chat = self.chats.setdefault(message.chat.id, ChatOutbox(interval))
            if len(chat.pending) + len(replies) <= MAX_PENDING_PER_CHAT:
                chat.pending.extend((message, text) for text in replies)
            elif not chat.busy_notice_queued:
                chat.pending.append((message, BUSY_REPLY))
                chat.busy_notice_queued = True
            self.condition.notify()
            if self.running:
                return
            self.running = True

        if self.threaded:
            Thread(target=self._run, name="telegram-replies", daemon=True).start()
        else:
            self._run()

    def _next_chat(self) -> ChatOutbox | None:
        # Called with the condition held. Other chats can run during a cooldown.
        while True:
            now = monotonic()
            self.chats = {
                chat_id: chat for chat_id, chat in self.chats.items() if chat.pending or chat.ready_at > now
            }
            chat = min(
                (chat for chat in self.chats.values() if chat.pending),
                key=lambda chat: chat.ready_at,
                default=None,
            )
            if chat is None:
                if not self.threaded or not self.chats:
                    self.running = False
                    return None
                self.condition.wait(min(chat.ready_at for chat in self.chats.values()) - now)
            elif chat.ready_at <= now:
                return chat
            else:
                self.condition.wait(chat.ready_at - now)

    def _run(self) -> None:
        while True:
            with self.condition:
                chat = self._next_chat()
                if chat is None:
                    return
                message, text = chat.pending[0]

            retry_after = self._send(message, text, chat.interval)
            with self.condition:
                if retry_after is None:
                    chat.pending.popleft()
                    if text == BUSY_REPLY:
                        chat.busy_notice_queued = False
                chat.ready_at = monotonic() + (chat.interval if retry_after is None else retry_after)

    def _send(self, message: Message, text: str, interval: float) -> float | None:
        thread_id = message.message_thread_id if message.is_topic_message else None
        try:
            self.bot.reply_to(message, text, parse_mode="HTML", message_thread_id=thread_id)
        except ApiTelegramException as error:
            if error.error_code == 429:
                parameters = error.result_json.get("parameters") or {}
                retry_after = parameters.get("retry_after")
                return max(interval, retry_after) if isinstance(retry_after, int) else interval
            LOGGER.warning("Telegram rejected a reply to chat %s (code %s).", message.chat.id, error.error_code)
        except Exception as error:
            LOGGER.warning("Failed to send a reply to chat %s (%s).", message.chat.id, type(error).__name__)
        return None
