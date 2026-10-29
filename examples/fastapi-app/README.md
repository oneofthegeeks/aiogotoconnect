# FastAPI example

A host app that mounts the goto-sms setup page at `/api/goto/` behind a demo cookie login, logs
inbound texts (masked sender and length only), and has a `POST /send` route.

```sh
pip install -e .[sms] fastapi uvicorn
ADMIN_PASSWORD=change-me GOTO_SMS_PUBLIC_URL=http://localhost:8765 \
  uvicorn app:app --app-dir examples/fastapi-app --port 8765
```

Open `http://localhost:8765/login?password=change-me` once (it sets the cookie), then
`http://localhost:8765/api/goto/`. Data is kept in `./data`.

- `mount_path` must equal the path the app is mounted at (`/api/goto`), or the page shows a wrong
  redirect URI.
- The lifespan uses `async with sms:`, so polling starts at boot and stops on shutdown.
- `GOTO_SMS_AUTH_BASE_URL` / `GOTO_SMS_API_BASE_URL` are read by this example only, to point it at
  a fake GoTo for `pytest e2e`. Leave them unset against real GoTo.
- The cookie login is a demo; replace `is_admin` with your own authentication.
