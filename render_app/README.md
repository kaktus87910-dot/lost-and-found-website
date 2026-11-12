# Found — Render server

This package is the server-hosted version of the Lost & Found app. It serves the
English web UI, public report creation/browsing, OpenCV DNN object and color
detection, SQLite-backed storage, and administrator-only edit/delete/match
confirmation. `../render.yaml` configures the service and its persistent disk.

## Deploy

Connect the repository to Render and create a Blueprint from `render.yaml`.
It creates a paid Starter web service and a 1 GB persistent disk; check current
pricing before approving. Set the prompted `ADMIN_PASSWORD_HASH` secret using a
hash generated locally:

```sh
python3 render_app/server.py --hash-password
```

Use a unique password of at least 14 characters. Paste only the generated
`pbkdf2_sha256$...` value into the Render secret field. Render generates
`SESSION_SECRET`; never commit either secret, an `.env` file, SQLite data, or
photos. Render supplies HTTPS. The app expects one service instance because
SQLite, login rate limits, and active admin sessions are process-local.

## Model and storage

The Render disk stores `lost_found.sqlite3` and public photos in `/var/data`.
The model is expected at `/var/data/models/yolov8n.onnx`. The model is not in
this repository: its source repository has no declared license and the upstream
YOLOv8 model card states AGPL-3.0. Review the terms before provisioning any
weights. Until a rights-cleared compatible model is securely provisioned,
`/api/health` reports detection unavailable and report submission is blocked;
the app does not fabricate detections or switch to manual labels.

Reports and photos are public. Avoid personal or sensitive information. No
automatic retention or backup job is configured; arrange protected backups of
both the SQLite database and photo directory, and test restores.

The GitHub Pages build in `../pages/` remains a separate static demo. Pages has
no server-side inference or shared database and uses manual category/color
selection with browser-local storage.
