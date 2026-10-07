import os
import asyncio
import aiohttp
from youtube_search import YoutubeSearch

NEXOR_STREAM_URL = os.environ.get("NEXOR_STREAM_URL", "https://nexor-yt-pannl.lovable.app")
NEXOR_STREAM_KEY = os.environ.get("NEXOR_STREAM_KEY", "NEXOR_3481fsghsh62ghs")

# API ko gaana taiyaar karne me time lagta hai — 3 minute tak wait karte hain.
DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "180"))



async def search_youtube(query: str):
    """YouTube pe search karta hai, pehla result deta hai (raw dict, library format)."""
    loop = asyncio.get_event_loop()

    def _search():
        results = YoutubeSearch(query, max_results=1).to_dict()
        return results[0] if results else None

    return await loop.run_in_executor(None, _search)


async def search_track(query: str):
    """search_youtube ka normalized wrapper — id/title/duration/thumbnail/url deta hai."""
    result = await search_youtube(query)
    if not result:
        return None

    thumbnails = result.get("thumbnails") or []
    video_id = result.get("id")

    return {
        "id": video_id,
        "title": result.get("title", "Unknown"),
        "duration": result.get("duration", ""),
        "thumbnail": thumbnails[0] if thumbnails else None,
        "channel": result.get("channel") or result.get("uploader") or "",
        "url": f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
    }


class DownloadError(Exception):
    """API se gaana download/ready nahi ho paya."""


def _video_id(video: str) -> str:
    """Poora YouTube link ya id — dono se sirf video id nikalta hai (API ko id chahiye)."""
    video = (video or "").strip()
    if not video:
        return ""
    if "youtu.be/" in video:
        video = video.split("youtu.be/")[1]
    elif "v=" in video:
        video = video.split("v=")[1]
    elif "/shorts/" in video:
        video = video.split("/shorts/")[1]
    elif "/embed/" in video:
        video = video.split("/embed/")[1]
    for sep in ("?", "&", "/", "#"):
        video = video.split(sep)[0]
    return video


async def get_stream_url(video: str) -> str:
    """
    Nexor stream API se seedha streaming url banata hai:
    https://nexor-yt-pannl.lovable.app/stream/<video_id>?key=<NEXOR_STREAM_KEY>

    Yeh API audio seedha stream karta hai (koi redirect/json nahi, seedha
    audio/mpeg response), isliye humein file download karke rakhne ki zaroorat
    nahi — yahi URL seedha AudioPiped ko de dete hain aur ffmpeg khud
    progressively stream kar leta hai. Bas ek baar connect karke confirm kar
    lete hain ki video id valid hai aur API jawab de raha hai (bina poora
    body padhe), warna DownloadError.
    """
    video_id = _video_id(video)
    if not video_id:
        raise DownloadError("Video id nahi mila")

    stream_url = f"{NEXOR_STREAM_URL}/stream/{video_id}?key={NEXOR_STREAM_KEY}"
    timeout = aiohttp.ClientTimeout(total=DOWNLOAD_TIMEOUT, sock_connect=30)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(stream_url) as resp:
                if resp.status != 200:
                    raise DownloadError(f"API ne unexpected response diya: {resp.status}")
                # Sirf itna confirm karna tha ki stream mil raha hai — poora
                # audio yahan download nahi karna, isliye body padhe bina hi
                # `async with` connection close kar dega.
    except asyncio.TimeoutError:
        raise DownloadError("Stream API timeout — thodi der baad try karo")
    except aiohttp.ClientError as e:
        raise DownloadError(f"API tak pahunch nahi paaye: {e}")

    return stream_url


async def get_related_track(title: str, exclude_id: str = None):
    """
    Autoplay ke liye — current gaane ke naam se milta-julta agla gaana dhoondta
    hai (youtube ke autoplay jaisa). Same video dobara na aaye iske liye
    `exclude_id` skip kar diya jaata hai.
    """
    loop = asyncio.get_event_loop()
    base = (title or "").split("|")[0].split("(")[0].strip()
    if not base:
        return None

    def _search():
        try:
            return YoutubeSearch(f"{base} song", max_results=8).to_dict()
        except Exception:
            return []

    results = await loop.run_in_executor(None, _search)
    for result in results or []:
        video_id = result.get("id")
        if not video_id or video_id == exclude_id:
            continue
        thumbnails = result.get("thumbnails") or []
        return {
            "id": video_id,
            "title": result.get("title", "Unknown"),
            "duration": result.get("duration", ""),
            "thumbnail": thumbnails[0] if thumbnails else None,
            "channel": result.get("channel") or result.get("uploader") or "",
            "url": f"https://www.youtube.com/watch?v={video_id}",
        }
    return None
