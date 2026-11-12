# Lost & Found website

This repository contains two versions of the project:

- **GitHub Pages demo:** a static browser-only sample at
  `https://kaktus87910-dot.github.io/lost-and-found-website/`. Reports are kept
  in that browser's local storage. Item and color are selected manually; this
  version does not run AI or share data with other browsers.
- **Render server app:** the `render_app/` package provides a Python website
  with public reports, OpenCV DNN object/color detection, SQLite storage, and
  administrator-only report edits, deletes, and match confirmation.

## Deploy the server app to Render

`render.yaml` is a Render Blueprint. Connect this repository to Render and
create the Blueprint. It requests a paid Starter web service and a 1 GB
persistent disk; review the current price before approving. Configure a strong
administrator password hash when prompted. Generate one locally, without
sharing the plaintext password:

```sh
python3 -m pip install -r render_app/requirements.txt
python3 render_app/server.py --hash-password
```

Set the generated `pbkdf2_sha256$...` string as `ADMIN_PASSWORD_HASH` in Render.
Render generates `SESSION_SECRET`. Never put either secret in GitHub.

### Object detection model requirement

The model weights are deliberately not included. The existing local model's
source repository does not declare a license, and the upstream YOLOv8 model
card declares AGPL-3.0. Review the exact terms before using or redistributing
any weights. To enable inference, provision a compatible, rights-cleared model
at `/var/data/models/yolov8n.onnx` on the Render persistent disk. Until then,
the server reports detection as unavailable and rejects report creation rather
than inventing results.

Reports and photos submitted to the Render app are public. Do not upload
sensitive personal information. The persistent disk stores the SQLite database
and images; arrange protected backups and test restores. This deployment is a
single-instance prototype, not a fully managed production service.

See [`render_app/README.md`](render_app/README.md) for backend details. The
GitHub Pages workflow deploys the separate `pages/` static demo.
