FROM python:3.13-slim

# Required by the MCP registry to verify that this image belongs to the server entry.
LABEL io.modelcontextprotocol.server.name="io.github.buzzer/imap-mcp"
LABEL org.opencontainers.image.source="https://github.com/buzzer/imap-mcp"
LABEL org.opencontainers.image.description="MCP server for IMAP mailboxes: read, search, create drafts"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY server.py mail_client.py ./

# The server needs no write access and no privileges.
RUN useradd --system --uid 10001 --no-create-home mcp
USER mcp

# stdio transport by default; configuration comes from environment variables
# (or from a config file mounted and selected with MAIL_MCP_CONFIG).
ENTRYPOINT ["python", "/app/server.py"]
