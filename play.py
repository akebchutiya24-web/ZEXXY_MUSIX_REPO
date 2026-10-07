import random
import time

from pyrogram import filters, StopPropagation
from pyrogram.enums import ChatMemberStatus
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InputMediaPhoto,
)
from pytgcalls.types.input_stream import AudioPiped
from pytgcalls.exceptions import NoActiveGroupCall

import config
import db
import music_queue as q
import progress
import botstate
from clients import bot, assistant, call_py, LOGGER, START_TIME
from youtube import search_track, get_stream_url
from helpers import (
    smallcaps_title,
    random_processing_text,
    fancy_italic,
    duration_to_seconds,
    format_uptime,
    expandable_quote,
    strip_quotes,
    smallcaps,
    DIVIDER,
    bullet_lines,
)

from nowplaying import generate_now_playing_card
from assistant_join import ensure_assistant_in_chat

OWNER_FILTER = filters.user(config.OWNER_ID) if config.OWNER_ID else filters.create(lambda _, __, ___: False)

ADMIN_STATUSES = (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

# /addvd chalane ke baad owner ki agli image/video/gif ka wait karte hain (private start message)
_pending_addvd = set()

# /addvd2 chalane ke baad owner ki agli image/video/gif ka wait karte hain (GROUP start message)
_pending_addvd2 = set()

_SEND_MEDIA_MAP_NAME = {"photo": "send_photo", "video": "send_video", "animation": "send_animation"}


# ---------------------------------------------------------------------------
# Peer cache helper — "Peer id invalid" error se bachne ke liye.
# Assistant account jab kisi chat mein direct koi update receive nahi karta
# (sirf VC join karta hai), to pyrogram uska peer/access_hash cache nahi kar
# paata aur baad mein change_stream/leave_group_call fail ho jaata hai.
# Isliye error aane par ek baar dialogs refresh karke retry karte hain.
# ---------------------------------------------------------------------------
async def _refresh_assistant_peers():
    try:
        async for _ in assistant.get_dialogs():
            pass
    except Exception as e:
        LOGGER.warning(f"Peer refresh fail: {e}")


def _is_peer_error(e: Exception) -> bool:
    return isinstance(e, ValueError) and "Peer id invalid" in str(e)


# ---------------------------------------------------------------------------
# Admin / owner check — /skip /pause /resume /stop /reload sirf group admin
# ya bot OWNER_ID ke liye. Normal user sirf /play use kar sakta hai.
# ---------------------------------------------------------------------------
async def _is_group_admin(client, chat_id: int, user_id: int) -> bool:
    if config.OWNER_ID and user_id == config.OWNER_ID:
        return True
    try:
        member = await client.get_chat_member(chat_id, user_id)
        return member.status in ADMIN_STATUSES
    except Exception:
        return False


ADMIN_ONLY_TEXT = f"❌ {smallcaps_title('sirf group admin ya owner hi is command ko use kar sakte hain')}."

NOT_YOUR_REQUEST_TEXT = (
    f"❌ {smallcaps_title('yeh aapka request nahi hai')}!\n"
    f"{smallcaps_title('sirf jisne yeh gaana request kiya hai, ya group ke admin/owner hi ise control kar sakte hain')}."
)


# ---------------------------------------------------------------------------
# Control-permission check — /skip /pause /resume /stop (aur inke inline
# buttons) sirf 3 log use kar sakte hain: jisne current track request kiya
# tha, group admin, ya bot owner. Baaki normal users ko NOT_YOUR_REQUEST_TEXT
# dikhaya jaata hai.
# ---------------------------------------------------------------------------
async def _can_control(client, chat_id: int, user_id: int) -> bool:
    if await _is_group_admin(client, chat_id, user_id):
        return True
    track = q.get_now_playing(chat_id)
    return bool(track and track.get("requested_by_id") == user_id)


ASSISTANT_NOT_JOINED_TEXT = (
    f"❌ **{smallcaps_title('mera assistant account is group mein nahi hai')}!**\n\n"
    f"{smallcaps_title('music bajane ke liye assistant account ka group mein hona zaroori hai')}.\n"
    f"👉 @{config.ASSISTANT_USERNAME} {smallcaps_title('ko group mein add karo, ya isse group join karwao')}.\n\n"
    f"{smallcaps_title('phir dobara')} `/play` {smallcaps_title('karo')}."
)

ASSISTANT_FLOOD_TEXT = (
    f"⏳ {smallcaps_title('telegram ne thodi der ke liye rate-limit laga diya hai, thodi der baad dobara try karo')}."
)


# ---------------------------------------------------------------------------
# Owner: /on /off — pura bot chalu/band karne ke liye global switch.
# OFF hone par bot kisi bhi message/button ka jawab nahi deta, sirf /on /off
# chalte rehte hain. DB mein persist hota hai, isliye restart ke baad bhi
# wahi status yaad rehta hai.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("on") & OWNER_FILTER)
async def on_command(client, message: Message):
    botstate.set_enabled(True)
    await db.set_bot_status(True)
    await message.reply_text(f"✅ {smallcaps_title('bot on kar diya gaya hai')}.")


@bot.on_message(filters.command("off") & OWNER_FILTER)
async def off_command(client, message: Message):
    botstate.set_enabled(False)
    await db.set_bot_status(False)
    await message.reply_text(
        f"🔴 {smallcaps_title('bot off kar diya gaya hai')}.\n"
        f"{smallcaps_title('ab sirf')} `/on` {smallcaps_title('kaam karega')}."
    )


def _off_blocker(_, __, message: Message) -> bool:
    if botstate.is_enabled():
        return False
    text = message.text or message.caption or ""
    # /on aur /off hamesha chalne chahiye, chahe bot OFF hi ho
    return not text.startswith(("/on", "/off"))


def _off_blocker_cb(_, __, cq: CallbackQuery) -> bool:
    return not botstate.is_enabled()


# group=-1 -> yeh handler sabse pehle chalta hai; OFF hone par isse aage kisi
# aur handler tak message/callback pahunchta hi nahi (StopPropagation).
@bot.on_message(filters.create(_off_blocker), group=-1)
async def _blocked_while_off(client, message: Message):
    raise StopPropagation


@bot.on_callback_query(filters.create(_off_blocker_cb), group=-1)
async def _blocked_cb_while_off(client, cq: CallbackQuery):
    await cq.answer(smallcaps_title("bot abhi off hai"), show_alert=True)
    raise StopPropagation


def _btn(text: str, *, style: str = None, **kwargs) -> InlineKeyboardButton:
    """
    InlineKeyboardButton banata hai. Telegram Bot API 9.4 (9 Feb 2026) ke
    colored buttons (style="primary"/"success"/"danger") sirf tab dikhenge
    jab tumhari pyrogram/kurigram/pyrofork library isko support karti ho —
    agar nahi karti, to bina kisi error ke normal (colorless) button ban
    jaata hai. Isse purana bot kabhi crash nahi hoga, aur library update
    karne par colors khud-ba-khud aa jaayenge.
    """
    if style:
        try:
            return InlineKeyboardButton(text, style=style, **kwargs)
        except TypeError:
            pass
    return InlineKeyboardButton(text, **kwargs)


def _controls_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                _btn("▶️", callback_data="m_resume", style="success"),
                _btn("⏸", callback_data="m_pause", style="primary"),
                _btn("🔁", callback_data="m_replay", style="primary"),
                _btn("⏭", callback_data="m_skip", style="primary"),
                _btn("⏹", callback_data="m_stop", style="danger"),
            ],
            [_btn(f"⊙ {smallcaps_title('close')} ⊙", callback_data="m_close", style="danger")],
        ]
    )





def _start_keyboard(bot_username: str):
    return InlineKeyboardMarkup(
        [
            [
                _btn(
                    f"➕ {smallcaps_title('add me to your group')}",
                    url=f"https://t.me/{bot_username}?startgroup=true",
                    style="success",
                )
            ],
            [
                _btn(f"👑 {smallcaps_title('owner')}", url=config.OWNER_URL, style="primary"),
                _btn(f"🛠 {smallcaps_title('support')}", url=config.SUPPORT_URL, style="primary"),
            ],
            [
                _btn(f"📢 {smallcaps_title('channel')}", url=config.CHANNEL_URL, style="primary"),
                _btn(f"❓ {smallcaps_title('help')}", callback_data="help_menu", style="primary"),
            ],
        ]
    )


def _help_keyboard():
    return InlineKeyboardMarkup(
        [[_btn(f"🔙 {smallcaps_title('back')}", callback_data="back_to_start", style="primary")]]
    )


async def _safe_quote_send(action, text: str):
    """
    Message bhejta/edit karta hai expandable quote ke saath. Agar tumhari
    pyrogram/kurigram build `<blockquote expandable>` support na kare, to bina
    kisi crash ke wahi message plain (bina quote) chala jaata hai — text style
    bilkul same rehta hai.
    """
    try:
        return await action(text)
    except Exception as e:
        LOGGER.warning(f"Quote send fail, plain fallback: {e}")
        return await action(strip_quotes(text))


async def _send_welcome(chat_id: int, text: str, reply_markup, media: dict = None):
    """Diye gaye media (photo/video/gif) ke saath ya sirf text ke saath welcome bhejta hai."""
    if media:
        send_func = getattr(bot, _SEND_MEDIA_MAP_NAME.get(media["media_type"], "send_photo"))
        try:
            return await _safe_quote_send(
                lambda t: send_func(chat_id, media["file_id"], caption=t, reply_markup=reply_markup),
                text,
            )
        except Exception as e:
            LOGGER.warning(f"Start media send fail, text fallback: {e}")
    return await _safe_quote_send(
        lambda t: bot.send_message(chat_id, t, reply_markup=reply_markup, disable_web_page_preview=True),
        text,
    )


async def _edit_body(cq_message, text: str, reply_markup):
    """Callback pe message edit karta hai — chahe woh media caption ho ya plain text."""
    if cq_message.photo or cq_message.video or cq_message.animation:
        await _safe_quote_send(lambda t: cq_message.edit_caption(t, reply_markup=reply_markup), text)
    else:
        await _safe_quote_send(
            lambda t: cq_message.edit_text(t, reply_markup=reply_markup, disable_web_page_preview=True),
            text,
        )



HELP_TEXT = (
    "🦋 **ᴀᴠᴀɪʟᴀʙʟᴇ ᴄᴏᴍᴍᴀɴᴅs**\n\n"
    "`/play <song>` — gaana bajao ya queue mein daalo\n"
    "`/skip` — agla gaana _(admin only)_\n"
    "`/pause` — pause karo _(admin only)_\n"
    "`/resume` — resume karo _(admin only)_\n"
    "`/stop` — band karo _(admin only)_\n"
    "`/reload` — bot ko refresh karo _(admin only)_\n"
    "`/id` — apni/group ki ID dekho"
)


def _welcome_text(user_name: str, user_id: int, bot_name: str, bot_username: str) -> str:
    user_tag = f"[{smallcaps_title(user_name)}](tg://user?id={user_id})"
    bot_tag = f"[{fancy_italic(bot_name)}](https://t.me/{bot_username})"
    # Body ek expandable quote ke andar jaata hai — screenshot/video wala effect:
    # tap karke expand hota hai aur andar hi scroll hota hai. Text style same hai.
    body = (
        f"🦋 ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ {bot_tag}\n"
        f"『 ᴘʀᴇᴍɪᴜᴍ ✧ ᴀᴅ-ꜰʀᴇᴇ ✧ ᴜʟᴛʀᴀ sᴍᴏᴏᴛʜ 』\n\n"
        f"🦋 ʜɪɢʜ • Qᴜᴀʟɪᴛʏ • ᴍᴜsɪᴄ • ʙᴏᴛ\n"
        f"ғᴏʀ ᴛᴇʟᴇɢʀᴀᴍ ɢʀᴏᴜᴘs & ᴄʜᴀɴɴᴇʟs\n\n"
        f"🦋✦ ɪɴsᴛᴀɴᴛ sᴛʀᴇᴀᴍɪɴɢ\n"
        f"🦋✦ ᴜʟᴛʀᴀ sᴍᴏᴏᴛʜ ᴘʟᴀʏʙᴀᴄᴋ\n"
        f"🦋✦ ᴄʀʏsᴛᴀʟ ᴄʟᴇᴀʀ sᴏᴜɴᴅ • ɴᴏ ʟᴀɢ\n\n"
        f"🦋✦ ᴛᴀᴘ ʜᴇʟᴘ ᴛᴏ ᴠɪᴇᴡ ᴀʟʟ ᴄᴏᴍᴍᴀɴᴅs\n\n"
        f"🦋ᴘᴏᴡᴇʀᴇᴅ ʙʏ : [Aᴅɪᴛʏᴀ × Aᴘɪꜱ](https://t.me/AdityaXzexxyAPI)\n\n"
        f"╭───────────── ✦ ─────────────╮\n"
        f"❖ ᴇɴᴊᴏʏ ᴛʜᴇ ᴍᴜsɪᴄ ❖\n"
        f"╰───────────── ✦ ─────────────╯"
    )
    return f"🦋 ʜᴇʏ {user_tag}..!! ✦\n\n" + expandable_quote(body)



def _group_start_text(bot_name: str) -> str:
    """Group mein /start ka message — music bot ke kaam ki quick info ke saath."""
    return f"✨ {fancy_italic(bot_name)} ɪs ᴏɴʟɪɴᴇ ᴀɴᴅ ʀᴇᴀᴅʏ ✨\n\n" + expandable_quote(
        "🎧 ᴍᴜsɪᴄ ᴘᴀɴᴇʟ\n"
        "╰┈➤ /play <sᴏɴɢ ɴᴀᴍᴇ> — ɢᴀᴀɴᴀ ʙᴀᴊᴀᴏ\n"
        "╰┈➤ /skip • /pause • /resume • /stop\n"
        "╰┈➤ /queue — ᴀɢʟᴇ ɢᴀᴀɴᴏɴ ᴋɪ ʟɪsᴛ\n\n"
        "⌾ ᴄᴏɴᴛʀᴏʟ : ᴀᴅᴍɪɴs ᴀɴᴅ ʀᴇǫᴜᴇsᴛᴇʀ ᴏɴʟʏ\n"
        "⌾ ǫᴜᴀʟɪᴛʏ : ʜɪɢʜ ᴅᴇғɪɴɪᴛɪᴏɴ ᴀᴜᴅɪᴏ\n\n"
        "•── ⋅ ⋅  ────── ⋅᯽⋅ ────── ⋅ ⋅ ⋅──•"
    )


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("start"))
async def start_cmd(client, message: Message):
    await db.add_user(message.from_user.id)
    me = await bot.get_me()

    if message.chat.type != "private":
        # Group mein /start — alag message, alag media (/addvd2 se set), live uptime
        await db.add_chat(message.chat.id)
        media = await db.get_group_start_media()
        await _send_welcome(
            message.chat.id,
            _group_start_text(me.first_name),
            _start_keyboard(me.username),
            media,
        )
    else:
        # Private /start — purana welcome message, purana media (/addvd se set)
        media = await db.get_start_media()
        await _send_welcome(
            message.chat.id,
            _welcome_text(message.from_user.first_name, message.from_user.id, me.first_name, me.username),
            _start_keyboard(me.username),
            media,
        )

    # Owner ko batao ki kisne bot use kiya (private chat mein)
    if message.chat.type == "private" and config.OWNER_ID and message.from_user.id != config.OWNER_ID:
        try:
            await bot.send_message(
                config.OWNER_ID,
                f"👤 Bot use kiya:\n"
                f"Name: {message.from_user.first_name}\n"
                f"Username: @{message.from_user.username}\n"
                f"ID: `{message.from_user.id}`",
            )
        except Exception as e:
            LOGGER.warning(f"Owner notify fail: {e}")


@bot.on_callback_query(filters.regex("^help_menu$"))
async def help_menu_cb(client, cq: CallbackQuery):
    await cq.answer()
    await _edit_body(cq.message, HELP_TEXT, _help_keyboard())


@bot.on_callback_query(filters.regex("^back_to_start$"))
async def back_to_start_cb(client, cq: CallbackQuery):
    await cq.answer()
    me = await bot.get_me()
    if cq.message.chat.type != "private":
        text = _group_start_text(me.first_name)
    else:
        text = _welcome_text(cq.from_user.first_name, cq.from_user.id, me.first_name, me.username)
    await _edit_body(cq.message, text, _start_keyboard(me.username))


# ---------------------------------------------------------------------------
# Bot ko group mein add kiya jaana
# ---------------------------------------------------------------------------
@bot.on_message(filters.new_chat_members)
async def added_to_group(client, message: Message):
    me = await bot.get_me()
    if not any(u.id == me.id for u in message.new_chat_members):
        return

    await db.add_chat(message.chat.id)
    adder = message.from_user.first_name if message.from_user else "there"

    await message.reply_text(
        f"🎉 ʜᴇʏ **{adder}**!\n\n"
        f"ᴛʜᴀɴᴋ ʏᴏᴜ ғᴏʀ ᴀᴅᴅɪɴɢ **[{me.first_name}](https://t.me/{me.username})** ɪɴ {message.chat.title}.\n\n"
        f"🎶 **{me.first_name}** ɪs ɴᴏᴡ ʀᴇᴀᴅʏ ᴛᴏ sᴛʀᴇᴀᴍ ᴍᴜsɪᴄ, ᴍᴀɴᴀɢᴇ ᴄʜᴀᴛs ᴀɴᴅ ᴅᴇʟɪᴠᴇʀ ᴛʜᴇ ʙᴇsᴛ ᴇxᴘᴇʀɪᴇɴᴄᴇ.",
        reply_markup=_start_keyboard(me.username),
        disable_web_page_preview=True,
    )


# ---------------------------------------------------------------------------
# /play — sabke liye khula hai
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("play") & filters.group)
async def play_command(client, message: Message):
    if len(message.command) < 2:
        return await message.reply_text(
            f"❌ {smallcaps_title('gaane ka naam bhi likho')}!\nExample: `/play Aaj Ki Raat`"
        )

    query = message.text.split(None, 1)[1]
    chat_id = message.chat.id
    requester = message.from_user.mention if message.from_user else "Someone"
    requester_id = message.from_user.id if message.from_user else None

    # Pehle confirm karo ki assistant account is group mein hai — nahi hai to
    # VC join hi nahi ho paayega. Khud join karwane ki koshish yahin hoti hai.
    joined, reason = await ensure_assistant_in_chat(chat_id)
    if not joined:
        if reason == "flood_wait":
            return await message.reply_text(ASSISTANT_FLOOD_TEXT)
        return await message.reply_text(ASSISTANT_NOT_JOINED_TEXT)

    status = await message.reply_text(random_processing_text())

    track = await search_track(query)
    if not track:
        return await status.edit_text(f"❌ {smallcaps_title('kuch nahi mila, doosra naam try karo')}.")

    # Naya API gaana download karke deta hai — thoda time lagta hai, isliye
    # yahan tab tak wait hota hai jab tak gaana ready na ho (max 3 minute).
    try:
        await status.edit_text(f"⏳ {smallcaps_title('gaana download ho raha hai, thoda ruko')}...")
    except Exception:
        pass

    try:
        stream_url = await get_stream_url(track["id"])
    except Exception as e:
        LOGGER.error(f"Stream URL error: {e}")
        return await status.edit_text(
            f"❌ {smallcaps_title('download error')} — "
            f"{smallcaps_title('gaana load nahi ho paya, thodi der baad try karo ya koi aur gaana bhejo')}."
        )


    track["stream_url"] = stream_url
    track["requested_by"] = requester
    track["requested_by_id"] = requester_id

    # Agar pehle se kuch baj raha hai -> queue mein daal do
    if q.is_playing(chat_id):
        position = q.push(chat_id, track)
        await status.delete()
        await message.reply_text(
            f"🎵 {smallcaps_title('added to queue at')} #{position}\n"
            f"📝 {smallcaps_title('title')} : {track['title']}\n"
            f"🕐 {smallcaps_title('duration')} : {track['duration']} ᴍɪɴᴜᴛᴇs\n"
            f"👤 {smallcaps_title('requested')} : {requester}"
        )
        return

    await status.delete()
    await _start_playing(chat_id, track, message)


async def _start_playing(chat_id: int, track: dict, message: Message):
    """VC join/change karke track play karta hai aur Now Playing card bhejta hai."""
    try:
        try:
            await call_py.join_group_call(chat_id, AudioPiped(track["stream_url"]))
        except NoActiveGroupCall:
            return await message.reply_text(
                f"❌ **{smallcaps_title('voice chat active nahi hai')}!**\n\n"
                f"{smallcaps_title('pehle group mein voice chat start karo')}:\n"
                "Group Settings → Voice Chat → Start Voice Chat\n\n"
                f"{smallcaps_title('phir')} `/play` {smallcaps_title('dobara bhejo')}."
            )
        except Exception as e:
            if _is_peer_error(e):
                await _refresh_assistant_peers()
            try:
                await call_py.change_stream(chat_id, AudioPiped(track["stream_url"]))
            except Exception as e2:
                LOGGER.error(f"Play error: {e2}")
                return await message.reply_text(
                    f"❌ **{smallcaps_title('play nahi ho paya')}**\n\n"
                    f"{smallcaps_title('voice chat active hai ya nahi ek baar check kar lo, phir dobara try karo')}."
                )

        q.set_now_playing(chat_id, track)
        await _send_now_playing(chat_id, track, message)

    except Exception as e:
        LOGGER.error(f"_start_playing fatal error: {e}")
        await message.reply_text(f"❌ {smallcaps_title('kuch gadbad ho gayi, dobara try karo')}.")


def _now_playing_caption(track: dict) -> str:
    # Screenshot wala fancy style + expandable quote (tap karke expand/scroll).
    artists = [a.strip() for a in str(track.get("channel") or "").replace("-", ",").split(",") if a.strip()]
    if not artists:
        artists = [track.get("requested_by", "Unknown")]

    body = (
        f"» 『 {smallcaps_title(track['title'])} 』\n"
        f"{bullet_lines(smallcaps_title(a) for a in artists)}\n\n"
        f"⌾ {smallcaps_title('duration')} : {track['duration']}\n"
        f"⌾ {smallcaps_title('by')} : {track.get('requested_by', 'Unknown')}\n\n"
        f"{DIVIDER}"
    )
    return f"❖ {smallcaps_title('Now Playing')}..!! ✦\n\n" + expandable_quote(body)


async def _send_now_playing(chat_id: int, track: dict, message: Message = None, edit_message: Message = None):
    """
    Now Playing card bhejta hai. Agar `edit_message` diya gaya hai (jaise skip
    button se), to naya message bhejne/purana delete karne ke bajaye wahi
    message in-place update ho jaata hai — isse card kabhi "gayab" nahi hota,
    bas apne aap refresh ho jaata hai.
    """
    caption = _now_playing_caption(track)
    card = await generate_now_playing_card(track.get("thumbnail"), track["title"], track["duration"])
    markup = _controls_keyboard()
    media = card or track.get("thumbnail")

    sent = None

    if edit_message is not None:
        try:
            if media:
                sent = await edit_message.edit_media(InputMediaPhoto(media, caption=caption), reply_markup=markup)
            else:
                sent = await edit_message.edit_text(caption, reply_markup=markup, disable_web_page_preview=True)
        except Exception as e:
            LOGGER.warning(f"Now playing in-place edit fail, naya message bhej rahe hain: {e}")

    if sent is None:
        try:
            if media:
                sent = await _safe_quote_send(
                    lambda t: bot.send_photo(chat_id, media, caption=t, reply_markup=markup), caption
                )
            elif message is not None:
                sent = await _safe_quote_send(
                    lambda t: message.reply_text(t, reply_markup=markup, disable_web_page_preview=True), caption
                )
            else:
                sent = await _safe_quote_send(
                    lambda t: bot.send_message(chat_id, t, reply_markup=markup, disable_web_page_preview=True),
                    caption,
                )
        except Exception as e:
            LOGGER.warning(f"Now playing card send fail: {e}")
            plain = strip_quotes(caption)
            if message is not None:
                sent = await message.reply_text(plain, reply_markup=markup, disable_web_page_preview=True)
            else:
                sent = await bot.send_message(chat_id, plain, reply_markup=markup, disable_web_page_preview=True)


    # 🎚️ Live progress bar shuru — gaana ke saath 00:00 se duration tak khud
    # aage badhta rahega, jaise screenshot mein dikha tha.
    if sent is not None:
        total_sec = duration_to_seconds(track.get("duration"))
        progress.start(chat_id, track["id"])
        progress.start_updater(
            chat_id, sent,
            lambda: _now_playing_caption(track),
            _controls_keyboard,
            track["id"], total_sec,
        )

    return sent


# ---------------------------------------------------------------------------
# Stream khatam hone par queue se agla gaana
# ---------------------------------------------------------------------------
@call_py.on_stream_end()
async def on_stream_end(client, update):
    chat_id = update.chat_id
    next_track = q.pop_next(chat_id)

    if not next_track:
        q.set_now_playing(chat_id, None)
        progress.clear(chat_id)
        try:
            await call_py.leave_group_call(chat_id)
        except Exception as e:
            LOGGER.warning(f"Auto leave fail: {e}")
        return


    try:
        try:
            await call_py.change_stream(chat_id, AudioPiped(next_track["stream_url"]))
        except Exception as e:
            if _is_peer_error(e):
                await _refresh_assistant_peers()
            await call_py.join_group_call(chat_id, AudioPiped(next_track["stream_url"]))

        q.set_now_playing(chat_id, next_track)
        await _send_now_playing(chat_id, next_track)
    except Exception as e:
        LOGGER.error(f"Auto-play next error: {e}")


# ---------------------------------------------------------------------------
# /skip /pause /resume /stop — sirf group admin/owner
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("skip") & filters.group)
async def skip_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)

    chat_id = message.chat.id
    next_track = q.pop_next(chat_id)
    if not next_track:
        q.set_now_playing(chat_id, None)
        progress.clear(chat_id)
        try:
            await call_py.leave_group_call(chat_id)
        except Exception:
            pass
        return await message.reply_text(f"⏭ {smallcaps_title('queue khaali hai, vc se nikal gaya')}.")

    try:
        await call_py.change_stream(chat_id, AudioPiped(next_track["stream_url"]))
    except Exception as e:
        if _is_peer_error(e):
            await _refresh_assistant_peers()
        try:
            await call_py.join_group_call(chat_id, AudioPiped(next_track["stream_url"]))
        except Exception as e2:
            LOGGER.error(f"Skip error: {e2}")
            return await message.reply_text(f"❌ {smallcaps_title('skip nahi ho paya, dobara try karo')}.")

    q.set_now_playing(chat_id, next_track)
    await _send_now_playing(chat_id, next_track, message)


@bot.on_message(filters.command("pause") & filters.group)
async def pause_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.pause_stream(message.chat.id)
        q.set_state(message.chat.id, "paused")
        progress.pause(message.chat.id)
        await message.reply_text(f"⏸ {smallcaps_title('paused')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command("resume") & filters.group)
async def resume_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.resume_stream(message.chat.id)
        q.set_state(message.chat.id, "playing")
        progress.resume(message.chat.id)
        await message.reply_text(f"▶️ {smallcaps_title('resumed')}.")
    except Exception as e:
        await message.reply_text(f"❌ {e}")


@bot.on_message(filters.command(["stop", "end"]) & filters.group)
async def stop_command(client, message: Message):
    if not await _can_control(client, message.chat.id, message.from_user.id):
        return await message.reply_text(NOT_YOUR_REQUEST_TEXT)
    try:
        await call_py.leave_group_call(message.chat.id)
    except Exception:
        pass
    q.clear(message.chat.id)
    progress.clear(message.chat.id)
    await message.reply_text(f"⏹️ {smallcaps_title('voice chat band kar diya')}.")


# ---------------------------------------------------------------------------
# /reload — sirf group admin/owner. Check karta hai bot khud admin hai ya nahi.
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("reload") & filters.group)
async def reload_command(client, message: Message):
    if not await _is_group_admin(client, message.chat.id, message.from_user.id):
        return await message.reply_text(ADMIN_ONLY_TEXT)

    me = await bot.get_me()
    try:
        bot_member = await client.get_chat_member(message.chat.id, me.id)
        is_bot_admin = bot_member.status in ADMIN_STATUSES
    except Exception as e:
        LOGGER.warning(f"Reload admin-check fail: {e}")
        is_bot_admin = False

    if is_bot_admin:
        await message.reply_text(f"✅ {smallcaps_title('reloaded successfully')}.")
    else:
        await message.reply_text(
            f"❌ {smallcaps_title('mujhe pehle group admin banao, phir')} `/reload` {smallcaps_title('karo')}."
        )


# ---------------------------------------------------------------------------
# Inline buttons (Now Playing card ke neeche)
# ---------------------------------------------------------------------------

@bot.on_callback_query(filters.regex("^m_"))
async def controls_callback(client, cq: CallbackQuery):
    chat_id = cq.message.chat.id
    action = cq.data

    # Skip/pause/resume/stop buttons — commands jaisa hi permission check:
    # sirf jisne current track request kiya tha, ya group admin/owner.
    if action in ("m_resume", "m_pause", "m_skip", "m_stop"):
        if not await _can_control(client, chat_id, cq.from_user.id):
            return await cq.answer(NOT_YOUR_REQUEST_TEXT, show_alert=True)


    try:
        if action == "m_resume":
            await call_py.resume_stream(chat_id)
            q.set_state(chat_id, "playing")
            progress.resume(chat_id)
            await cq.answer("▶️ Resumed")

        elif action == "m_pause":
            await call_py.pause_stream(chat_id)
            q.set_state(chat_id, "paused")
            progress.pause(chat_id)
            await cq.answer("⏸ Paused")

        elif action == "m_replay":
            track = q.get_now_playing(chat_id)
            if track:
                await call_py.change_stream(chat_id, AudioPiped(track["stream_url"]))
                progress.replay(chat_id)
                await cq.answer("🔁 Replaying")
            else:
                await cq.answer(smallcaps_title("kuch bhi nahi baj raha"), show_alert=True)

        elif action == "m_skip":
            await cq.answer("⏭ Skipping")
            next_track = q.pop_next(chat_id)
            if not next_track:
                q.set_now_playing(chat_id, None)
                progress.clear(chat_id)
                await call_py.leave_group_call(chat_id)
                try:
                    await cq.message.edit_reply_markup(None)
                except Exception:
                    pass
                await cq.message.reply_text(f"⏭ {smallcaps_title('queue khaali hai, vc se nikal gaya')}.")
            else:
                await call_py.change_stream(chat_id, AudioPiped(next_track["stream_url"]))
                q.set_now_playing(chat_id, next_track)
                # Naya message bhejne ke bajaye wahi card in-place update ho jaata hai
                # (_send_now_playing khud naye track ka progress bar shuru kar deta hai)
                await _send_now_playing(chat_id, next_track, edit_message=cq.message)

        elif action == "m_stop":
            await call_py.leave_group_call(chat_id)
            q.clear(chat_id)
            progress.clear(chat_id)
            await cq.answer("⏹ Stopped")
            try:
                await cq.message.edit_reply_markup(None)
            except Exception:
                pass
            await cq.message.reply_text(f"⏹️ {smallcaps_title('voice chat band kar diya')}.")

        elif action == "m_close":

            await cq.answer()
            progress.cancel_task(chat_id)
            await cq.message.delete()

    except Exception as e:
        LOGGER.warning(f"Callback error ({action}): {e}")
        await cq.answer(f"❌ {e}", show_alert=True)


# ---------------------------------------------------------------------------
# Owner: /addvd /delvd — /start message ke saath jaane wala image/video/gif
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("addvd") & OWNER_FILTER)
async def addvd_command(client, message: Message):
    _pending_addvd.add(message.from_user.id)
    await message.reply_text(
        f"🖼 {smallcaps_title('ab ek image, video ya gif bhejo — wahi ab se PRIVATE start message ke saath sabko jayega')}."
    )


@bot.on_message(filters.command("delvd") & OWNER_FILTER)
async def delvd_command(client, message: Message):
    await db.delete_start_media()
    _pending_addvd.discard(message.from_user.id)
    await message.reply_text(f"🗑 {smallcaps_title('private start message media hata diya gaya')}.")


@bot.on_message(filters.command("addvd2") & OWNER_FILTER)
async def addvd2_command(client, message: Message):
    _pending_addvd2.add(message.from_user.id)
    await message.reply_text(
        f"🖼 {smallcaps_title('ab ek image, video ya gif bhejo — wahi ab se GROUP start message ke saath sabko jayega')}."
    )


@bot.on_message(filters.command("delvd2") & OWNER_FILTER)
async def delvd2_command(client, message: Message):
    await db.delete_group_start_media()
    _pending_addvd2.discard(message.from_user.id)
    await message.reply_text(f"🗑 {smallcaps_title('group start message media hata diya gaya')}.")


@bot.on_message(
    (filters.photo | filters.video | filters.animation)
    & OWNER_FILTER
    & filters.create(
        lambda _, __, m: bool(m.from_user)
        and (m.from_user.id in _pending_addvd or m.from_user.id in _pending_addvd2)
    )
)
async def addvd_receive(client, message: Message):
    is_group_variant = message.from_user.id in _pending_addvd2
    _pending_addvd.discard(message.from_user.id)
    _pending_addvd2.discard(message.from_user.id)

    if message.photo:
        file_id, media_type = message.photo.file_id, "photo"
    elif message.video:
        file_id, media_type = message.video.file_id, "video"
    elif message.animation:
        file_id, media_type = message.animation.file_id, "animation"
    else:
        return

    if is_group_variant:
        await db.set_group_start_media(file_id, media_type)
        await message.reply_text(f"✅ {smallcaps_title('group start message media set ho gaya')}.")
    else:
        await db.set_start_media(file_id, media_type)
        await message.reply_text(f"✅ {smallcaps_title('private start message media set ho gaya')}.")


# ---------------------------------------------------------------------------
# Owner: /broadcast
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("broadcast") & OWNER_FILTER)
async def broadcast_command(client, message: Message):
    if len(message.command) < 2 and not message.reply_to_message:
        return await message.reply_text(
            f"❌ {smallcaps_title('broadcast ke liye message do')}!\nExample: `/broadcast Hello everyone`"
        )

    text = message.text.split(None, 1)[1] if len(message.command) > 1 else None
    users = await db.get_all_users()
    status = await message.reply_text(f"📢 {smallcaps_title('broadcasting to')} {len(users)} {smallcaps_title('users')}...")

    sent, failed = 0, 0
    for uid in users:
        try:
            if message.reply_to_message:
                await message.reply_to_message.copy(uid)
            else:
                await bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1

    await status.edit_text(
        f"✅ {smallcaps_title('broadcast done')}.\n{smallcaps_title('sent')}: {sent}\n{smallcaps_title('failed')}: {failed}"
    )


# ---------------------------------------------------------------------------
# /id — user aur chat id batao
# ---------------------------------------------------------------------------
@bot.on_message(filters.command("id"))
async def id_command(client, message: Message):
    user_id = message.from_user.id if message.from_user else "Unknown"
    lines = [f"👤 **{smallcaps_title('your id')}:** `{user_id}`"]
    if message.chat.type != "private":
        lines.append(f"👥 **{smallcaps_title('chat id')}:** `{message.chat.id}`")
    if message.reply_to_message and message.reply_to_message.from_user:
        lines.append(f"↩️ **{smallcaps_title('replied user id')}:** `{message.reply_to_message.from_user.id}`")
    await message.reply_text("\n".join(lines))
