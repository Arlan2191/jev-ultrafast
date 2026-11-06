"""Keys and declared pages. The caller declares them; the model only chooses among what is offered."""

import re
from urllib.parse import urlsplit

# name -> (key, code, windowsVirtualKeyCode, text)
SPECIAL = {
    "Enter": ("Enter", "Enter", 13, "\r"),
    "Escape": ("Escape", "Escape", 27, None),
    "Tab": ("Tab", "Tab", 9, None),
    "ArrowUp": ("ArrowUp", "ArrowUp", 38, None),
    "ArrowDown": ("ArrowDown", "ArrowDown", 40, None),
    "ArrowLeft": ("ArrowLeft", "ArrowLeft", 37, None),
    "ArrowRight": ("ArrowRight", "ArrowRight", 39, None),
    "Space": (" ", "Space", 32, " "),
    "Backspace": ("Backspace", "Backspace", 8, None),
    "PageUp": ("PageUp", "PageUp", 33, None),
    "PageDown": ("PageDown", "PageDown", 34, None),
    "Home": ("Home", "Home", 36, None),
    "End": ("End", "End", 35, None),
}
# Offered to the model on every page, in this order. Shift+Tab is a chord of a fixed key.
FIXED_KEYS = (
    "Enter",
    "Escape",
    "Tab",
    "Shift+Tab",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Space",
    "Backspace",
    "PageUp",
    "PageDown",
    "Home",
    "End",
)
MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}  # CDP Input.dispatchKeyEvent bitmask
FUNCTION_KEY = re.compile(r"F([1-9]|1[0-2])")


def parse_key(chord):
    """Return (modifiers, key) for 'Control+K', 'Shift+Tab' or 'Enter'; raise ValueError on anything else."""
    if type(chord) is not str:
        raise ValueError(f"Invalid key {chord!r}")
    *mods, key = chord.split("+")
    valid_key = key in SPECIAL or (len(key) == 1 and key.isascii() and key.isalnum()) or FUNCTION_KEY.fullmatch(key)
    if not valid_key or len(set(mods)) != len(mods) or any(m not in MODIFIERS for m in mods):
        raise ValueError(f"Invalid key {chord!r}")
    return mods, key


def validate_keys(keys):
    """Caller-declared chords: at least one modifier, and never a bare Shift on a character (that types text)."""
    if isinstance(keys, str) or not isinstance(keys, (list, tuple)):
        raise ValueError("keys must be a sequence of key chords")
    for chord in keys:
        mods, key = parse_key(chord)
        if not mods:
            raise ValueError(f"Invalid key {chord!r}: declare a chord with a modifier, plain keys are already offered")
        if set(mods) == {"Shift"} and len(key) == 1:
            raise ValueError(f"Invalid key {chord!r}: Shift with a character types text")
    return tuple(dict.fromkeys(keys))


def offered_keys(declared):
    return tuple(dict.fromkeys((*FIXED_KEYS, *declared)))


def validate_urls(urls):
    """Caller-declared pages: {name: absolute http(s) URL}, no credentials."""
    if urls is None:
        return {}
    if not isinstance(urls, dict):
        raise ValueError("urls must be a dict of name to URL")
    for name, url in urls.items():
        if type(name) is not str or not name.strip() or type(url) is not str:
            raise ValueError("urls must map non-empty names to URL strings")
        if re.search(r"[\s\x00-\x1f\x7f]", url):
            raise ValueError(f"Invalid URL for {name!r}")
        try:
            parts = urlsplit(url)
            parts.port  # noqa: B018 - raises ValueError on a bad port
        except ValueError:
            raise ValueError(f"Invalid URL for {name!r}") from None
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            raise ValueError(f"Invalid URL for {name!r}: use an absolute http(s) URL without credentials")
    return dict(urls)


def key_events(chord):
    """CDP Input.dispatchKeyEvent parameters for pressing a chord: keyDown (with text if it types), keyUp."""
    mods, key = parse_key(chord)
    modifiers = sum(MODIFIERS[m] for m in mods)
    if key in SPECIAL:
        value, code, vk, text = SPECIAL[key]
    elif FUNCTION_KEY.fullmatch(key):
        value, code, vk, text = key, key, 111 + int(key[1:]), None
    elif key.isdigit():
        value, code, vk, text = key, "Digit" + key, ord(key), key
    else:
        shifted = "Shift" in mods
        value, code, vk = (key.upper() if shifted else key.lower()), "Key" + key.upper(), ord(key.upper())
        text = value
    if text is not None and set(mods) - {"Shift"}:
        text = None  # A shortcut does not type: Control+K must not insert "k".
    down = {"type": "keyDown", "key": value, "code": code, "windowsVirtualKeyCode": vk, "modifiers": modifiers}
    if text is not None:
        down["text"] = text
    up = {k: v for k, v in down.items() if k != "text"} | {"type": "keyUp"}
    return [down, up]
