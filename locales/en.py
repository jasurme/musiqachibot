STRINGS = {
    "welcome": (
        "👋 Hi! I'll help you find music 🎶\n\n"
        "Send me one of the following:\n\n"
        "🎵 Song or artist name\n"
        "🎙 Voice message with music\n"
        "🎬 Video with music\n"
        "🎧 Audio recording\n"
        "🔗 An Instagram, TikTok, YouTube, Facebook or X link\n\n"
        "🕺 Enjoy!"
    ),
    "choose_language": "🌐 Choose your language:",
    "language_set": "✅ Language changed.",
    "cmd_start": "Start",
    "cmd_round": "Make a round video",
    "cmd_top_music": "Top Music",
    "cmd_new_music": "New Music",
    "cmd_rising": "Rising music",
    "cmd_discoveries": "Weekly discoveries",
    "cmd_moods": "Music by mood",
    "cmd_total_users": "User count",
    "cmd_lang": "Change language",
    "cmd_privacy": "Privacy information",
    "cmd_delete_my_data": "Delete my stored data",
    "btn_privacy_policy": "Full privacy policy",
    "privacy_notice": (
        "🔐 <b>Privacy</b>\n\n"
        "The bot stores your Telegram user ID and language until deletion, "
        "including to deliver bot announcements; "
        "button/search session data for up to 7 days; and recognition match "
        "metadata for up to 30 days. Shared Telegram file IDs may be retained. "
        "Uploaded media and recognition samples are deleted locally after processing.\n\n"
        "Short samples or metadata may be processed by Shazam/AudD; searches and "
        "downloads contact the source services; lyric requests contact lyrics.ovh; "
        "and Railway plus any configured proxy process network traffic. Use "
        "/delete_my_data to remove data associated with your user ID."
    ),
    "data_deleted": "✅ Data associated with your user ID was deleted.",
    "total_users_report": (
        "👥 <b>Users</b>\n\n"
        "Total: {total}\nActive: {active}\nInactive: {inactive}"
    ),
    "broadcast_done": (
        "✅ Broadcast complete. Sent: {sent}. "
        "Inactive: {inactive}. Failed: {failed}."
    ),
    "broadcast_unsupported": "⚠️ Telegram cannot copy this message type.",
    "broadcast_choose_format": "How should this video be sent?",
    "broadcast_btn_normal": "🎬 Normal video",
    "broadcast_btn_circle": "⭕ Circle video",
    "broadcast_btn_confirm": "✅ Send to everyone",
    "broadcast_btn_cancel": "❌ Cancel",
    "broadcast_confirm_normal": "🎬 Send this as a normal video to every user?",
    "broadcast_confirm_circle": "⭕ Send this as a circle video to every user?",
    "broadcast_preparing": "⏳ Preparing the video…",
    "broadcast_cancelled": "✅ Broadcast cancelled.",
    "broadcast_draft_expired": "⚠️ These broadcast buttons expired. Send the video again.",
    "broadcast_prepare_failed": "❌ Couldn't prepare the selected video format.",
    "broadcast_already_started": "⚠️ This broadcast was already sent or is in progress.",
    "btn_top_music": "🎧 Find music",
    "top_music_header": "<b>Top Music</b>",
    "top_music_unavailable": (
        "⚠️ Top Music is not ready yet. Please try again shortly."
    ),
    "new_music_header": "<b>🔥 New Music</b>",
    "rising_header": "<b>🚀 Rising Now</b>",
    "discoveries_header": "<b>💎 Weekly Discoveries</b>",
    "moods_header": "<b>🎭 Choose a mood</b>",
    "mood_night": "🌙 Night Vibes",
    "mood_road": "🚗 For the Road",
    "mood_workout": "🏋️ Workout",
    "mood_calm": "💔 Calm Songs",
    "mood_weekend": "🎉 Weekend Mood",
    "mood_night_header": "<b>🌙 Night Vibes</b>",
    "mood_road_header": "<b>🚗 For the Road</b>",
    "mood_workout_header": "<b>🏋️ Workout</b>",
    "mood_calm_header": "<b>💔 Calm Songs</b>",
    "mood_weekend_header": "<b>🎉 Weekend Mood</b>",
    "music_campaign_unavailable": (
        "⚠️ This list is not ready yet. Please try again shortly."
    ),
    "btn_back": "⬅️ Back",
    "btn_open_moods": "🎭 Music by mood",
    # Feature D — links
    "fetching": "🔎 Fetching info...",
    "choose_quality": "🎬 Choose quality:",
    "downloading_quality": "⏳ Downloading {quality}...",
    "link_expired": "⚠️ Link expired. Please send it again.",
    "download_failed": "❌ Couldn't download. Make sure the link is correct and public.",
    "download_blocked": "⚠️ The source temporarily blocked this server. Please try again later.",
    "download_rate_limited": "⚠️ The source is rate-limiting downloads. Please try again shortly.",
    "download_private": "🔒 This media is private or requires an account.",
    "download_too_large": "⚠️ This media is live, too long, or exceeds the bot's download limit.",
    "upload_failed": "⚠️ The file was prepared, but Telegram delivery failed. Please try again.",
    "already_processing": "⏳ Your previous request is still processing.",
    "service_busy": "⏳ The media service is busy. Please try again shortly.",
    "invalid_action": "⚠️ This button is invalid or outdated. Please start again.",
    "unsupported_link": "🤔 That link isn't supported. Send an Instagram, TikTok, YouTube, Facebook or X link.",
    "too_big": "⚠️ File is too big ({size} MB). This bot's current limit is {limit} MB.",
    # Feature A/B — search & lyrics
    "searching": "🔎 Searching...",
    "no_results": "😔 Nothing found. Try a different name.",
    "query_too_long": "⚠️ That search is too long. Keep it under {limit} characters.",
    "sending_track": "⏳ Sending...",
    "btn_lyrics": "Lyrics",
    "btn_video": "Video",
    "btn_listen": "Listen",
    "btn_audio": "Audio",
    "btn_find_music": "Find music",
    "btn_round": "Round video",
    "lyrics_not_found": "😔 Lyrics not found.",
    # round video-notes
    "round_prompt": "⭕ Send me a video or a video link and I'll turn it into a round video.",
    "round_processing": "⭕ Making a round video...",
    "round_failed": "❌ Couldn't make the round video. Try another one.",
    # Feature C — recognition
    "recognizing": "🎧 Recognizing music...",
    "not_recognized": "😔 Couldn't recognize it. Send a clearer clip.",
    "recognition_failed": "⚠️ Music recognition is temporarily unavailable. Please try again.",
    "rec_header": "🎵 Song title: <b>{title}</b>\nArtist: <b>{artist}</b>",
    "generic_error": "❌ Something went wrong. Please try again later.",
}
