# womptake

take any published [womp](https://beta.womp.com) project home: its full
public metadata and its render images, anonymously, no account.

    python3 womptake.py search "query" [limit]
    python3 womptake.py <link-or-id-or-url> [outdir]

examples:

    python3 womptake.py search cat 20
    python3 womptake.py lb6oevy747rdd92k out/
    python3 womptake.py https://beta.womp.com/discover?aph=lb6oevy747rdd92k out/

for every project you take home you get:

- `project.json` — the complete public record: title, author, license,
  publish date, likes, views, remix tree (who it was remixed from and
  everything remixed from it), categories, badges, share mode
- `images/preview.webp` — the full-size rendered preview (1920x1080)
- `images/thumbnail.webp` — the square thumbnail used in the gallery

no dependencies, no account, no api key. pure python 3 stdlib.

## how it works

womp is a text-to-3d community. published projects appear in the public
discover gallery, which the anonymous web client renders after three
plain requests. womptake makes the same three requests:

1. `POST /frontend-session/refresh` asks the backend for a guest
   frontend key (every anonymous browser gets one automatically)
2. `GET /search/discover?q=...` lists published projects
3. `GET /project/<link>` pulls one project's full metadata, and the
   preview/thumbnail images are plain files on their public cdns

everything here is data the discover gallery already shows any visitor.

## honest scope note: no mesh

the 3d scene itself (splat / mesh / nerf files) is served from an
endpoint that requires a logged-in session, and "view in 3d" pushes
anonymous visitors to a login wall. womptake does not log in, does not
touch credentials, and will not fetch scene files. it takes home what is
public: metadata and renders. if you need the 3d data, use your own
account in the official app.

license fields come from the project itself: most projects are CCO
(public domain), others CC BY / BY-NC / BY-ND / BY-NC-ND. respect the
license of what you take.

## scope and intent

same spirit as [roomtaker](https://github.com/kqrla/roomtaker) and
splinetake: public published data, fetchable by any plain browser, for
backup, archival and interoperability. this is for projects you
published or have the right to use.

MIT license. womp is a product of Womp, inc.; this project is
independent and not affiliated.
