# QuickPoll

Create a poll, share a link, watch live results roll in. No account needed.

## Run it

```bash
pip install -r requirements.txt
python app.py
```

Open http://localhost:4000. The SQLite database (`quickpoll.db`) is created
automatically on first start. To use a different database path or port:

```bash
QUICKPOLL_DB=/data/polls.db PORT=8000 python app.py
```

## Features

- **Create a poll** — question + 2–8 options (dynamic add/remove), with settings:
  single vs. multiple selections, live results visible to voters or hidden until
  close, optional voter-name field, optional automatic close date/time.
- **Share link** — every poll gets a short public code (`/p/abcdef`); the creator
  gets a secret manage link (`/m/<token>`) to edit (before the first vote),
  close/reopen, delete, and copy the share link.
- **Vote page** — options as big clickable cards, one-tap voting, animated
  result bars that auto-refresh every 5 seconds, total vote count, closed state.
- **Duplicate-vote prevention** — server-side IP-hash check plus a voter cookie
  (best effort; ballot = one set of choices per voter).
- **Correctness** — closed polls reject votes with a clear message (HTTP 410),
  percentages computed server-side, all SQL parameterized, output escaped.
- **Testability** — stable `data-testid` attributes on key elements
  (`create-form`, `question-input`, `vote-button`, `result-bar`, …), semantic HTML.

## Routes

| Route | What it does |
|---|---|
| `GET /` | Home: hero, create CTA, recent public polls |
| `GET /new`, `POST /new` | Create-poll form and handler |
| `GET /created/<token>` | Post-creation page with share + manage links |
| `GET /p/<code>` | Vote page with live results |
| `POST /p/<code>/vote` | Cast a vote (JSON response) |
| `GET /api/p/<code>/results` | Results JSON for the 5s auto-refresh |
| `GET /m/<token>` | Manage page (results, edit, close/reopen/delete) |
| `POST /m/<token>/close` `/reopen` `/delete` `/edit` | Manage actions |
| `GET /healthz` | Health check |

## Notes

- Stdlib SQLite, Flask only — no build step, no npm.
- Rate limiting on the vote endpoint is a simple in-memory per-IP throttle
  (20 votes/min); replace with Redis for multi-worker deployments.
- For production, set `SECRET_SALT` to a random value and serve behind a
  reverse proxy with HTTPS.
