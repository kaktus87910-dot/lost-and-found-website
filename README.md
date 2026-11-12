# Lost & Found — GitHub Pages Demo

A static, browser-only demonstration of a Lost & Found site. It supports creating
lost/found reports, attaching a photo, choosing an item category and color,
reviewing possible opposite-type matches, editing reports, and confirming or
deleting reports.

## Important limitations

This GitHub Pages version has no server or shared database. Reports and photos
are saved only in the current browser's `localStorage`; they are not visible to
other people or devices and can be removed when browser storage is cleared.
Categories are selected manually. GitHub Pages cannot run the project's Python,
OpenCV/YOLO model, C++ application, or SQLite database, so this demo does not
perform AI object recognition and is not the full local application.

Do not enter sensitive personal information or upload photos you do not have
permission to use. This public repository contains only the static website and
its deployment workflow, not local reports, photos, model weights, or database
files.

## Deploy

The included GitHub Actions workflow deploys the `pages/` directory. In the
repository, open **Settings → Pages**, choose **GitHub Actions** as the build
and deployment source, then run the **Deploy static Lost & Found demo** workflow
or push a change to `pages/`.

The site URL will be:

`https://kaktus87910-dot.github.io/lost-and-found-website/`
