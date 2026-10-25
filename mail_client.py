"""IMAP access to mailboxes (Hetzner, iCloud, ...): read mail and create drafts.

Layout of this file: configuration (MailConfig, load_accounts) -> helpers without
network access (folder names, IMAP responses, headers, message text) -> MailClient.

Standard library only. Read access opens folders read-only and fetches messages
with BODY.PEEK so nothing gets marked as read. Nothing is ever sent: drafts are
stored in the drafts folder via IMAP APPEND and sent by the user in their mail client.
"""

from __future__ import annotations

import base64
import logging
import imaplib
import os
import re
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.policy import SMTP, default as default_policy
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterator

log = logging.getLogger("mail-mcp")

MAX_LIMIT = 100
DEFAULT_BODY_CHARS = 20_000
# Never load more than this per message (attachments usually come last).
MAX_FETCH_BYTES = 2 * 1024 * 1024
# Connection reuse: reconnect after POOL_MAX_IDLE seconds, check with NOOP after POOL_CHECK_AFTER.
POOL_MAX_IDLE = 300.0
POOL_CHECK_AFTER = 5.0
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


class MailError(RuntimeError):
    """Error whose message may be returned to Claude."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env parser (KEY=VALUE, # comments, optional quotes)."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    try:
        if path.stat().st_mode & 0o077:
            log.warning("%s is readable by others - please chmod 600.", path)
    except OSError:
        pass
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        values[key.strip()] = val
    return values


def _int(value: str, key: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise MailError(f"{key} must be a number, not {value!r}.") from None


@dataclass(frozen=True)
class MailConfig:
    """One IMAP account. `user` is the login, `address` the own sender address
    (with iCloud the login can differ from the mail name)."""
    imap_host: str
    imap_port: int
    user: str
    password: str
    from_name: str = ""
    drafts_folder: str | None = None
    timeout: int = 30
    aliases: tuple[str, ...] = ()
    name: str = "hetzner"
    address: str = ""

    @property
    def own_address(self) -> str:
        return (self.address or self.user).lower()

    @classmethod
    def from_env(cls, env: dict[str, str], name: str, prefix: str = "") -> "MailConfig":
        def get(key: str, default: str = "") -> str:
            return env.get(prefix + key, default)

        missing = [prefix + k for k in ("MAIL_USER", "MAIL_PASSWORD") if not get(k)]
        if missing:
            raise MailError(f"Account {name!r}: missing configuration {missing}.")
        imap_host = get("IMAP_HOST", DEFAULT_HOSTS.get(name.lower(), ""))
        if not imap_host:
            # Never guess a server: the password would be sent to the wrong provider.
            raise MailError(f"Account {name!r}: {prefix}IMAP_HOST is not set and there is "
                            "no default server for this account name.")
        return cls(
            name=name,
            imap_host=imap_host,
            imap_port=_int(get("IMAP_PORT", "993"), prefix + "IMAP_PORT"),
            user=get("MAIL_USER"),
            password=get("MAIL_PASSWORD"),
            address=get("MAIL_ADDRESS"),
            from_name=get("MAIL_FROM_NAME", env.get("MAIL_FROM_NAME", "")),
            drafts_folder=get("DRAFTS_FOLDER") or None,
            timeout=_int(get("IMAP_TIMEOUT", env.get("IMAP_TIMEOUT", "30")),
                         prefix + "IMAP_TIMEOUT"),
            aliases=tuple(a.strip().lower() for a in get("MAIL_ALIASES").split(",")
                          if a.strip()),
        )

    def sender_addresses(self) -> list[str]:
        return [self.own_address, *(a for a in self.aliases if a != self.own_address)]

    def is_allowed_sender(self, addr: str) -> bool:
        addr = addr.strip().lower()
        if re.search(r"[\x00-\x20\x7f]", addr):  # no control characters or spaces
            return False
        for allowed in self.sender_addresses():
            if allowed.startswith("*@"):
                if addr.endswith(allowed[1:]) and addr.count("@") == 1:
                    return True
            elif addr == allowed:
                return True
        return False


DEFAULT_HOSTS = {
    "hetzner": "mail.your-server.de",
    "icloud": "imap.mail.me.com",
}


def config_path(env_file: str | os.PathLike | None = None) -> Path:
    return Path(env_file or os.environ.get("MAIL_MCP_CONFIG")
                or Path(__file__).with_name("config.env"))


def load_accounts(env_file: str | os.PathLike | None = None) -> dict[str, MailConfig]:
    """Read all accounts from config.env.

    Without ACCOUNTS: one account from the unprefixed keys (MAIL_USER, ...).
    With ACCOUNTS=hetzner,icloud: the first account may keep using the unprefixed
    keys, all others use prefixes such as ICLOUD_MAIL_USER.
    """
    path = config_path(env_file)
    env = {**load_env_file(path), **os.environ}
    names = [n.strip().lower() for n in env.get("ACCOUNTS", "").split(",") if n.strip()]
    if not names:
        names = [env.get("ACCOUNT_NAME", "hetzner").lower()]
    accounts: dict[str, MailConfig] = {}
    for i, name in enumerate(names):
        if not re.fullmatch(r"[a-z0-9_]+", name):
            raise MailError(f"Invalid account name {name!r} (only a-z, 0-9, _).")
        prefix = name.upper() + "_"
        if i == 0 and not env.get(prefix + "MAIL_USER"):
            prefix = ""  # first account with the old unprefixed keys
        try:
            accounts[name] = MailConfig.from_env(env, name, prefix)
        except MailError as e:
            raise MailError(f"{e} (looked in {path} and environment variables)") from e
    return accounts


# --------------------------------------------------------------------------
# IMAP folder names (no network, testable on its own)
# --------------------------------------------------------------------------

def imap_utf7_encode(name: str) -> str:
    """Folder name -> IMAP modified UTF-7 (RFC 3501), e.g. Entwürfe -> Entw&APw-rfe."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            b64 = base64.b64encode("".join(buf).encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            buf.clear()

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(name: str) -> str:
    def repl(m: re.Match) -> str:
        chunk = m.group(1)
        if not chunk:
            return "&"
        chunk = chunk.replace(",", "/")
        chunk += "=" * (-len(chunk) % 4)
        try:
            return base64.b64decode(chunk).decode("utf-16-be")
        except ValueError:  # malformed name from the server: keep it as it is
            return m.group(0)

    return re.sub(r"&([A-Za-z0-9+,]*)-", repl, name)


def quote_mailbox(name: str) -> str:
    encoded = imap_utf7_encode(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


_LIST_RE = re.compile(r'\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.*)$')


def parse_list_line(item) -> dict | None:
    """One line of an IMAP LIST response -> {name, flags, delimiter}."""
    if isinstance(item, tuple):  # name delivered as a literal
        head, literal = item[0].decode(), item[1].decode()
        m = _LIST_RE.match(head.rsplit("{", 1)[0].strip() + " X")
        name = literal
    else:
        if item is None:
            return None
        m = _LIST_RE.match(item.decode())
        name = m.group("name").strip() if m else ""
        if len(name) >= 2 and name.startswith('"') and name.endswith('"'):
            name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if not m:
        return None
    delim = m.group("delim")
    return {
        "name": imap_utf7_decode(name),
        "flags": m.group("flags").split(),
        "delimiter": None if delim == "NIL" else delim.strip('"'),
    }


# --------------------------------------------------------------------------
# Parsing IMAP responses and validating input
# --------------------------------------------------------------------------

def _check(typ: str, data, what: str):
    """Return `data` if the server answered OK, otherwise raise MailError."""
    if typ != "OK":
        detail = b" ".join(d for d in data if isinstance(d, bytes)).decode(errors="replace")
        raise MailError(f"{what} failed: {detail or typ}")
    return data


# Control characters (CR/LF above all) must never reach an IMAP command: imaplib
# does not check for them, and injected lines would be further commands.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


# Metadata of a FETCH response; the keys are the names used in _parse_meta().
_FETCH_META = {
    "uid": re.compile(rb"UID (\d+)"),
    "size": re.compile(rb"RFC822\.SIZE (\d+)"),
    "flags": re.compile(rb"FLAGS \(([^)]*)\)"),
}


def _parse_meta(raw: bytes, meta: dict) -> None:
    for key, out in (("uid", "uid"), ("size", "size_bytes"), ("flags", "flags")):
        if out in meta:
            continue
        if m := _FETCH_META[key].search(raw):
            meta[out] = (m.group(1).decode().split() if key == "flags"
                         else int(m.group(1)))


def parse_fetch(data: list) -> list[tuple[dict, bytes]]:
    """imaplib FETCH response -> [(meta, literal_bytes)].

    Some servers (e.g. iCloud/Cyrus) send UID/FLAGS only after the literal;
    imaplib returns that as a following bytes element."""
    results = []
    for i, item in enumerate(data):
        if not isinstance(item, tuple):
            continue
        meta: dict = {}
        _parse_meta(item[0], meta)
        if i + 1 < len(data) and isinstance(data[i + 1], bytes):
            _parse_meta(data[i + 1], meta)
        results.append((meta, item[1]))
    return results


def _search_result_uids(data) -> set[int]:
    """SEARCH response -> set of UIDs; imaplib sometimes returns [None] when nothing matches."""
    return {int(x) for x in (data[0] or b"").split()} if data else set()


def _status_numbers(raw: str) -> dict:
    out = {}
    if m := re.search(r"MESSAGES (\d+)", raw):
        out["messages"] = int(m.group(1))
    if m := re.search(r"UNSEEN (\d+)", raw):
        out["unseen"] = int(m.group(1))
    return out


# --------------------------------------------------------------------------
# Message content: headers, text, attachments, addresses
# --------------------------------------------------------------------------

def _header(msg: Message, name: str) -> str:
    """Header value as text ("" if missing).

    Malformed headers (e.g. a broken Message-ID) make the parser of the default
    policy raise; that must not abort reading the message."""
    try:
        value = msg.get(name)
    except Exception:
        value = next((v for k, v in msg.raw_items() if k.lower() == name.lower()), "")
    return "" if value is None else str(value)


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decoded_header(msg: Message, name: str) -> str:
    """Header with MIME encodings (=?utf-8?...?=) resolved."""
    return decode_mime_header(_header(msg, name))


def imap_date(iso_date: str) -> str:
    """'2026-10-01' -> '01-Oct-2026' (independent of the locale)."""
    try:
        dt = date.fromisoformat(iso_date)
    except ValueError as e:
        raise MailError(f"Invalid date {iso_date!r}, expected YYYY-MM-DD.") from e
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


# Elements without content or end tag.
_VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
              "meta", "source", "track", "wbr"}
# Elements whose end tag may be omitted; a new one of the same kind closes the previous.
_OPTIONAL_END_TAGS = {"p", "li", "tr", "td", "th", "dt", "dd", "option"}
# Elements that may appear inside <head>; anything else implicitly ends it.
_HEAD_TAGS = {"title", "meta", "link", "style", "script", "base", "noscript", "template"}
# Elements whose content is never shown to the reader.
_NEVER_SHOWN_TAGS = {"script", "style", "head", "template"}

# Inline styles that hide an element and everything in it from the reader, but not from
# a parser (also used for invisible prompt injection and "preheader" hacks). Matched
# against a normalized style string (lowercase, no whitespace, no !important).
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)(?:display:none|visibility:(?:hidden|collapse)|mso-hide:all|opacity:0(?![.\d]))")
_COLLAPSED_STYLE_RE = re.compile(r"(?:^|;)max-height:0(?:px|pt|em|rem|%)?(?=;|$)")
# font-size:0 is also used on layout wrappers whose children set their own size, so
# it only hides the element's own text, not its descendants.
_ZERO_FONT_RE = re.compile(r"(?:^|;)font-size:0(?:px|pt|em|rem|%)?(?=;|$)")


def _normalized_style(attrs) -> str:
    return re.sub(r"\s+|!important", "", (dict(attrs).get("style") or "").lower())


def _is_hidden(attrs) -> bool:
    if "hidden" in dict(attrs):
        return True
    style = _normalized_style(attrs)
    return bool(_HIDDEN_STYLE_RE.search(style)
                or (_COLLAPSED_STYLE_RE.search(style) and "overflow:hidden" in style))


class _HTMLToText(HTMLParser):
    """Converts HTML to text, leaving out what a reader would not see.

    Keeps a stack of open elements so that hidden content (display:none, script,
    style, head, ...) is dropped together with everything nested in it, and so that
    missing end tags (common in mail HTML) cannot swallow the rest of the message.
    """

    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
              "blockquote", "table", "section", "article", "header", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        # open elements: (tag, hidden, own text hidden)
        self._open: list[tuple[str, bool, bool]] = []
        self._hidden = 0  # number of hidden elements on the stack
        self._in_head = False

    def _pop_to(self, index: int) -> None:
        """Close the open element at `index` and everything opened after it."""
        for tag, hidden, _ in self._open[index:]:
            self._hidden -= hidden
            if tag == "head":
                self._in_head = False
        del self._open[index:]

    def handle_starttag(self, tag, attrs):
        if self._in_head and tag not in _HEAD_TAGS and tag != "head":
            # <body> or any content element ends a head whose </head> is missing.
            self._pop_to(next(i for i, (t, _, _) in enumerate(self._open) if t == "head"))
        hidden = tag in _NEVER_SHOWN_TAGS or _is_hidden(attrs)
        if self._hidden == 0 and not hidden:
            if tag in self._BLOCK:
                self.parts.append("\n")
            if tag == "li":
                self.parts.append("- ")
        if tag in _VOID_TAGS:
            return
        if tag in _OPTIONAL_END_TAGS and self._open and self._open[-1][0] == tag:
            self._pop_to(len(self._open) - 1)
        self._open.append((tag, hidden, bool(_ZERO_FONT_RE.search(_normalized_style(attrs)))))
        self._hidden += hidden
        if tag == "head":
            self._in_head = True

    def handle_endtag(self, tag):
        index = next((i for i in range(len(self._open) - 1, -1, -1)
                      if self._open[i][0] == tag), None)
        if index is None:
            return  # stray end tag
        self._pop_to(index)
        if self._hidden == 0 and tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._hidden == 0 and not (self._open and self._open[-1][2]):
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLToText()
    parser.feed(html)
    parser.close()
    text = "".join(parser.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _attachment_size(part: Message) -> int:
    """Size of the decoded attachment. For base64 it is computed from the text length
    without decoding the attachment (saves memory and time for large files)."""
    if str(part.get("Content-Transfer-Encoding", "")).strip().lower() == "base64":
        data = "".join(str(part.get_payload()).split())
        return len(data) // 4 * 3 - data[-2:].count("=")
    return len(part.get_payload(decode=True) or b"")


def extract_body(msg: Message) -> tuple[str, list[dict]]:
    """Message text (text/plain preferred, otherwise HTML converted to text) + attachments."""
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if disposition == "attachment" or (filename and not ctype.startswith("text/")):
            attachments.append({
                "filename": decode_mime_header(filename) or "(unnamed)",
                "content_type": ctype,
                "size_bytes": _attachment_size(part),
            })
            continue
        if ctype not in ("text/plain", "text/html"):
            continue
        try:
            text = part.get_content()
        except Exception:
            raw = part.get_payload(decode=True) or b""
            text = raw.decode(part.get_content_charset() or "utf-8", errors="replace")
        (plain if ctype == "text/plain" else html).append(text)
    if plain:
        body = "\n\n".join(t.strip() for t in plain)
    elif html:
        body = "\n\n".join(html_to_text(t) for t in html)
    else:
        body = ""
    return body, attachments


def _quote_original(body: str, original: Message) -> str:
    """Reply text + quoted original ("> ..." under an "Am ... schrieb ..." line).

    The attribution line stays German on purpose: it becomes part of the outgoing mail."""
    original_body, _ = extract_body(original)
    quoted = "\n".join("> " + line for line in original_body.splitlines())
    return (f"{body.rstrip()}\n\nAm {_header(original, 'Date')} schrieb "
            f"{_decoded_header(original, 'From')}:\n{quoted}\n")


def _clean_addresses(value: str | list[str] | None) -> str:
    """Address list -> header value "Name <addr>, ..."; rejects addresses without "@"
    or with a line break (header injection)."""
    if not value:
        return ""
    items = value if isinstance(value, list) else [value]
    addrs = getaddresses([str(v) for v in items])
    out = []
    for name, addr in addrs:
        if not addr or "@" not in addr or any(c in addr for c in "\r\n"):
            raise MailError(f"Invalid address: {name or addr!r}")
        out.append(formataddr((name, addr)))
    return ", ".join(out)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------

class MailClient:
    def __init__(self, config: MailConfig):
        self.cfg = config
        self._drafts_cache: str | None = config.drafts_folder
        self._lock = threading.Lock()
        self._conn: imaplib.IMAP4_SSL | None = None
        self._last_used = 0.0

    def _open(self) -> imaplib.IMAP4_SSL:
        try:
            conn = imaplib.IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port,
                                     ssl_context=ssl.create_default_context(),
                                     timeout=self.cfg.timeout)
        except (OSError, imaplib.IMAP4.error) as e:
            raise MailError(f"Connection to {self.cfg.imap_host}:{self.cfg.imap_port} "
                            f"failed: {e}") from e
        try:
            conn.login(self.cfg.user, self.cfg.password)
        except imaplib.IMAP4.error as e:
            self._logout(conn)
            raise MailError("IMAP login failed (check user/password).") from e
        except OSError as e:
            self._logout(conn)
            raise MailError(f"Network error at {self.cfg.imap_host}: {e}") from e
        return conn

    @staticmethod
    def _logout(conn) -> None:
        try:
            conn.logout()
        except Exception:
            pass

    def _drop(self) -> None:
        if self._conn is not None:
            self._logout(self._conn)
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self._drop()

    def _reusable(self) -> bool:
        idle = time.monotonic() - self._last_used
        if self._conn is None or idle > POOL_MAX_IDLE:
            return False
        if idle > POOL_CHECK_AFTER:
            try:
                return self._conn.noop()[0] == "OK"
            except Exception:
                return False
        return True

    @contextmanager
    def _connect(self) -> Iterator[imaplib.IMAP4_SSL]:
        """One connection per account, reused across calls (serialized by a lock).

        It is discarded after a protocol or network error."""
        with self._lock:
            if not self._reusable():
                self._drop()
                self._conn = self._open()
            try:
                yield self._conn
            except MailError:
                raise  # connection is fine (e.g. folder not found)
            except imaplib.IMAP4.error as e:
                self._drop()
                raise MailError(f"IMAP error: {e}") from e
            except OSError as e:  # timeout, TLS error, connection reset
                self._drop()
                raise MailError(f"Network error at {self.cfg.imap_host}: {e}") from e
            except BaseException:
                self._drop()
                raise
            finally:
                self._last_used = time.monotonic()

    def _select(self, conn, folder: str, readonly: bool = True) -> int:
        typ, data = conn.select(quote_mailbox(folder), readonly=readonly)
        if typ != "OK":
            raise MailError(f"Folder {folder!r} not found. Use list_folders "
                            "to get the exact names.")
        return int(data[0] or 0)

    # ---- Folders ---------------------------------------------------------

    def list_folders(self) -> list[dict]:
        with self._connect() as conn:
            data = _check(*conn.list(), "LIST")
            folders = [f for f in (parse_list_line(x) for x in data) if f]
            for f in folders:
                if "\\Noselect" in f["flags"] or "\\NonExistent" in f["flags"]:
                    continue
                typ, st = conn.status(quote_mailbox(f["name"]), "(MESSAGES UNSEEN)")
                if typ == "OK" and st and st[0]:
                    f.update(_status_numbers(st[0].decode(errors="replace")))
            return folders

    def _drafts_folder(self, conn) -> str:
        if self._drafts_cache:
            return self._drafts_cache
        data = _check(*conn.list(), "LIST")
        folders = [f for f in (parse_list_line(x) for x in data) if f]
        chosen = next((f["name"] for f in folders if "\\Drafts" in f["flags"]), None)
        if not chosen:
            names = {f["name"].lower(): f["name"] for f in folders}
            for cand in ("drafts", "inbox.drafts", "entwürfe", "inbox.entwürfe"):
                if cand in names:
                    chosen = names[cand]
                    break
        if not chosen:
            raise MailError("Drafts folder not found. Set DRAFTS_FOLDER in config.env.")
        self._drafts_cache = chosen
        return chosen

    # ---- Searching and listing ---------------------------------------------

    def _summaries(self, conn, uids: list[int] | None = None,
                   seq_range: str | None = None) -> list[dict]:
        """Header data by UID list or by sequence range (e.g. "1981:*")."""
        fields = "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE)])"
        if seq_range:
            typ, data = conn.fetch(seq_range, fields)
        elif uids:
            typ, data = conn.uid("FETCH", ",".join(str(u) for u in uids), fields)
        else:
            return []
        _check(typ, data, "FETCH")
        out = []
        for meta, raw in parse_fetch(data):
            hdr = message_from_bytes(raw, policy=default_policy)
            flags = meta.get("flags", [])
            out.append({
                "uid": meta.get("uid"),
                "date": _header(hdr, "Date"),
                "from": _decoded_header(hdr, "From"),
                "to": _decoded_header(hdr, "To"),
                "subject": _decoded_header(hdr, "Subject"),
                "unread": "\\Seen" not in flags,
                "flagged": "\\Flagged" in flags,
                "answered": "\\Answered" in flags,
                "size_bytes": meta.get("size_bytes"),
            })
        out.sort(key=lambda m: m["uid"] or 0, reverse=True)
        return out

    def _search(self, conn, ascii_criteria: list[str],
                text_criteria: list[tuple[str, str]]) -> list[int]:
        """Run SEARCH. Non-ASCII terms go to the server one by one as UTF-8 literals;
        the resulting UID sets are intersected afterwards."""
        sets: list[set[int]] = []
        for key, value in text_criteria:
            if _CONTROL_RE.search(value):
                raise MailError(f"Invalid control characters in the search term for {key}.")
            if value.isascii():
                ascii_criteria.append(f'{key} "{value.replace(chr(92), "").replace(chr(34), "")}"')
            else:
                conn.literal = value.encode("utf-8")
                typ, data = conn.uid("SEARCH", "CHARSET", "UTF-8", key)
                _check(typ, data, "SEARCH")
                sets.append(_search_result_uids(data))
        if ascii_criteria or not sets:
            typ, data = conn.uid("SEARCH", *(ascii_criteria or ["ALL"]))
            _check(typ, data, "SEARCH")
            sets.append(_search_result_uids(data))
        result = set.intersection(*sets)
        return sorted(result)

    def list_messages(self, folder: str = "INBOX", limit: int = 20,
                      unread_only: bool = False) -> dict:
        limit = max(1, min(limit, MAX_LIMIT))
        with self._connect() as conn:
            total = self._select(conn, folder)
            if not unread_only:  # no SEARCH needed: fetch the latest messages by sequence number
                msgs = (self._summaries(conn, seq_range=f"{max(1, total - limit + 1)}:*")
                        if total else [])
                return {"folder": folder, "total_in_folder": total,
                        "matching": total, "messages": msgs}
            uids = self._search(conn, ["UNSEEN"], [])
            return {"folder": folder, "total_in_folder": total,
                    "matching": len(uids),
                    "messages": self._summaries(conn, uids[-limit:])}

    def search_messages(self, folder: str = "INBOX", *, sender: str | None = None,
                        recipient: str | None = None, subject: str | None = None,
                        text: str | None = None, since: str | None = None,
                        before: str | None = None, unread_only: bool = False,
                        limit: int = 20) -> dict:
        limit = max(1, min(limit, MAX_LIMIT))
        ascii_criteria: list[str] = []
        if since:
            ascii_criteria.append(f"SINCE {imap_date(since)}")
        if before:
            ascii_criteria.append(f"BEFORE {imap_date(before)}")
        if unread_only:
            ascii_criteria.append("UNSEEN")
        text_criteria = [(k, v.strip()) for k, v in (
            ("FROM", sender), ("TO", recipient), ("SUBJECT", subject), ("TEXT", text))
            if v and v.strip()]
        with self._connect() as conn:
            self._select(conn, folder)
            uids = self._search(conn, ascii_criteria, text_criteria)
            return {"folder": folder, "matching": len(uids),
                    "messages": self._summaries(conn, uids[-limit:])}

    # ---- Single message ---------------------------------------------

    def _fetch_full(self, conn, folder: str, uid: int) -> tuple[dict, EmailMessage]:
        """Fetch at most MAX_FETCH_BYTES; meta["truncated"] says whether it was cut off."""
        self._select(conn, folder)
        typ, data = conn.uid("FETCH", str(int(uid)),
                             f"(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.{MAX_FETCH_BYTES}>)")
        _check(typ, data, "FETCH")
        parsed = parse_fetch(data)
        if not parsed:
            raise MailError(f"No message with UID {uid} in {folder!r}.")
        meta, raw = parsed[0]
        meta["truncated"] = meta.get("size_bytes", len(raw)) > len(raw)
        return meta, message_from_bytes(raw, policy=default_policy)

    def read_message(self, uid: int, folder: str = "INBOX",
                     max_chars: int = DEFAULT_BODY_CHARS) -> dict:
        max_chars = max(500, min(max_chars, 200_000))
        with self._connect() as conn:
            meta, msg = self._fetch_full(conn, folder, uid)
        body, attachments = extract_body(msg)
        truncated = len(body) > max_chars
        return {
            "uid": uid,
            "folder": folder,
            "date": _header(msg, "Date"),
            "from": _decoded_header(msg, "From"),
            "reply_to": _decoded_header(msg, "Reply-To"),
            "to": _decoded_header(msg, "To"),
            "cc": _decoded_header(msg, "Cc"),
            "subject": _decoded_header(msg, "Subject"),
            "message_id": _header(msg, "Message-ID"),
            "unread": "\\Seen" not in meta.get("flags", []),
            "attachments": attachments,
            "body": body[:max_chars],
            "body_truncated": truncated,
            "size_bytes": meta.get("size_bytes"),
            "message_truncated": meta["truncated"],
        }

    # ---- Drafts -------------------------------------------------------

    def create_draft(self, to: str | list[str] | None, subject: str, body: str, *,
                     cc: str | list[str] | None = None,
                     bcc: str | list[str] | None = None,
                     reply_to_uid: int | None = None,
                     reply_folder: str = "INBOX",
                     quote_original: bool = True,
                     from_address: str | None = None) -> dict:
        thread_headers: dict[str, str] = {}
        with self._connect() as conn:
            if reply_to_uid is not None:
                _, original = self._fetch_full(conn, reply_folder, reply_to_uid)
                reply = self._reply_defaults(original)
                thread_headers = reply["thread_headers"]
                to = to or reply["to"]
                subject = subject or reply["subject"]
                from_address = from_address or reply["from_address"]
                if quote_original:
                    body = _quote_original(body, original)

            sender = (from_address or self.cfg.own_address).strip()
            msg = self._build_draft(sender, to, cc, bcc, subject, body, thread_headers)
            folder, saved_uid = self._append_draft(conn, msg)
        return {
            "saved_in": folder,
            "from": sender,
            "uid": saved_uid,
            "to": str(msg["To"]),
            "subject": str(msg["Subject"]),
            "note": "Saved as a draft only, not sent.",
        }

    def _reply_defaults(self, original: Message) -> dict:
        """Defaults for a reply: thread headers, recipient, "Re:" subject, sender."""
        thread_headers = {}
        original_id = _header(original, "Message-ID").strip()
        if original_id:
            references = _header(original, "References").strip()
            thread_headers = {"In-Reply-To": original_id,
                              "References": f"{references} {original_id}".strip()}
        original_subject = _decoded_header(original, "Subject")
        has_reply_prefix = re.match(r"(?i)^(re|aw):", original_subject)
        return {
            "thread_headers": thread_headers,
            "to": _header(original, "Reply-To") or _header(original, "From"),
            "subject": original_subject if has_reply_prefix else f"Re: {original_subject}",
            "from_address": self._matching_alias(original),
        }

    def _build_draft(self, sender: str, to, cc, bcc, subject: str, body: str,
                     thread_headers: dict[str, str]) -> EmailMessage:
        if not self.cfg.is_allowed_sender(sender):
            raise MailError(f"Sender {sender!r} is not permitted. Allowed: "
                            + ", ".join(self.cfg.sender_addresses()))
        to_header = _clean_addresses(to)
        if not to_header:
            raise MailError("No recipient given.")

        msg = EmailMessage()
        try:
            msg["Date"] = formatdate(localtime=True)
            for name, value in thread_headers.items():
                msg[name] = value
            msg["From"] = formataddr((self.cfg.from_name, sender))
            msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
            msg["To"] = to_header
            if cc_header := _clean_addresses(cc):
                msg["Cc"] = cc_header
            if bcc_header := _clean_addresses(bcc):
                msg["Bcc"] = bcc_header
            # German on purpose: it becomes part of the outgoing mail.
            msg["Subject"] = subject or "(kein Betreff)"
            msg.set_content(body)
        except ValueError as e:  # e.g. a line break in a header value
            raise MailError(f"Invalid value in the draft: {e}") from e
        return msg

    def _append_draft(self, conn, msg: EmailMessage) -> tuple[str, int | None]:
        """Store the message in the drafts folder -> (folder, UID or None)."""
        folder = self._drafts_folder(conn)
        typ, data = conn.append(quote_mailbox(folder), "(\\Draft \\Seen)",
                                imaplib.Time2Internaldate(time.time()),
                                msg.as_bytes(policy=SMTP))
        _check(typ, data, "Entwurf speichern (APPEND)")
        response = data[0].decode(errors="replace") if data and data[0] else ""
        m = re.search(r"APPENDUID \d+ (\d+)", response)  # only with UIDPLUS
        return folder, int(m.group(1)) if m else None

    def _matching_alias(self, orig: Message) -> str | None:
        """Own address the original mail was sent to, if any.

        To/Cc first: Delivered-To often names only the mailbox itself, even when
        the mail went to an alias."""
        for headers in (("To", "Cc"), ("X-Original-To", "Delivered-To")):
            fields = []
            for h in headers:
                fields.extend(str(v) for v in (orig.get_all(h) or []))
            for _, addr in getaddresses(fields):
                if addr and self.cfg.is_allowed_sender(addr):
                    return addr.lower()
        return None

    def check_connection(self) -> dict:
        with self._connect() as conn:
            typ, caps = conn.capability()
            drafts = self._drafts_folder(conn)
        return {"account": self.cfg.name, "host": self.cfg.imap_host, "user": self.cfg.user,
                "drafts_folder": drafts,
                "sender_addresses": self.cfg.sender_addresses(),
                "capabilities": caps[0].decode().split() if typ == "OK" else []}
