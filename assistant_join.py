"""
Har group mein music play karne se pehle assistant (userbot) account ka
wahan hona zaroori hai — VC join wahi karta hai, bot nahi.

Yeh module check karta hai ki assistant already group mein hai ya nahi, aur
agar nahi hai to khud-ba-khud join karwane ki koshish karta hai:

  1. Agar group public hai (username wala) -> assistant seedha us
     username se join kar leta hai.
  2. Agar group private hai -> bot (jo already group mein hai) ek invite
     link nikalta hai aur assistant usse join karta hai.
  3. Last resort -> bot khud assistant ko member ke roop mein add karne ki
     koshish karta hai (isके liye bot ke paas "invite users" admin right
     chahiye).

Teeno fail ho jayein (jaise bot khud admin nahi hai, ya assistant ka privacy
setting block kar rahi hai) to caller ko bataya jaata hai ki manually
@ASSISTANT_USERNAME ko group mein add/join karwao.
"""

from pyrogram.errors import (
    UserNotParticipant,
    UserAlreadyParticipant,
    FloodWait,
    ChatAdminRequired,
    RPCError,
)

import config
from clients import bot, assistant, LOGGER


def _is_peer_error(e: Exception) -> bool:
    return isinstance(e, ValueError) and "Peer id invalid" in str(e)


async def _refresh_assistant_peers():
    try:
        async for _ in assistant.get_dialogs():
            pass
    except Exception as e:
        LOGGER.warning(f"Assistant peer refresh fail: {e}")


async def is_assistant_in_chat(chat_id: int) -> bool:
    """
    Assistant is chat ka member hai ya nahi, seedha check karta hai.

    Assistant `in_memory=True` session use karta hai, isliye bot restart hone
    par uska peer/access_hash cache khaali ho jaata hai. Agar assistant us
    group mein khud koi update receive nahi karta (sirf VC join karta hai),
    to pyrogram `get_chat_member` call pe "Peer id invalid" ValueError de
    deta hai — chahe assistant group mein ho bhi, isliye galat se "nahi hai"
    bol deta tha. Fix: yeh error aane par ek baar dialogs refresh karke (jo
    saare peers wapas cache mein le aata hai) dobara try karte hain.
    """
    try:
        await assistant.get_chat_member(chat_id, "me")
        return True
    except UserNotParticipant:
        return False
    except Exception as e:
        if _is_peer_error(e):
            LOGGER.info("Assistant peer cache mein nahi tha — dialogs refresh karke retry.")
            await _refresh_assistant_peers()
            try:
                await assistant.get_chat_member(chat_id, "me")
                return True
            except UserNotParticipant:
                return False
            except Exception as e2:
                LOGGER.warning(f"Assistant membership check fail (retry ke baad bhi): {e2}")
                return False
        LOGGER.warning(f"Assistant membership check fail: {e}")
        return False


async def ensure_assistant_in_chat(chat_id: int):
    """
    Assistant chat mein hai ya nahi confirm karta hai, aur agar nahi hai to
    join karwane ki poori koshish karta hai.

    Returns: (joined: bool, reason: str)
        joined=True  -> assistant ab chat mein hai, VC join kiya jaa sakta hai.
        joined=False -> nahi ho paya; `reason` batata hai kyun (caller isse
                         user-facing message banane ke liye use kar sakta hai).
    """
    if await is_assistant_in_chat(chat_id):
        return True, ""

    try:
        chat = await bot.get_chat(chat_id)
    except Exception as e:
        LOGGER.warning(f"get_chat fail (assistant join se pehle): {e}")
        chat = None

    # --- Try 1: public group -> username se seedha join ---------------
    if chat and chat.username:
        try:
            await assistant.join_chat(chat.username)
            LOGGER.info(f"Assistant public group @{chat.username} mein join ho gaya.")
            return True, ""
        except UserAlreadyParticipant:
            return True, ""
        except FloodWait as e:
            LOGGER.warning(f"Assistant join FloodWait: {e.value}s")
            return False, "flood_wait"
        except RPCError as e:
            LOGGER.warning(f"Assistant username join fail: {e}")

    # --- Try 2: private group -> bot invite link banaye, assistant use join kare
    try:
        link = getattr(chat, "invite_link", None) if chat else None
        if not link:
            link = await bot.export_chat_invite_link(chat_id)
        if link:
            try:
                await assistant.join_chat(link)
                LOGGER.info(f"Assistant invite link se chat {chat_id} mein join ho gaya.")
                return True, ""
            except UserAlreadyParticipant:
                return True, ""
    except ChatAdminRequired:
        LOGGER.warning("Bot admin nahi hai — invite link export nahi ho paya.")
    except FloodWait as e:
        LOGGER.warning(f"Assistant join FloodWait: {e.value}s")
        return False, "flood_wait"
    except Exception as e:
        LOGGER.warning(f"Assistant invite-link join fail: {e}")

    # --- Try 3: bot khud assistant ko group mein add kare (agar right ho)
    try:
        me_assistant = await assistant.get_me()
        await bot.add_chat_members(chat_id, me_assistant.id)
        # add_chat_members turant confirm nahi karta, isliye dobara verify karo
        if await is_assistant_in_chat(chat_id):
            LOGGER.info(f"Bot ne assistant ko chat {chat_id} mein add kar diya.")
            return True, ""
    except Exception as e:
        LOGGER.warning(f"Bot add_chat_members(assistant) fail: {e}")

    return False, "manual_needed"
