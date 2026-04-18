# HappyDays dev server (`server/main.py`)

A single-file Python server (stdlib `http.server` + `anthropic` SDK). It serves
the static HappyDays SPA from the repo root on `http://localhost:8765` and
exposes a streaming chat endpoint at `POST /api/chat` powered by Claude Haiku
4.5. The cacheable system prefix (persona plus panel, zone, and SMHI station
tables plus a Swedish-solar primer) is loaded once at start and tagged with
Anthropic's `cache_control: ephemeral` so repeat calls hit the prompt cache at
roughly 0.1× the regular input cost.

## Run

```sh
pip install -r server/requirements.txt          # or: pip3 install --user -r server/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...              # needed for chat; static files still serve without it
python3 server/main.py
```

Then open <http://localhost:8765>. The AI adviser panel on the right talks to
`/api/chat` via Server-Sent Events — tokens stream in as they arrive.

## Troubleshooting

- **"Kunde inte svara — kontrollera att servern har ANTHROPIC_API_KEY satt"**:
  the server is running but the env var is missing. Export the key and restart
  the server (the client reads the env once at boot). `/api/chat` returns a
  clean `503` JSON in this state; everything else keeps working.
- **Port 8765 already in use**: `lsof -ti :8765 | xargs kill` to free it, then
  relaunch. The server only binds `127.0.0.1` so it won't leak to the LAN.
- **Cache hit verification**: look for `[chat] usage: ...` lines in the server
  log. After the second request with the same system prefix you should see
  `cache_read_input_tokens` jump well above `cache_creation_input_tokens` (on
  the first call the reverse is true). The `cached prefix chars` line at
  startup also reports the approximate token count; anything below 4096 means
  caching silently misses on Haiku 4.5 and the reference block needs padding.

## Cost note

Haiku 4.5 is priced at roughly $1 / million input tokens and $5 / million
output tokens. Cached-read input tokens bill at about 0.1× input — so once the
reference block is warm, a short chat turn costs only the dynamic context plus
the user/assistant turns and the generated reply. Rate limiting is capped at
20 requests per IP per minute, in-memory, which resets when the server is
restarted.
