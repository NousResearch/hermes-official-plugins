"""The adapter's user-facing media notices, in every language Hermes core shipped them in.

Core carried these keys in its bundled ``locales/*.yaml`` until WeCom left core; the plugin keeps the
same text and the same language choice (``HERMES_LANGUAGE`` from the profile's secrets, else
``display.language`` from its config.yaml, else English) without importing core's private i18n module.
The notices shipped in Chinese before core had i18n; the ``zh`` table keeps that original text.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_MESSAGES: dict[str, dict[str, str]] = {
    'en': {
        'platform.wecom.media.kind_image': "Image",
        'platform.wecom.media.kind_video': "Video",
        'platform.wecom.media.kind_voice': "Voice message",
        'platform.wecom.media.file_too_large': "File size {size_mb}MB exceeds WeCom's 20MB limit and cannot be sent. Try compressing the file or reducing its size.",
        'platform.wecom.media.voice_format_downgraded': "Voice format {content_type} is not supported (WeCom only accepts AMR); sent as a file instead.",
        'platform.wecom.media.size_downgraded': "{label} size {size_mb}MB exceeds the {cap} limit; sent as a file instead.",
    },
    'af': {
        'platform.wecom.media.kind_image': "Beeld",
        'platform.wecom.media.kind_video': "Video",
        'platform.wecom.media.kind_voice': "Stemboodskap",
        'platform.wecom.media.file_too_large': "Lêergrootte {size_mb}MB oorskry WeCom se limiet van 20MB en kan nie gestuur word nie. Probeer die lêer saampers of kleiner maak.",
        'platform.wecom.media.voice_format_downgraded': "Stemformaat {content_type} word nie ondersteun nie (WeCom aanvaar slegs AMR); eerder as 'n lêer gestuur.",
        'platform.wecom.media.size_downgraded': "{label}-grootte {size_mb}MB oorskry die limiet van {cap}; eerder as 'n lêer gestuur.",
    },
    'ar': {
        'platform.wecom.media.kind_image': "صورة",
        'platform.wecom.media.kind_video': "فيديو",
        'platform.wecom.media.kind_voice': "رسالة صوتية",
        'platform.wecom.media.file_too_large': "حجم الملف {size_mb} ميغابايت يتجاوز حد WeCom البالغ 20 ميغابايت ولا يمكن إرساله. جرّب ضغط الملف أو تقليل حجمه.",
        'platform.wecom.media.voice_format_downgraded': "صيغة الصوت {content_type} غير مدعومة (WeCom يقبل AMR فقط)؛ أُرسل كملف بدلًا من ذلك.",
        'platform.wecom.media.size_downgraded': "حجم {label} البالغ {size_mb} ميغابايت يتجاوز حد {cap}؛ أُرسل كملف بدلًا من ذلك.",
    },
    'de': {
        'platform.wecom.media.kind_image': "Bild",
        'platform.wecom.media.kind_video': "Video",
        'platform.wecom.media.kind_voice': "Sprachnachricht",
        'platform.wecom.media.file_too_large': "Die Dateigröße von {size_mb}MB überschreitet das 20-MB-Limit von WeCom und kann nicht gesendet werden. Versuchen Sie, die Datei zu komprimieren oder zu verkleinern.",
        'platform.wecom.media.voice_format_downgraded': "Das Sprachformat {content_type} wird nicht unterstützt (WeCom akzeptiert nur AMR); stattdessen als Datei gesendet.",
        'platform.wecom.media.size_downgraded': "{label}-Größe von {size_mb}MB überschreitet das Limit von {cap}; stattdessen als Datei gesendet.",
    },
    'es': {
        'platform.wecom.media.kind_image': "Imagen",
        'platform.wecom.media.kind_video': "Video",
        'platform.wecom.media.kind_voice': "Mensaje de voz",
        'platform.wecom.media.file_too_large': "El archivo pesa {size_mb}MB y supera el límite de 20MB de WeCom, así que no se puede enviar. Prueba comprimirlo o reducir su tamaño.",
        'platform.wecom.media.voice_format_downgraded': "El formato de voz {content_type} no es compatible (WeCom solo acepta AMR); se envió como archivo.",
        'platform.wecom.media.size_downgraded': "{label} pesa {size_mb}MB y supera el límite de {cap}; se envió como archivo.",
    },
    'fr': {
        'platform.wecom.media.kind_image': "Image",
        'platform.wecom.media.kind_video': "Vidéo",
        'platform.wecom.media.kind_voice': "Message vocal",
        'platform.wecom.media.file_too_large': "La taille du fichier ({size_mb} Mo) dépasse la limite de 20 Mo de WeCom et il ne peut pas être envoyé. Essayez de compresser le fichier ou de réduire sa taille.",
        'platform.wecom.media.voice_format_downgraded': "Le format vocal {content_type} n'est pas pris en charge (WeCom n'accepte que l'AMR) ; envoyé sous forme de fichier à la place.",
        'platform.wecom.media.size_downgraded': "La taille de {label} ({size_mb} Mo) dépasse la limite de {cap} ; envoyé sous forme de fichier à la place.",
    },
    'ga': {
        'platform.wecom.media.kind_image': "Íomhá",
        'platform.wecom.media.kind_video': "Físeán",
        'platform.wecom.media.kind_voice': "Teachtaireacht ghutha",
        'platform.wecom.media.file_too_large': "Sáraíonn méid an chomhaid {size_mb}MB teorainn 20MB WeCom agus ní féidir é a sheoladh. Bain triail as an gcomhad a dhlúthú nó a mhéid a laghdú.",
        'platform.wecom.media.voice_format_downgraded': "Ní thacaítear leis an bhformáid ghutha {content_type} (ní ghlacann WeCom ach le AMR); seoladh mar chomhad ina ionad.",
        'platform.wecom.media.size_downgraded': "Sáraíonn méid {label} {size_mb}MB an teorainn {cap}; seoladh mar chomhad ina ionad.",
    },
    'hu': {
        'platform.wecom.media.kind_image': "Kép",
        'platform.wecom.media.kind_video': "Videó",
        'platform.wecom.media.kind_voice': "Hangüzenet",
        'platform.wecom.media.file_too_large': "A fájl mérete ({size_mb} MB) meghaladja a WeCom 20 MB-os korlátját, így nem küldhető el. Próbáld tömöríteni vagy kisebbre venni a fájlt.",
        'platform.wecom.media.voice_format_downgraded': "A {content_type} hangformátum nem támogatott (a WeCom csak AMR-t fogad); fájlként küldtem el helyette.",
        'platform.wecom.media.size_downgraded': "{label} mérete ({size_mb} MB) meghaladja a {cap} korlátot; fájlként küldtem el helyette.",
    },
    'it': {
        'platform.wecom.media.kind_image': "Immagine",
        'platform.wecom.media.kind_video': "Video",
        'platform.wecom.media.kind_voice': "Messaggio vocale",
        'platform.wecom.media.file_too_large': "La dimensione del file ({size_mb}MB) supera il limite di 20MB di WeCom e non può essere inviato. Prova a comprimere il file o a ridurne la dimensione.",
        'platform.wecom.media.voice_format_downgraded': "Il formato vocale {content_type} non è supportato (WeCom accetta solo AMR); inviato come file.",
        'platform.wecom.media.size_downgraded': "{label}: la dimensione di {size_mb}MB supera il limite di {cap}; inviato come file.",
    },
    'ja': {
        'platform.wecom.media.kind_image': "画像",
        'platform.wecom.media.kind_video': "動画",
        'platform.wecom.media.kind_voice': "音声メッセージ",
        'platform.wecom.media.file_too_large': "ファイルサイズ {size_mb}MB が WeCom の上限 20MB を超えているため送信できません。ファイルを圧縮するか、サイズを小さくしてください。",
        'platform.wecom.media.voice_format_downgraded': "音声形式 {content_type} はサポートされていません（WeCom は AMR のみ対応）。代わりにファイルとして送信しました。",
        'platform.wecom.media.size_downgraded': "{label}のサイズ {size_mb}MB が上限 {cap} を超えています。代わりにファイルとして送信しました。",
    },
    'ko': {
        'platform.wecom.media.kind_image': "이미지",
        'platform.wecom.media.kind_video': "동영상",
        'platform.wecom.media.kind_voice': "음성 메시지",
        'platform.wecom.media.file_too_large': "파일 크기 {size_mb}MB가 WeCom의 20MB 한도를 넘어 보낼 수 없어요. 파일을 압축하거나 크기를 줄여 보세요.",
        'platform.wecom.media.voice_format_downgraded': "음성 형식 {content_type}은(는) 지원되지 않아요 (WeCom은 AMR만 허용). 대신 파일로 보냈어요.",
        'platform.wecom.media.size_downgraded': "{label} 크기 {size_mb}MB가 {cap} 한도를 넘어 대신 파일로 보냈어요.",
    },
    'pt': {
        'platform.wecom.media.kind_image': "Imagem",
        'platform.wecom.media.kind_video': "Vídeo",
        'platform.wecom.media.kind_voice': "Mensagem de voz",
        'platform.wecom.media.file_too_large': "O tamanho do arquivo, {size_mb}MB, excede o limite de 20MB do WeCom e não pode ser enviado. Tente comprimir o arquivo ou reduzir seu tamanho.",
        'platform.wecom.media.voice_format_downgraded': "O formato de voz {content_type} não é suportado (o WeCom só aceita AMR); enviado como arquivo.",
        'platform.wecom.media.size_downgraded': "{label} com {size_mb}MB excede o limite de {cap}; enviado como arquivo.",
    },
    'ru': {
        'platform.wecom.media.kind_image': "Изображение",
        'platform.wecom.media.kind_video': "Видео",
        'platform.wecom.media.kind_voice': "Голосовое сообщение",
        'platform.wecom.media.file_too_large': "Размер файла {size_mb} МБ превышает лимит WeCom в 20 МБ, отправить его нельзя. Попробуйте сжать файл или уменьшить его размер.",
        'platform.wecom.media.voice_format_downgraded': "Голосовой формат {content_type} не поддерживается (WeCom принимает только AMR); отправлено как файл.",
        'platform.wecom.media.size_downgraded': "{label}: размер {size_mb} МБ превышает лимит {cap}; отправлено как файл.",
    },
    'tr': {
        'platform.wecom.media.kind_image': "Görüntü",
        'platform.wecom.media.kind_video': "Video",
        'platform.wecom.media.kind_voice': "Sesli mesaj",
        'platform.wecom.media.file_too_large': "Dosya boyutu ({size_mb}MB) WeCom'un 20MB sınırını aşıyor ve gönderilemiyor. Dosyayı sıkıştırmayı veya boyutunu küçültmeyi deneyin.",
        'platform.wecom.media.voice_format_downgraded': "{content_type} ses biçimi desteklenmiyor (WeCom yalnızca AMR kabul eder); bunun yerine dosya olarak gönderildi.",
        'platform.wecom.media.size_downgraded': "{label} boyutu ({size_mb}MB) {cap} sınırını aşıyor; bunun yerine dosya olarak gönderildi.",
    },
    'uk': {
        'platform.wecom.media.kind_image': "Зображення",
        'platform.wecom.media.kind_video': "Відео",
        'platform.wecom.media.kind_voice': "Голосове повідомлення",
        'platform.wecom.media.file_too_large': "Розмір файлу {size_mb} МБ перевищує ліміт WeCom у 20 МБ, тому надіслати його не можна. Спробуйте стиснути файл або зменшити його розмір.",
        'platform.wecom.media.voice_format_downgraded': "Формат голосу {content_type} не підтримується (WeCom приймає лише AMR); надіслано як файл.",
        'platform.wecom.media.size_downgraded': "{label}: розмір {size_mb} МБ перевищує ліміт {cap}; надіслано як файл.",
    },
    'zh': {
        'platform.wecom.media.kind_image': "图片",
        'platform.wecom.media.kind_video': "视频",
        'platform.wecom.media.kind_voice': "语音",
        'platform.wecom.media.file_too_large': "文件大小 {size_mb}MB 超过了企业微信允许的最大限制 20MB，无法发送。请尝试压缩文件或减小文件大小。",
        'platform.wecom.media.voice_format_downgraded': "语音格式 {content_type} 不支持，企微仅支持 AMR 格式，已转为文件格式发送",
        'platform.wecom.media.size_downgraded': "{label}大小 {size_mb}MB 超过 {cap} 限制，已转为文件格式发送",
    },
    'zh-hant': {
        'platform.wecom.media.kind_image': "圖片",
        'platform.wecom.media.kind_video': "影片",
        'platform.wecom.media.kind_voice': "語音訊息",
        'platform.wecom.media.file_too_large': "檔案大小 {size_mb}MB 超過 WeCom 的 20MB 上限，無法傳送。請嘗試壓縮檔案或縮小其大小。",
        'platform.wecom.media.voice_format_downgraded': "不支援語音格式 {content_type}（WeCom 只接受 AMR）；已改以檔案傳送。",
        'platform.wecom.media.size_downgraded': "{label}大小 {size_mb}MB 超過 {cap} 上限；已改以檔案傳送。",
    },
}

# Spellings core's language picker accepted for these packs (``agent.i18n._LANGUAGE_ALIASES``).
_ALIASES = {
    "chinese": "zh", "mandarin": "zh", "zh-cn": "zh", "zh-hans": "zh", "zh-sg": "zh",
    "traditional-chinese": "zh-hant", "traditional_chinese": "zh-hant",
    "zh-tw": "zh-hant", "zh-hk": "zh-hant", "zh-mo": "zh-hant",
}


def _language_setting() -> str:
    from agent.secret_scope import UnscopedSecretError, get_secret
    try:
        value = get_secret("HERMES_LANGUAGE")
    except UnscopedSecretError:  # unscoped default-profile path: the process env is that profile's own value
        value = os.environ.get("HERMES_LANGUAGE")
    if value:
        return str(value)
    try:
        from hermes_cli.config import load_config_readonly
        return str((load_config_readonly().get("display") or {}).get("language") or "")
    except Exception:  # an unreadable config.yaml must never break a reply; core's t() degrades the same way
        logger.debug("Could not read display.language", exc_info=True)
        return ""


def language() -> str:
    """The active profile's language id among the ones this module carries, else ``en``."""
    key = _language_setting().strip().lower().replace("_", "-")
    key = _ALIASES.get(key, key)
    if key in _MESSAGES:
        return key
    base = key.split("-", 1)[0]
    return base if base in _MESSAGES else "en"


def t(key: str, **format_kwargs) -> str:
    """``key`` in the active language (English fallback, then the bare key), ``str.format``-ed."""
    value = _MESSAGES.get(language(), {}).get(key) or _MESSAGES["en"].get(key, key)
    if not format_kwargs:
        return value
    try:
        return value.format(**format_kwargs)
    except (KeyError, IndexError, ValueError):
        return value
