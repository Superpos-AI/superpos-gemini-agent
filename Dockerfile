FROM node:22-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip git curl ripgrep tini && \
    rm -rf /var/lib/apt/lists/*

# Install GitHub CLI
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# Install Google's Antigravity CLI (`agy`).
#
# Why `agy` rather than `@google/gemini-cli` (the npm wrapper this image
# used to install): `agy` is Google's current first-party CLI for the
# Gemini-3 family, ships a static native Go binary (no node runtime
# needed at run-time), is dramatically faster cold-start, and exposes
# the same Gemini models without intermittent npx/npm-resolution races.
# The upstream installer is vendored to `install-agy.sh` in the repo
# root because `antigravity.google` requires IPv6, which most Docker
# bridges don't have — but the binary CDN it downloads from is IPv4-OK.
COPY install-agy.sh /tmp/install-agy.sh
RUN bash /tmp/install-agy.sh -d /usr/local/bin && rm /tmp/install-agy.sh

WORKDIR /app

# superpos-agent-core is pulled directly from GitHub via requirements.txt
# (the `git+https://…` line), so no parent-directory build context required.
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

COPY src/ /app/src/
COPY entrypoint.sh /app/entrypoint.sh
COPY workspace/ /workspace/

# Symlink module scripts onto PATH so Gemini can call them by name
RUN mkdir -p /workspace/.gemini/modules-bin && \
    if [ -d /workspace/.gemini/modules ]; then \
      for dir in /workspace/.gemini/modules/*/scripts; do \
        if [ -d "$dir" ]; then \
          for script in "$dir"/*; do \
            chmod +x "$script" && \
            ln -sf "$script" /workspace/.gemini/modules-bin/$(basename "$script"); \
          done; \
        fi; \
      done; \
    fi
ENV PATH="/workspace/.gemini/modules-bin:$PATH"

# Create non-root user
RUN useradd -m -s /bin/bash -u 1001 agent && \
    mkdir -p /home/agent/.gemini && \
    chown -R agent:agent /workspace /home/agent/.gemini

# Module is nested at /app/src/slim_agent_gemini/; PYTHONPATH must include
# /app/src so that `python3 -m slim_agent_gemini` (the CMD below) resolves
# the package. (Sister claude/codex agents put their code directly under
# src/ so /app works for them; this repo's pyproject.toml-style nesting
# needs /app/src.)
ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1
ENV HOME="/home/agent"

VOLUME ["/home/agent/.gemini"]

USER agent
WORKDIR /workspace

# tini reaps orphaned grandchildren (npm/node subprocesses left behind when
# a gemini run dies) — without it they accumulate as zombies because Python
# doesn't reap reparented orphans.
ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
CMD ["python3", "-m", "slim_agent_gemini"]
