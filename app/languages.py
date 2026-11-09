"""ISO 639-1 language codes accepted by OpenSubtitles searches."""

LANGUAGE_CODES = frozenset("""aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu""".split())
DEFAULT_TARGET_LANGUAGES = ("en",)


def normalize_languages(values):
    if values is None:
        return list(DEFAULT_TARGET_LANGUAGES)
    if not isinstance(values, (list, tuple, set)) or not values:
        raise ValueError("Select at least one target language.")
    normalized = []
    for value in values:
        code = str(value).strip().lower()
        if code not in LANGUAGE_CODES:
            raise ValueError(f"Unsupported target language: {code or 'empty'}.")
        if code not in normalized:
            normalized.append(code)
    return normalized


_LANGUAGE_ALIASES = {
    "eng": "en", "fra": "fr", "fre": "fr", "deu": "de", "ger": "de",
    "spa": "es", "ita": "it", "por": "pt", "nld": "nl", "dut": "nl",
    "zho": "zh", "chi": "zh", "jpn": "ja", "kor": "ko", "rus": "ru",
    "ara": "ar", "hin": "hi", "pol": "pl", "swe": "sv", "nor": "no",
    "dan": "da", "fin": "fi", "tur": "tr", "ces": "cs", "cze": "cs",
    "ell": "el", "gre": "el", "ron": "ro", "rum": "ro", "hun": "hu",
    "ukr": "uk", "heb": "he", "tha": "th", "vie": "vi", "ind": "id",
    "msa": "ms", "may": "ms", "hrv": "hr", "srp": "sr", "bul": "bg",
    "slk": "sk", "slo": "sk", "slv": "sl", "est": "et", "lav": "lv",
    "lit": "lt", "cat": "ca", "isl": "is", "ice": "is", "fas": "fa",
    "per": "fa", "ben": "bn", "tam": "ta", "tel": "te", "urd": "ur",
}


def canonical_language(value):
    code = str(value or "").strip().lower().replace("_", "-")
    if "-" in code:
        code = code.split("-", 1)[0]
    return _LANGUAGE_ALIASES.get(code, code)


def language_matches(detected, expected):
    return canonical_language(detected) == canonical_language(expected)
