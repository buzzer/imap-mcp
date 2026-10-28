# Security policy

## Scope and intended use

imap-mcp gives an AI assistant access to real mailboxes. It is a personal project
intended for a **trusted home network**. The HTTP transport uses a single static
bearer token without rate limiting; the recommended setup is the stdio transport over
SSH, or HTTPS on a private LAN. Do not expose the HTTP port to the internet.

Design limits that reduce the impact of mistakes:

- no tool can send mail, delete or move messages (drafts only);
- the only flag that can be set is `\Seen` (`mark_read`, or `read_message` if
  `MARK_READ_ON_READ` is enabled); no other flag can be changed;
- messages are fetched with `BODY.PEEK` and folders are opened read-only, except
  for the single `STORE` that sets `\Seen`;
- sender addresses are restricted to the account address and configured aliases;
- attachments are opt-in (`ATTACHMENT_DIR`), limited to PDF/PNG/JPEG below that one
  folder (no `..`, symlinks or hidden files) with size and count limits; the draft
  still has to be reviewed and sent by the user;
- mail content is treated as untrusted data (prompt injection is still possible, so
  review every draft before sending it).

## Reporting a vulnerability

Please report security problems privately through GitHub's
["Report a vulnerability"](../../security/advisories/new) form of this repository
instead of opening a public issue. Include the affected version or commit and, if
possible, steps to reproduce. This is a hobby project, so there is no guaranteed
response time, but reports are taken seriously.

Never post mailbox credentials, tokens or private keys in an issue. If you ever
committed `config.env` by accident, rotate the passwords and the `MCP_AUTH_TOKEN`.
