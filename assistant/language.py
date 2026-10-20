"""Cheap language hint (uz / ru / en) for the latest incoming message.

This is only a hint for Claude; the model makes the final decision.
"""
from __future__ import annotations

import re
from typing import Optional

APOSTROPHES = "'ʻ’‘`"

_UZ_CYRILLIC_LETTERS = set("ўқғҳЎҚҒҲ")
_UZ_CYRILLIC_WORDS = {
    "салом", "ассалому", "алейкум", "рахмат", "раҳмат", "яхши", "нима", "қалесан", "калесан",
    "йўқ", "йук", "бугун", "эртага", "керак", "хоп", "бопти", "ҳозир", "хозир", "кейин",
    "менга", "сенга", "сизга", "қаерда", "каерда", "нимага", "қачон", "качон",
}
_UZ_CYRILLIC_SUFFIXES = ("мисан", "мисиз", "япсан", "япман", "япти", "япмиз", "ясизми")

_UZ_LATIN_WORDS = {
    "salom", "assalomu", "alaykum", "rahmat", "raxmat", "yaxshi", "nima", "qalesan", "qalaysan",
    "qalaysiz", "qayerda", "qayerdasan", "bugun", "ertaga", "kerak", "xop", "bopti", "zor",
    "hozir", "keyin", "men", "sen", "siz", "yoq", "nimaga", "qachon", "uyda", "ketdim",
    "kelaman", "boraman", "qilaman", "bormi", "yoqmi", "kerakmi", "qancha", "qanchadan", "narxi",
    "bor", "necha", "pul", "narx", "qaerda", "qayerga", "qaerga", "buyurtma", "yetkazib", "dostavka",
    "olmoqchiman", "bermoqchiman", "mahsulot", "bormikan", "bo'lsa", "bolsa", "iltimos", "marhamat",
    "ha", "yo'q", "mumkin", "mumkinmi", "arzon", "qimmat", "kafolat", "tekin", "naqd",
}
# Clear English evidence; short Latin text without any of these has no reliable language.
_EN_WORDS = {
    "hi", "hello", "hey", "ok", "okay", "thanks", "thank", "you", "your", "are", "is", "the", "a", "an", "i",
    "do", "does", "have", "how", "much", "what", "where", "when", "which", "who", "why", "price", "free",
    "please", "can", "could", "will", "would", "my", "me", "we", "it", "it's", "in", "on", "at", "for", "to",
    "of", "and", "or", "not", "no", "yes", "there", "this", "that", "with", "want", "need", "tonight",
    "today", "tomorrow", "see", "bye", "good", "morning", "evening", "available", "delivery", "order",
}
_UZ_LATIN_SUFFIXES = (
    "misan", "misiz", "mizmi", "yapsan", "yapman", "yapti", "yapmiz", "asizmi", "ganman", "dingmi",
    "moqchiman", "moqchimiz", "moqchi", "laymi", "larmi",
)

_APOS_CLASS = "[" + re.escape(APOSTROPHES) + "]"
# o' / g' as in "bo'sh", "g'alaba" — but not English "dog's" or "o'clock".
_UZ_DIGRAPH_RE = re.compile(r"[og]" + _APOS_CLASS + r"(?!s\b)(?=[a-z])", re.IGNORECASE)
_OCLOCK_RE = re.compile(r"\bo" + _APOS_CLASS + r"clock\b", re.IGNORECASE)


def _is_cyrillic(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


def _is_latin(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def detect_language(text: str) -> Optional[str]:
    if not text:
        return None
    cyrillic = sum(1 for ch in text if _is_cyrillic(ch))
    latin = sum(1 for ch in text if _is_latin(ch))
    if cyrillic == 0 and latin == 0:
        return None

    if cyrillic > latin:
        if any(ch in _UZ_CYRILLIC_LETTERS for ch in text):
            return "uz"
        words = re.findall(r"[Ѐ-ӿ]+", text.lower())
        if any(w in _UZ_CYRILLIC_WORDS or w.endswith(_UZ_CYRILLIC_SUFFIXES) for w in words):
            return "uz"
        return "ru"

    if _UZ_DIGRAPH_RE.search(_OCLOCK_RE.sub(" ", text)):
        return "uz"
    stripped = re.sub(_APOS_CLASS, "", text.lower())
    for word in re.findall(r"[a-z]+", stripped):
        if word in _UZ_LATIN_WORDS or word.endswith(_UZ_LATIN_SUFFIXES):
            return "uz"
    english = re.findall(r"[a-z']+", text.lower())
    if any(word in _EN_WORDS for word in english):
        return "en"
    return None  # e.g. "Somsa", "Dior parfyum": not enough evidence either way


LANGUAGE_NAMES = {"uz": "Uzbek", "ru": "Russian", "en": "English"}
