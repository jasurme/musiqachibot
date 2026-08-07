"""Shared test harness.

`bot` is a real aiogram Bot whose network layer is replaced by a fake that
CAPTURES every outgoing method (SendMessage, SendAudio, ...) and returns a
plausible bound Message, so handlers run end-to-end with zero network.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    Audio,
    CallbackQuery,
    Chat,
    Document,
    Message,
    MessageId,
    PhotoSize,
    Update,
    User,
    Video,
    VideoNote,
    Voice,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config
from bot import jobs
from bot.db.storage import Storage
from bot.handlers import (
    broadcast,
    media_recognize,
    results,
    round as round_handler,
    start,
    text_search,
    top_music,
    url_download,
)
from bot.middlewares.i18n import I18nMiddleware

FAKE_TOKEN = "123456:FAKEfakeFAKEfakeFAKEfakeFAKEfakeFAKE"
ALL_ROUTERS = (broadcast.router, top_music.router, round_handler.router, start.router, url_download.router,
               media_recognize.router, text_search.router, results.router)


def _now():
    return datetime.now(timezone.utc)


class Capture:
    def __init__(self):
        self.methods = []

    def by(self, name):
        return [m for m in self.methods if type(m).__name__ == name]

    def last(self, name):
        xs = self.by(name)
        return xs[-1] if xs else None

    def names(self):
        return [type(m).__name__ for m in self.methods]


@pytest.fixture
def cap():
    return Capture()


@pytest_asyncio.fixture
async def bot(cap):
    b = Bot(FAKE_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    counter = {"n": 0}

    async def fake_make_request(bot_, method, timeout=None):
        cap.methods.append(method)
        counter["n"] += 1
        n = counter["n"]
        name = type(method).__name__
        response_chat_id = getattr(method, "chat_id", 1)
        if not isinstance(response_chat_id, int):
            response_chat_id = 1
        if name in ("AnswerCallbackQuery", "DeleteMessage"):
            return True
        if name == "CopyMessage":
            return MessageId(message_id=n)
        if name == "SendAudio":
            msg = Message(message_id=n, date=_now(), chat=Chat(id=response_chat_id, type="private"),
                          audio=Audio(file_id=f"AUDIO_{n}", file_unique_id=f"u{n}", duration=1))
        elif name == "SendVideo":
            msg = Message(message_id=n, date=_now(), chat=Chat(id=response_chat_id, type="private"),
                          video=Video(file_id=f"VIDEO_{n}", file_unique_id=f"u{n}",
                                      width=1, height=1, duration=1))
        elif name == "SendVideoNote":
            msg = Message(
                message_id=n, date=_now(), chat=Chat(id=response_chat_id, type="private"),
                video_note=VideoNote(
                    file_id=f"NOTE_{n}", file_unique_id=f"nu{n}",
                    length=480, duration=1,
                ),
            )
        elif name == "SendPhoto":
            msg = Message(message_id=n, date=_now(), chat=Chat(id=response_chat_id, type="private"),
                          photo=[PhotoSize(file_id=f"PHOTO_{n}", file_unique_id=f"u{n}",
                                           width=1, height=1)])
        else:
            msg = Message(message_id=n, date=_now(), chat=Chat(id=response_chat_id, type="private"),
                          text=getattr(method, "text", None) or getattr(method, "caption", None))
        return msg.as_(bot_)

    b.session.make_request = fake_make_request
    yield b
    await b.session.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        bot_token="x", local_api_url=None, default_locale="en",
        download_dir=str(tmp_path), max_file_mb=50, max_input_mb=20,
        audd_token=None,
    )


@pytest_asyncio.fixture
async def storage():
    s = Storage(":memory:")
    await s.init()
    yield s
    await s.close()


@pytest.fixture(autouse=True)
def _clear_module_state():
    results._SESS.clear()
    url_download._PENDING.clear()
    jobs.clear()
    jobs.configure(3)
    from bot.services import downloader
    from bot.services.search import clear_search_cache
    downloader.clear_provider_failures()
    clear_search_cache()
    yield
    results._SESS.clear()
    url_download._PENDING.clear()
    jobs.clear()
    jobs.configure(3)
    downloader.clear_provider_failures()
    clear_search_cache()


@pytest_asyncio.fixture
async def dp(storage, config):
    for r in ALL_ROUTERS:  # allow re-attaching singleton routers each test
        r._parent_router = None
    d = Dispatcher()
    d["db"] = storage
    d["config"] = config
    d["bot_username"] = "testbot"
    i18n = I18nMiddleware(storage, config.default_locale)
    d.message.outer_middleware(i18n)
    d.callback_query.outer_middleware(i18n)
    for r in ALL_ROUTERS:
        d.include_router(r)
    return d


# ── update builders ──────────────────────────────────────
def text_update(text, uid=1, lang="en", user_id=100, chat_id=100, chat_type="private"):
    return Update(update_id=uid, message=Message(
        message_id=uid, date=_now(), chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="T", language_code=lang),
        text=text,
    ))


def callback_update(
    data, uid=1, lang="en", user_id=100, chat_id=100,
    message_id=None,
):
    return Update(update_id=uid, callback_query=CallbackQuery(
        id=str(uid), chat_instance="ci", data=data,
        from_user=User(id=user_id, is_bot=False, first_name="T", language_code=lang),
        message=Message(
            message_id=message_id if message_id is not None else uid + 5000,
            date=_now(), chat=Chat(id=chat_id, type="private"),
        ),
    ))


def voice_update(uid=1, lang="en", user_id=100, chat_id=100, chat_type="private"):
    return Update(update_id=uid, message=Message(
        message_id=uid, date=_now(), chat=Chat(id=chat_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="T", language_code=lang),
        voice=Voice(file_id="VOICE1", file_unique_id="vu1", duration=5),
    ))


def video_update(uid=1, lang="en", user_id=100, chat_id=100):
    return Update(update_id=uid, message=Message(
        message_id=uid, date=_now(), chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T", language_code=lang),
        video=Video(file_id="VID1", file_unique_id="vu1", width=100, height=100, duration=10),
    ))


def video_note_update(uid=1, lang="en", user_id=100, chat_id=100):
    return Update(update_id=uid, message=Message(
        message_id=uid, date=_now(), chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T", language_code=lang),
        video_note=VideoNote(
            file_id="NOTE1", file_unique_id="nu1", length=240, duration=10
        ),
    ))


def document_update(uid=1, lang="en", user_id=100, chat_id=100):
    return Update(update_id=uid, message=Message(
        message_id=uid, date=_now(), chat=Chat(id=chat_id, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="T", language_code=lang),
        document=Document(
            file_id="DOC1", file_unique_id="du1", file_name="announcement.pdf"
        ),
    ))


def first_callback_data(markup, prefix):
    """Return the first callback_data starting with prefix in an inline markup."""
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith(prefix):
                return btn.callback_data
    return None
