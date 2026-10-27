"""Offline tests without network and without the MCP SDK: python -m unittest discover tests

German strings in fixtures (umlauts, "Angebot") are deliberate test data for non-ASCII handling."""

import imaplib
import os
import sys
import tempfile
import unittest
from email.message import EmailMessage
from email.policy import default
from email import message_from_bytes
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import mail_client as mc  # noqa: E402


class Utf7Tests(unittest.TestCase):
    def test_roundtrip(self):
        for name in ["INBOX", "Entwürfe", "Gelöschte Elemente", "A&B", "日本語", "INBOX.Rechnungen 2026"]:
            enc = mc.imap_utf7_encode(name)
            self.assertTrue(enc.isascii())
            self.assertEqual(mc.imap_utf7_decode(enc), name)

    def test_known_value(self):
        self.assertEqual(mc.imap_utf7_encode("Entwürfe"), "Entw&APw-rfe")
        self.assertEqual(mc.imap_utf7_encode("A&B"), "A&-B")


class ListParsingTests(unittest.TestCase):
    def test_quoted(self):
        f = mc.parse_list_line(b'(\\HasNoChildren \\Drafts) "." "INBOX.Drafts"')
        self.assertEqual(f["name"], "INBOX.Drafts")
        self.assertIn("\\Drafts", f["flags"])
        self.assertEqual(f["delimiter"], ".")

    def test_unquoted_utf7(self):
        f = mc.parse_list_line(b'(\\HasNoChildren) "/" Entw&APw-rfe')
        self.assertEqual(f["name"], "Entwürfe")

    def test_literal(self):
        f = mc.parse_list_line((b'(\\HasNoChildren) "." {11}', b'Sp"ecial x'))
        self.assertEqual(f["name"], 'Sp"ecial x')


class FetchParsingTests(unittest.TestCase):
    def test_parse(self):
        data = [(b'1 (UID 42 FLAGS (\\Seen \\Answered) RFC822.SIZE 1234 BODY[HEADER.FIELDS (FROM)] {20}',
                 b"From: a@b.de\r\n\r\n"), b")"]
        (meta, raw), = mc.parse_fetch(data)
        self.assertEqual(meta["uid"], 42)
        self.assertEqual(meta["size_bytes"], 1234)
        self.assertEqual(meta["flags"], ["\\Seen", "\\Answered"])


class BodyTests(unittest.TestCase):
    def test_prefers_plain_and_lists_attachments(self):
        m = EmailMessage()
        m["Subject"] = "=?utf-8?q?Gr=C3=BC=C3=9Fe?="
        m.set_content("Hallo Welt")
        m.add_alternative("<p>Hallo <b>HTML</b></p>", subtype="html")
        m.add_attachment(b"%PDF-1.4", maintype="application", subtype="pdf", filename="Rechnung.pdf")
        msg = message_from_bytes(m.as_bytes(), policy=default)
        body, att = mc.extract_body(msg)
        self.assertEqual(body, "Hallo Welt")
        self.assertEqual(att[0]["filename"], "Rechnung.pdf")
        self.assertEqual(mc.decode_mime_header(m["Subject"]), "Grüße")

    def test_attachment_size_matches_decoded_size(self):
        for n in (0, 1, 2, 3, 4, 100, 1000, 1001):
            m = EmailMessage()
            m.set_content("x")
            m.add_attachment((bytes(range(256)) * 5)[:n], maintype="application", subtype="octet-stream",
                             filename="a.bin")
            msg = message_from_bytes(m.as_bytes(), policy=default)
            _, att = mc.extract_body(msg)
            self.assertEqual(att[0]["size_bytes"], n, n)

    def test_html_only(self):
        m = EmailMessage()
        m.set_content("<html><head><style>x{}</style></head><body><h1>Titel</h1>"
                      "<p>Absatz &amp; mehr</p><ul><li>eins</li></ul></body></html>", subtype="html")
        body, _ = mc.extract_body(message_from_bytes(m.as_bytes(), policy=default))
        self.assertIn("Titel", body)
        self.assertIn("Absatz & mehr", body)
        self.assertIn("- eins", body)
        self.assertNotIn("x{}", body)


class HardeningTests(unittest.TestCase):
    def test_sender_with_control_characters_is_refused(self):
        cfg = mc.MailConfig("h", 993, "user@example.com", "pw", aliases=("*@example.com",))
        for addr in ("a\nb@example.com", "a b@example.com", "a\x00@example.com", "a\r@example.com"):
            with self.subTest(repr(addr)):
                self.assertFalse(cfg.is_allowed_sender(addr))
        self.assertTrue(cfg.is_allowed_sender(" User@Example.com "))

    def test_malformed_utf7_from_server_is_kept(self):
        for name in ("&A-", "&2AA-", "&!!!-"):
            with self.subTest(name):
                self.assertEqual(mc.imap_utf7_decode(name), name)
        self.assertEqual(mc.imap_utf7_decode("Entw&APw-rfe"), "Entwürfe")

    def test_folder_listing_survives_malformed_names(self):
        line = b'(\\HasNoChildren) "." "&A-"'
        self.assertEqual(mc.parse_list_line(line)["name"], "&A-")

    def test_line_break_in_draft_header_is_a_mail_error(self):
        client = mc.MailClient(mc.MailConfig("h", 993, "user@example.com", "pw"))
        for subject in ("Hi\r\nBcc: evil@x.de", "a\nb"):
            with self.subTest(repr(subject)):
                with self.assertRaises(mc.MailError) as cm:
                    client._build_draft("user@example.com", ["a@b.de"], None, None,
                                        subject, "body", {})
                self.assertIn("Invalid value", str(cm.exception))

    def test_ordinary_draft_still_builds(self):
        client = mc.MailClient(mc.MailConfig("h", 993, "user@example.com", "pw"))
        msg = client._build_draft("user@example.com", ["a@b.de"], None, None, "Hi", "body", {})
        self.assertEqual(msg["Subject"], "Hi")


class HtmlToTextTests(unittest.TestCase):
    def test_hidden_content_is_dropped(self):
        for label, html in {
            "display:none": '<div style="display:none">IGNORE</div>',
            "important and spaces": '<div style="display: none !important">IGNORE</div>',
            "nested": '<div style="display:none"><p>IGNORE</p><span>IGNORE</span></div>',
            "hidden attribute": "<p hidden>IGNORE</p>",
            "visibility": '<span style="visibility:hidden">IGNORE</span>',
            "opacity zero": '<span style="opacity:0">IGNORE</span>',
            "collapsed preheader": '<div style="max-height:0;overflow:hidden">IGNORE</div>',
            "mso-hide": '<span style="mso-hide:all">IGNORE</span>',
            "zero font on own text": '<div style="font-size:0px">IGNORE</div>',
            "script": "<script>IGNORE</script>",
            "unclosed optional-end element": '<p style="display:none">IGNORE<p>',
        }.items():
            with self.subTest(label):
                self.assertEqual(mc.html_to_text(html + "Hello"), "Hello")

    def test_visible_content_is_kept(self):
        for label, html in {
            "opacity below one": '<span style="opacity:0.5">Hello</span>',
            "collapsed without overflow": '<div style="max-height:0">Hello</div>',
            "zero-font layout wrapper": '<div style="font-size:0"><div style="font-size:16px">Hello</div></div>',
            "small font": '<p style="font-size:0.9em">Hello</p>',
        }.items():
            with self.subTest(label):
                self.assertEqual(mc.html_to_text(html), "Hello")

    def test_missing_end_head_does_not_swallow_body(self):
        html = "<html><head><title>x</title><body><p>Hello</p></body></html>"
        self.assertEqual(mc.html_to_text(html), "Hello")

    def test_head_ended_by_closing_html(self):
        self.assertEqual(mc.html_to_text("<html><head><title>x</title></html><p>Hello</p>"), "Hello")

    def test_head_content_is_not_shown(self):
        html = "<head><title>Title</title><style>p{}</style></head><body><p>Hello</p></body>"
        self.assertEqual(mc.html_to_text(html), "Hello")

    def test_unclosed_style_hides_rest_like_a_browser(self):
        self.assertEqual(mc.html_to_text("<p>before</p><style>p{}<p>after</p>"), "before")


class MiscTests(unittest.TestCase):
    def test_imap_date(self):
        self.assertEqual(mc.imap_date("2026-03-05"), "05-Mar-2026")
        with self.assertRaises(mc.MailError):
            mc.imap_date("05.03.2026")

    def test_addresses(self):
        self.assertEqual(mc._clean_addresses(["Anna <anna@x.de>", "b@y.de"]), "Anna <anna@x.de>, b@y.de")
        with self.assertRaises(mc.MailError):
            mc._clean_addresses("kein-at-zeichen")

    def test_env_file(self):
        p = Path(__file__).with_name("_tmp.env")
        p.write_text('# c\nMAIL_USER=a@b.de\nMAIL_PASSWORD="p w#1"\n', encoding="utf-8")
        try:
            env = mc.load_env_file(p)
            self.assertEqual(env, {"MAIL_USER": "a@b.de", "MAIL_PASSWORD": "p w#1"})
        finally:
            p.unlink()


class FakeIMAP:
    """Simulates the imaplib calls that MailClient uses."""

    def __init__(self, messages):
        self.messages = messages  # uid -> bytes
        self.appended = []
        self.literal = None
        self.searches = []

    capabilities = ()

    def login(self, u, p): return "OK", [b"ok"]
    def logout(self):
        self.logged_out = True
        return "BYE", []
    def noop(self): return "OK", [b"done"]
    def capability(self): return "OK", [b"IMAP4rev1 UIDPLUS SPECIAL-USE"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "." INBOX',
                      b'(\\HasNoChildren \\Drafts) "." "Entw&APw-rfe"']

    def status(self, box, what): return "OK", [box.encode() + b" (MESSAGES 2 UNSEEN 1)"]
    def select(self, box, readonly=False):
        self.readonly = readonly
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            self.searches.append((args, self.literal))
            self.literal = None
            if args[0] == "UID":
                found = [u for u in map(int, args[1].split(",")) if u in self.messages]
                return "OK", [" ".join(map(str, found)).encode() or None]
            return "OK", [" ".join(str(u) for u in self.messages).encode()]
        if cmd == "STORE":
            assert not self.readonly, "STORE on a read-only folder"
            self.stored = args
            return "OK", [None]
        if cmd == "FETCH":
            out = []
            for u in args[0].split(","):
                if int(u) not in self.messages:
                    continue
                raw = self.messages[int(u)]
                part = raw
                if args[1].count("<0."):
                    part = raw[:int(args[1].split("<0.")[1].split(">")[0])]
                self.fetch_args = args[1]
                out.append((f"1 (UID {u} FLAGS () RFC822.SIZE {len(raw)} BODY[] {{{len(part)}}}".encode(), part))
                out.append(b")")
            return "OK", out
        raise AssertionError(cmd)

    def fetch(self, seq, fields):
        start = int(seq.split(":")[0])
        self.fetched_seq = seq
        uids = sorted(self.messages)[start - 1:]
        return self.uid("FETCH", ",".join(map(str, uids)), fields)

    def append(self, box, flags, date, raw):
        self.appended.append((box, flags, raw))
        return "OK", [b"[APPENDUID 1 77] Append completed."]


def _orig():
    m = EmailMessage()
    m["From"] = "Kunde <kunde@example.com>"
    m["To"] = "Shop <shop@example.com>"
    m["Subject"] = "Angebot Coaching"
    m["Message-ID"] = "<orig@example.com>"
    m["Date"] = "Fri, 23 Oct 2026 10:00:00 +0200"
    m.set_content("Können Sie mir ein Angebot schicken?")
    return m.as_bytes()


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeIMAP({5: _orig(), 9: _orig()})
        patcher = mock.patch.object(imaplib, "IMAP4_SSL", return_value=self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = mc.MailClient(mc.MailConfig("h", 993, "user@example.com", "pw", "Jane",
                                                    aliases=("shop@example.com",)))

    def test_folders(self):
        names = [f["name"] for f in self.client.list_folders()]
        self.assertEqual(names, ["INBOX", "Entwürfe"])

    def test_list_messages_newest_first(self):
        res = self.client.list_messages(limit=1)
        self.assertEqual([m["uid"] for m in res["messages"]], [9])

    def test_search_non_ascii_uses_literal(self):
        self.client.search_messages(subject="Grüße", sender="kunde", since="2026-10-01")
        literals = [lit for _, lit in self.fake.searches if lit]
        self.assertEqual(literals, ["Grüße".encode()])
        ascii_args = [a for a, lit in self.fake.searches if not lit][0]
        self.assertIn("SINCE 01-Oct-2026", ascii_args)
        self.assertIn('FROM "kunde"', ascii_args)

    def test_read_message(self):
        r = self.client.read_message(5)
        self.assertEqual(r["subject"], "Angebot Coaching")
        self.assertIn("Angebot schicken", r["body"])

    def test_mark_read_sets_only_seen(self):
        r = self.client.mark_read([9, 5, 7])
        self.assertEqual(r["marked_read"], [5, 9])
        self.assertEqual(r["not_found"], [7])
        self.assertEqual(self.fake.stored, ("5,9", "+FLAGS.SILENT", "(\\Seen)"))

    def test_mark_read_unknown_uids_store_nothing(self):
        r = self.client.mark_read([7])
        self.assertEqual(r["marked_read"], [])
        self.assertFalse(hasattr(self.fake, "stored"))

    def test_mark_read_rejects_bad_input(self):
        for uids in ([], [0], ["5 +FLAGS (\\Deleted)"], list(range(1, 102))):
            with self.subTest(uids=uids[:3]):
                with self.assertRaises(mc.MailError):
                    self.client.mark_read(uids)

    def test_reading_stays_read_only(self):
        self.client.read_message(5)
        self.assertTrue(self.fake.readonly)

    def test_reply_draft(self):
        r = self.client.create_draft(None, "", "Gerne, anbei.", reply_to_uid=5)
        self.assertEqual(r["uid"], 77)
        self.assertEqual(r["saved_in"], "Entwürfe")
        box, flags, raw = self.fake.appended[0]
        self.assertEqual(box, '"Entw&APw-rfe"')
        self.assertIn("\\Draft", flags)
        d = message_from_bytes(raw, policy=default)
        self.assertEqual(d["Subject"], "Re: Angebot Coaching")
        self.assertEqual(d["In-Reply-To"], "<orig@example.com>")
        self.assertIn("kunde@example.com", d["To"])
        self.assertIn("> Können Sie", d.get_content())
        self.assertIn("shop@example.com", d["From"])
        self.assertTrue(d["Message-ID"].endswith("@example.com>"))

    def test_from_default_and_rejected(self):
        self.client.create_draft(["a@b.de"], "Hi", "x")
        d = message_from_bytes(self.fake.appended[0][2], policy=default)
        self.assertIn("user@example.com", d["From"])
        with self.assertRaises(mc.MailError):
            self.client.create_draft(["a@b.de"], "Hi", "x", from_address="chef@fremd.de")

    def test_wildcard_alias(self):
        cfg = mc.MailConfig("h", 993, "user@example.com", "pw", aliases=("*@example.com",))
        self.assertTrue(cfg.is_allowed_sender("Dev@example.com"))
        self.assertFalse(cfg.is_allowed_sender("x@example.com.evil.de"))

    def test_header_injection_rejected(self):
        with self.assertRaises(Exception):
            self.client.create_draft(["a@b.de"], "x\r\nBcc: evil@x.de", "body")


PDF = b"%PDF-1.4\n%test\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 8


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "att"
        self.dir.mkdir()
        self.outside = Path(tmp.name) / "outside.pdf"
        self.outside.write_bytes(PDF)
        (self.dir / "invoice.pdf").write_bytes(PDF)
        (self.dir / "sub").mkdir()
        (self.dir / "sub" / "pic.PNG").write_bytes(PNG)

    def cfg(self, **kw):
        kw.setdefault("attachment_dir", self.dir)
        return mc.MailConfig("h", 993, "user@example.com", "pw", **kw)

    def refused(self, names, **kw):
        with self.assertRaises(mc.MailError) as cm:
            mc.load_attachments(self.cfg(**kw), names)
        return str(cm.exception)

    def test_loads_allowed_files(self):
        files = mc.load_attachments(self.cfg(), ["invoice.pdf", "sub/pic.PNG"])
        self.assertEqual([(n, m) for n, m, _ in files],
                         [("invoice.pdf", "application/pdf"), ("pic.PNG", "image/png")])

    def test_nothing_requested_needs_no_config(self):
        self.assertEqual(mc.load_attachments(self.cfg(attachment_dir=None), None), [])
        self.assertEqual(mc.load_attachments(self.cfg(attachment_dir=None), []), [])

    def test_disabled_without_directory(self):
        self.assertIn("not enabled", self.refused(["invoice.pdf"], attachment_dir=None))

    def test_path_escapes_are_refused(self):
        for name in ("../outside.pdf", "/etc/passwd.pdf", "sub/../../outside.pdf",
                     "sub\\..\\x.pdf", "a\nb.pdf", "", "./invoice.pdf"):
            with self.subTest(repr(name)):
                self.refused([name])

    def test_symlinks_are_refused(self):
        os.symlink(self.outside, self.dir / "link.pdf")
        os.symlink(self.outside.parent, self.dir / "linkdir")
        self.assertIn("symbolic", self.refused(["link.pdf"]))
        self.assertIn("symbolic", self.refused(["linkdir/outside.pdf"]))

    def test_hidden_files_are_refused(self):
        (self.dir / ".secret.pdf").write_bytes(PDF)
        (self.dir / ".hidden").mkdir()
        (self.dir / ".hidden" / "a.pdf").write_bytes(PDF)
        self.refused([".secret.pdf"])
        self.refused([".hidden/a.pdf"])

    def test_only_allowed_types(self):
        for name in ("run.sh", "a.zip", "a.exe", "doc.docx", "noext"):
            (self.dir / name).write_bytes(PDF)
            with self.subTest(name):
                self.assertIn("allowed", self.refused([name]))

    def test_content_must_match_type(self):
        (self.dir / "fake.pdf").write_bytes(b"#!/bin/sh\necho hi\n")
        (self.dir / "fake.jpg").write_bytes(PNG)
        self.assertIn("does not look like", self.refused(["fake.pdf"]))
        self.assertIn("does not look like", self.refused(["fake.jpg"]))

    def test_missing_directory_and_special_files(self):
        self.assertIn("not found", self.refused(["nope.pdf"]))
        (self.dir / "dir.pdf").mkdir()
        self.refused(["dir.pdf"])

    def test_limits(self):
        (self.dir / "b.pdf").write_bytes(PDF)
        self.assertIn("too large", self.refused(["invoice.pdf"], attachment_max_bytes=5))
        self.assertIn("together", self.refused(["invoice.pdf", "b.pdf"],
                                               attachment_max_total_bytes=len(PDF) + 1))
        self.assertIn("Too many", self.refused(["invoice.pdf", "b.pdf"],
                                               attachment_max_count=1))
        self.assertIn("more than once", self.refused(["invoice.pdf", "invoice.pdf"]))

    def test_draft_contains_attachment(self):
        fake = FakeIMAP({1: _orig()})
        with mock.patch.object(imaplib, "IMAP4_SSL", return_value=fake):
            r = mc.MailClient(self.cfg()).create_draft(
                ["a@b.de"], "Hi", "see attachment", attachments=["invoice.pdf"])
        self.assertEqual(r["attachments"], ["invoice.pdf"])
        d = message_from_bytes(fake.appended[0][2], policy=default)
        (part,) = list(d.iter_attachments())
        self.assertEqual((part.get_filename(), part.get_content_type()),
                         ("invoice.pdf", "application/pdf"))
        self.assertEqual(part.get_content(), PDF)
        self.assertIn("see attachment", d.get_body().get_content())

    def test_refused_attachment_means_no_draft(self):
        fake = FakeIMAP({1: _orig()})
        with mock.patch.object(imaplib, "IMAP4_SSL", return_value=fake):
            with self.assertRaises(mc.MailError):
                mc.MailClient(self.cfg()).create_draft(
                    ["a@b.de"], "Hi", "x", attachments=["../outside.pdf"])
        self.assertEqual(fake.appended, [])

    def test_config_keys(self):
        p = self.dir.parent / "acc.env"
        p.write_text(f"MAIL_USER=a@b.de\nMAIL_PASSWORD=x\nATTACHMENT_DIR={self.dir}\n"
                     "ATTACHMENT_MAX_COUNT=2\n", encoding="utf-8")
        with mock.patch.dict("os.environ", {}, clear=True):
            cfg = mc.load_accounts(p)["hetzner"]
        self.assertEqual((cfg.attachment_dir, cfg.attachment_max_count), (self.dir, 2))
        p.write_text("MAIL_USER=a@b.de\nMAIL_PASSWORD=x\nATTACHMENT_DIR=/nonexistent\n",
                     encoding="utf-8")
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(mc.MailError):
                mc.load_accounts(p)


class AccountLoadingTests(unittest.TestCase):
    def _load(self, text):
        p = Path(__file__).with_name("_acc.env")
        p.write_text(text, encoding="utf-8")
        try:
            with mock.patch.dict("os.environ", {}, clear=True):
                return mc.load_accounts(p)
        finally:
            p.unlink()

    def test_single_legacy(self):
        acc = self._load("MAIL_USER=user@example.com\nMAIL_PASSWORD=x\nMAIL_ALIASES=shop@example.com\n")
        self.assertEqual(list(acc), ["hetzner"])
        self.assertEqual(acc["hetzner"].imap_host, "mail.your-server.de")
        self.assertEqual(acc["hetzner"].sender_addresses(), ["user@example.com", "shop@example.com"])

    def test_hetzner_plus_icloud(self):
        acc = self._load(
            "MAIL_USER=user@example.com\nMAIL_PASSWORD=x\nMAIL_FROM_NAME=Jane Doe\n"
            "ACCOUNTS=hetzner,icloud\n"
            "ICLOUD_MAIL_USER=jane\nICLOUD_MAIL_ADDRESS=Jane@icloud.com\nICLOUD_MAIL_PASSWORD=y\n")
        self.assertEqual(list(acc), ["hetzner", "icloud"])
        ic = acc["icloud"]
        self.assertEqual((ic.imap_host, ic.user, ic.own_address), ("imap.mail.me.com", "jane", "jane@icloud.com"))
        self.assertEqual(ic.from_name, "Jane Doe")
        self.assertTrue(ic.is_allowed_sender("jane@icloud.com"))
        self.assertFalse(ic.is_allowed_sender("jane"))

    def test_unknown_account_without_host_is_rejected(self):
        # The password must never be sent to a guessed server.
        with self.assertRaises(mc.MailError) as cm:
            self._load("ACCOUNTS=hetzner,gmail\nMAIL_USER=a@b.de\nMAIL_PASSWORD=x\n"
                       "GMAIL_MAIL_USER=me@gmail.com\nGMAIL_MAIL_PASSWORD=secret\n")
        self.assertIn("GMAIL_IMAP_HOST", str(cm.exception))

    def test_unknown_account_with_explicit_host(self):
        acc = self._load("ACCOUNTS=hetzner,gmail\nMAIL_USER=a@b.de\nMAIL_PASSWORD=x\n"
                         "GMAIL_MAIL_USER=me@gmail.com\nGMAIL_MAIL_PASSWORD=secret\n"
                         "GMAIL_IMAP_HOST=imap.gmail.com\n")
        self.assertEqual(acc["gmail"].imap_host, "imap.gmail.com")

    def test_missing_icloud_password(self):
        with self.assertRaises(mc.MailError) as cm:
            self._load("MAIL_USER=a@b.de\nMAIL_PASSWORD=x\nACCOUNTS=hetzner,icloud\nICLOUD_MAIL_USER=jane\n")
        self.assertIn("ICLOUD_MAIL_PASSWORD", str(cm.exception))

    def test_icloud_draft_uses_address(self):
        fake = FakeIMAP({1: _orig()})
        cfg = mc.MailConfig("imap.mail.me.com", 993, "jane", "pw", "Jane",
                            name="icloud", address="jane@icloud.com")
        with mock.patch.object(imaplib, "IMAP4_SSL", return_value=fake):
            r = mc.MailClient(cfg).create_draft(["a@b.de"], "Hi", "x")
        self.assertEqual(r["from"], "jane@icloud.com")


class SecurityAndParsingTests(unittest.TestCase):
    def test_search_rejects_control_chars(self):
        client = mc.MailClient(mc.MailConfig("h", 993, "u@x.de", "pw"))
        conn = mock.Mock()
        for bad in ('x"\r\na1 STORE 1:* +FLAGS (\\Deleted)', "a\nb", "a\x00b"):
            with self.assertRaises(mc.MailError):
                client._search(conn, [], [("SUBJECT", bad)])
        conn.uid.assert_not_called()

    def test_alias_preferred_over_delivered_to(self):
        cfg = mc.MailConfig("h", 993, "user@example.com", "pw",
                            aliases=("dev@example.com",))
        m = EmailMessage()
        m["Delivered-To"] = "user@example.com"
        m["To"] = "dev@example.com"
        self.assertEqual(mc.MailClient(cfg)._matching_alias(m), "dev@example.com")
        del m["To"]
        m["To"] = "list@example.com"
        self.assertEqual(mc.MailClient(cfg)._matching_alias(m), "user@example.com")

    def test_search_empty_result_none(self):
        client = mc.MailClient(mc.MailConfig("h", 993, "u@x.de", "pw"))
        conn = mock.Mock()
        conn.uid.return_value = ("OK", [None])
        self.assertEqual(client._search(conn, [], [("SUBJECT", "Größe")]), [])
        self.assertEqual(client._search(conn, ["UNSEEN"], []), [])

    def test_parse_fetch_meta_after_literal(self):
        data = [(b"1 (BODY[HEADER.FIELDS (FROM)] {5}", b"hello"),
                b" UID 42 FLAGS (\\Seen) RFC822.SIZE 99)"]
        (meta, raw), = mc.parse_fetch(data)
        self.assertEqual(raw, b"hello")
        self.assertEqual(meta, {"uid": 42, "flags": ["\\Seen"], "size_bytes": 99})

    def test_parse_fetch_meta_before_literal(self):
        data = [(b"1 (UID 7 FLAGS () RFC822.SIZE 3 BODY[] {1}", b"x"), b")"]
        (meta, _), = mc.parse_fetch(data)
        self.assertEqual((meta["uid"], meta["flags"]), (7, []))

    def test_network_error_becomes_mailerror(self):
        client = mc.MailClient(mc.MailConfig("h", 993, "u@x.de", "pw"))
        fake = mock.MagicMock()
        fake.list.side_effect = TimeoutError("timed out")
        with mock.patch.object(imaplib, "IMAP4_SSL", return_value=fake):
            with self.assertRaises(mc.MailError):
                client.list_folders()

    def test_config_path_env_name(self):
        with mock.patch.dict("os.environ", {"MAIL_MCP_CONFIG": "/new.env"}):
            self.assertEqual(str(mc.config_path()), "/new.env")
            self.assertEqual(str(mc.config_path("/arg.env")), "/arg.env")

    def test_bad_port_is_mailerror(self):
        with self.assertRaises(mc.MailError):
            mc.MailConfig.from_env({"MAIL_USER": "a@b.de", "MAIL_PASSWORD": "x",
                                    "IMAP_PORT": "abc"}, "hetzner")


class FakeServerTestCase(unittest.TestCase):
    """Base class for tests that run a MailClient against a FakeIMAP."""

    def _client(self, fake: "FakeIMAP") -> mc.MailClient:
        patcher = mock.patch.object(imaplib, "IMAP4_SSL", return_value=fake)
        self.ctor = patcher.start()  # ctor.call_count = number of connections opened
        self.addCleanup(patcher.stop)
        return mc.MailClient(mc.MailConfig("h", 993, "u@x.de", "pw"))


class PoolAndLimitTests(FakeServerTestCase):
    def test_connection_reused(self):
        c = self._client(FakeIMAP({1: _orig()}))
        c.list_messages(); c.list_messages(); c.read_message(1)
        self.assertEqual(self.ctor.call_count, 1)

    def test_reconnect_after_network_error(self):
        fake = FakeIMAP({1: _orig()})
        c = self._client(fake)
        c.list_messages()
        with mock.patch.object(fake, "select", side_effect=OSError("reset")):
            with self.assertRaises(mc.MailError):
                c.list_messages()
        self.assertTrue(fake.logged_out)
        c.list_messages()
        self.assertEqual(self.ctor.call_count, 2)

    def test_mailerror_keeps_connection(self):
        fake = FakeIMAP({1: _orig()})
        c = self._client(fake)
        c.list_messages()
        with self.assertRaises(mc.MailError):
            c.read_message(99)
        c.list_messages()
        self.assertEqual(self.ctor.call_count, 1)

    def test_idle_connection_replaced(self):
        c = self._client(FakeIMAP({1: _orig()}))
        c.list_messages()
        c._last_used -= mc.POOL_MAX_IDLE + 1
        c.list_messages()
        self.assertEqual(self.ctor.call_count, 2)

    def test_dead_connection_detected_by_noop(self):
        fake = FakeIMAP({1: _orig()})
        c = self._client(fake)
        c.list_messages()
        c._last_used -= mc.POOL_CHECK_AFTER + 1
        with mock.patch.object(fake, "noop", side_effect=OSError("gone")):
            c.list_messages()
        self.assertEqual(self.ctor.call_count, 2)

    def test_large_message_is_truncated(self):
        big = _orig() + b"x" * 200
        c = self._client(FakeIMAP({1: big}))
        with mock.patch.object(mc, "MAX_FETCH_BYTES", 100):
            r = c.read_message(1)
        self.assertTrue(r["message_truncated"])
        self.assertEqual(r["size_bytes"], len(big))

    def test_broken_message_id_does_not_break_read(self):
        raw = b"From: a@b.de\r\nMessage-ID: <\r\nSubject: Hi\r\n\r\nText\r\n"
        r = self._client(FakeIMAP({1: raw})).read_message(1)
        self.assertEqual(r["subject"], "Hi")
        self.assertEqual(r["body"], "Text")

    def test_small_message_not_truncated(self):
        c = self._client(FakeIMAP({1: _orig()}))
        r = c.read_message(1)
        self.assertFalse(r["message_truncated"])
        self.assertIn("Angebot", r["body"])


class ListingTests(FakeServerTestCase):

    def test_list_messages_uses_sequence_range_not_search(self):
        fake = FakeIMAP({u: _orig() for u in (3, 5, 9, 12)})
        r = self._client(fake).list_messages(limit=2)
        self.assertEqual(fake.searches, [])
        self.assertEqual(fake.fetched_seq, "3:*")
        self.assertEqual([m["uid"] for m in r["messages"]], [12, 9])
        self.assertEqual((r["total_in_folder"], r["matching"]), (4, 4))

    def test_list_messages_limit_larger_than_folder(self):
        fake = FakeIMAP({1: _orig()})
        r = self._client(fake).list_messages(limit=50)
        self.assertEqual(fake.fetched_seq, "1:*")
        self.assertEqual(len(r["messages"]), 1)

    def test_list_messages_unread_still_searches(self):
        fake = FakeIMAP({1: _orig(), 2: _orig()})
        self._client(fake).list_messages(unread_only=True)
        self.assertEqual(len(fake.searches), 1)

    def test_list_folders_counts(self):
        folders = self._client(FakeIMAP({1: _orig()})).list_folders()
        self.assertEqual([(f["name"], f["messages"], f["unseen"]) for f in folders],
                         [("INBOX", 2, 1), ("Entwürfe", 2, 1)])


if __name__ == "__main__":
    unittest.main()
