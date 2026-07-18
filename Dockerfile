# Minimal container that runs the Furl MCP server on stdio.
# Prebuilt manylinux wheels ship the Rust core, so no build toolchain is needed.
FROM python:3.12-slim

# No .pyc files and unbuffered stdio so the JSON-RPC stream is never held back.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install the compression library plus the MCP server extra from PyPI.
RUN pip install --no-cache-dir "furl-ctx[mcp]"

# Run as a non-root user with a writable home for the CCR store at ~/.furl.
RUN useradd --create-home --uid 10001 furl
USER furl
WORKDIR /home/furl

# The MCP server speaks JSON-RPC over stdin and stdout.
ENTRYPOINT ["python", "-m", "furl_ctx.ccr.mcp_server"]
