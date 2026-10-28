# imap-mcp

A small [MCP](https://modelcontextprotocol.io) server that gives an AI assistant
(e.g. Claude Desktop) **read access to IMAP mailboxes** and the ability to **create
drafts**. It was written for a Hetzner mailbox plus an iCloud account on a Raspberry
Pi, but it works with any IMAP server that supports IMAPS (port 993).

> **Status:** a personal project, published as-is. It is meant for a trusted home
> network, not for the open internet. See [Security](#security) and `SECURITY.md`.

## What it does

**Tools:** `list_accounts`, `list_folders`, `list_messages`, `search_messages`,
`read_message`, `mark_read`, `create_draft`. Every tool has an `account` parameter; if omitted,
the first configured account is used.

- Reading is read-only: messages are fetched with `BODY.PEEK`, nothing is marked as
  read, moved or deleted by default.
- The only write access to existing messages is the `\Seen` flag, set by `mark_read`
  or, if enabled, by `read_message` itself. See [Read status](#read-status).
- **Sending is deliberately not possible.** `create_draft` stores a draft in the
  drafts folder; you review and send it yourself in your mail client.
- Drafts can carry **attachments** (PDF, PNG, JPEG) taken from one configured folder
  (`ATTACHMENT_DIR`, off by default). See [Attachments](#attachments).
- Senders are limited to the account address plus configured aliases
  (`MAIL_ALIASES`, wildcards like `*@example.com` allowed). For replies, the address
  the original mail was sent to is chosen automatically.
- At most 2 MB of a message are loaded; `read_message` reports `message_truncated`
  when a mail was cut off.
- One IMAP connection per account is reused between calls.
- Mail content is treated as untrusted data (see the server instructions and
  [Security](#security)).

## Requirements

- Python 3.10+
- An IMAP account with an app password where required (iCloud, Gmail, ...)
- For the server itself: `mcp` and `uvicorn` (see `requirements.txt`); the mail
  logic uses only the standard library.

## 1. Installation

```bash
git clone https://github.com/<you>/imap-mcp.git
cd imap-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp config.env.example config.env
chmod 600 config.env
$EDITOR config.env          # user, password, aliases

.venv/bin/python -m unittest discover -s tests   # offline tests
.venv/bin/python server.py --check                # test the real IMAP connections
```

`--check` prints one line per account, for example
`OK   [hetzner] user@example.com @ mail.your-server.de, Drafts: INBOX.Drafts, Senders: …`.

## 2a. Connection via SSH (stdio, recommended)

The client starts the server on demand over SSH: no open port, no token, encryption
by SSH. Use a key without passphrase prompt (`ssh -o BatchMode=yes host echo ok` must
work). Example `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mail": {
      "command": "ssh",
      "args": [
        "-T", "-o", "BatchMode=yes", "-o", "ServerAliveInterval=30",
        "user@your-host",
        "/path/to/imap-mcp/.venv/bin/python",
        "/path/to/imap-mcp/server.py"
      ]
    }
  }
}
```

## 2b. Alternative: HTTP service on the LAN

Useful if several machines should use the server.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'   # generate a token
# in config.env: MCP_AUTH_TOKEN=<token>

scripts/make-cert.sh        # self-signed certificate for this host
# in config.env:
# TLS_CERT=/path/to/imap-mcp/certs/server.crt
# TLS_KEY=/path/to/imap-mcp/certs/server.key

sudo cp imap-mcp.service /etc/systemd/system/    # adjust User= and the paths first
sudo systemctl daemon-reload
sudo systemctl enable --now imap-mcp
```

The endpoint is `https://<host>:8765/mcp` and requires `Authorization: Bearer <token>`.
Without `--host` the server listens on `127.0.0.1` only; the systemd unit passes
`--host 0.0.0.0` (all interfaces), so change it to your LAN address if you prefer.
Anything but plain HTTP requests (e.g. WebSocket) is refused.
Without `TLS_CERT`/`TLS_KEY` the server keeps running in plain text and logs a
warning. The certificate is valid for 730 days; run `make-cert.sh` again afterwards
and redistribute it.

Clients that only speak stdio can use the `mcp-remote` bridge (needs Node.js). The
self-signed certificate must be trusted by Node, so point `NODE_EXTRA_CA_CERTS` to a
copy of `server.crt` **on the client machine**:

```json
{
  "mcpServers": {
    "mail": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://<host>:8765/mcp",
               "--header", "Authorization:${AUTH}"],
      "env": { "AUTH": "Bearer <token>",
               "NODE_EXTRA_CA_CERTS": "/path/on/the/client/server.crt" }
    }
  }
}
```

The custom connectors in Claude's settings do not work for a LAN address, because
they connect from Anthropic's cloud.

## Configuration

See `config.env.example` for all keys (`MAIL_USER`, `MAIL_PASSWORD`, `MAIL_ADDRESS`,
`MAIL_FROM_NAME`, `MAIL_ALIASES`, `IMAP_HOST`, `IMAP_PORT`, `IMAP_TIMEOUT`,
`DRAFTS_FOLDER`, `ATTACHMENT_DIR`, `ATTACHMENT_MAX_BYTES`, `ATTACHMENT_MAX_TOTAL_BYTES`,
`ATTACHMENT_MAX_COUNT`, `MARK_READ_ON_READ`, `BCC_SELF`, `ACCOUNTS`, `ACCOUNT_NAME`, `MCP_AUTH_TOKEN`,
`TLS_CERT`, `TLS_KEY`). The account keys up to `ATTACHMENT_MAX_COUNT` can be prefixed with
`<NAME>_` for additional accounts; the remaining keys are global. Every key can
also be set as an environment variable; `MAIL_MCP_CONFIG` or `--config` selects another
config file.

### Additional account: iCloud

```
ACCOUNTS=hetzner,icloud
ICLOUD_MAIL_USER=first.last                # login, usually the part before @icloud.com
ICLOUD_MAIL_ADDRESS=first.last@icloud.com
ICLOUD_MAIL_PASSWORD=abcd-efgh-ijkl-mnop   # app-specific password
```

The password is an app-specific password from account.apple.com
(Sign-In and Security → App-Specific Passwords), not the Apple ID password.
`imap.mail.me.com:993` is preconfigured for the account name `icloud` and
`mail.your-server.de:993` for `hetzner`. Every other account name needs
`<NAME>_IMAP_HOST`; the server never guesses a host, so a password is not sent to the
wrong provider.

## Read status

So that your local mail client shows what the assistant has already looked at,
`read_message` can set the `\Seen` flag on every message it returns:

- `MARK_READ_ON_READ=true` in `config.env` switches this on for **all accounts**
  (one global option, off by default). There is no per-call or per-account setting.
- Only `read_message` does this. Listing and searching show headers only and never
  change a flag; `mark_read` stays available for messages processed otherwise.
- The result has `unread` (state before reading) and `marked_read`. If the flag cannot
  be set, the message is still returned and `marked_read` is `false`.
- Nothing but `\Seen` is ever set; messages that are already read are left untouched.

## Bcc to yourself

With `BCC_SELF=true` in `config.env` (global for all accounts, off by default),
`create_draft` adds the sender address of the draft as Bcc, so a sent copy also lands in
that mailbox. It is the address actually used as sender (account address or alias, see
`from_address`). It is not added twice if it is already in To, Cc or Bcc. The result
shows the final `bcc`, and `list_accounts` reports `bcc_self`.

## Attachments

`create_draft` accepts `attachments`, a list of file paths **relative to
`ATTACHMENT_DIR`**. The feature is off until that key is set in `config.env`
(`list_accounts` shows `attachments_enabled` per account). Rules, all enforced on the
server:

- only `.pdf`, `.png`, `.jpg`, `.jpeg`, and the content must start with the matching
  magic bytes (a renamed file is refused);
- only files below `ATTACHMENT_DIR`: absolute paths, `..`, symbolic links and hidden
  files or folders (`.env`, `.ssh`, ...) are refused;
- at most `ATTACHMENT_MAX_COUNT` files (default 5), `ATTACHMENT_MAX_BYTES` each
  (default 10 MB) and `ATTACHMENT_MAX_TOTAL_BYTES` together (default 15 MB);
- the draft is only stored if every attachment passes; the model never sees the file
  content, only the names.

### Suggested setup

Use a dedicated, flat folder that holds nothing but files you are happy to attach.
Everything in it can be attached by the model.

```bash
mkdir -p ~/mail-attachments
chmod 700 ~/mail-attachments
```

```
ATTACHMENT_DIR=/home/<user>/mail-attachments
```

- With the stdio transport over SSH the server reads the files as the SSH user.
- Paths in the tool call are relative to that folder, e.g. `invoice-2026-10.pdf` or
  `invoices/2026/invoice-2026-10.pdf`; absolute paths are refused.
- The server only reads. Put files there yourself (`scp file.pdf host:mail-attachments/`,
  `rsync`, Syncthing or a Samba share), then ask the assistant to attach them by name.
- Several accounts can use separate folders with the prefix, e.g.
  `ICLOUD_ATTACHMENT_DIR=/home/<user>/mail-attachments-icloud`.
- With the systemd unit (`ProtectHome=read-only`) the service user needs read access;
  the simplest way is a folder outside `/home`, e.g. `/opt/imap-mcp/attachments`.
- Remove files you no longer need.

## Security

- `config.env` contains mailbox passwords: `chmod 600`, never commit it (the server
  warns if the file is readable by others).
- Mail content is untrusted text. The server instructions tell the model not to
  follow requests contained in mails. Since nothing can be sent or deleted, the
  possible damage is limited to drafts and to leaking mail content to the model.
- HTML mail is converted to text for the model without content a reader would not see
  (`display:none`, `hidden`, `opacity:0`, collapsed preheaders, `script`/`style`/`head`),
  which removes the most common hiding places for invisible prompt injection. Text that
  is only unreadable (white on white, tiny fonts set on child elements) is still passed
  on, so the protection is a mitigation, not a guarantee.
- Search terms containing control characters (CR/LF) are rejected, so they cannot
  inject additional IMAP commands. Recipient addresses with line breaks are rejected.
- The HTTP transport uses a single static bearer token without rate limiting. Use it
  only on a trusted network, with TLS, and rotate the token if it leaks.
- Failed HTTP authentication attempts are logged with the client IP; tool calls
  (account, folder, UID) are logged without mail content.

## Known limitations

- Drafts are plain text only. The attribution line of a quoted reply
  ("Am … schrieb …:") and the placeholder subject "(kein Betreff)" are in German.
- Attachments are limited to PDF/PNG/JPEG from one folder; no moving/deleting of
  messages; the only flag that can be set is `\Seen` (via `mark_read`).
- Tested against Hetzner (Dovecot) and iCloud only.

## Development

```bash
python -m unittest discover -s tests    # no network and no MCP SDK needed
```

## License

[MIT](LICENSE)
