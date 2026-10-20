#!/usr/bin/env python3
"""womptake: take any published womp project home (metadata + renders).

womp (beta.womp.com / womp3d.com) is a text-to-3d community. published
projects show up in /discover with their metadata and renders; those are
public to any browser. this tool reproduces exactly what the anonymous
web client does:

  1. POST /frontend-session/refresh   -> guest frontend key
  2. GET  /search/discover?q=...      -> published projects
  3. GET  /project/<link-or-id>       -> one project's full metadata
  4. download its public render images

the 3d scene files themselves (mesh / splat) sit behind a login wall, so
they are not included. see the README for the honest scope note.

usage (the search api returns one page of up to 30 matches per call):
  python3 womptake.py search "query" [limit]
  python3 womptake.py <link-or-id-or-url> [outdir]

examples:
  python3 womptake.py search cat 20
  python3 womptake.py lb6oevy747rdd92k out/
  python3 womptake.py https://beta.womp.com/discover?aph=lb6oevy747rdd92k out/

pure python 3 stdlib, no account, no api key.
"""

import json
import re
import sys
import urllib.parse
import urllib.request

API = "https://kkp-beta-backend.womp3d.com"
SITE = "https://beta.womp.com"
UA = "Mozilla/5.0 (womptake)"
TIMEOUT = 60


def http(url, data=None, headers=None, dest=None):
    req = urllib.request.Request(url, method="POST" if data else "GET")
    req.add_header("User-Agent", UA)
    req.add_header("Origin", SITE)
    req.add_header("Referer", SITE + "/discover")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(data).encode()
    if dest:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(dest, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        return None
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def frontend_key(cached=""):
    """guest session, same as the anonymous web client."""
    body = http(API + "/frontend-session/refresh", data={"frontendKey": cached})
    return body["data"]["frontendKey"]


def search(query, limit=30, fk=None):
    fk = fk or frontend_key()
    q = urllib.parse.urlencode({"q": query, "limit": str(min(limit, 200))})
    return http(API + "/search/discover?" + q,
                headers={"womp-frontend-key": fk})


def get_project(target, fk=None):
    fk = fk or frontend_key()
    return http(API + "/project/" + urllib.parse.quote(str(target)),
                headers={"womp-frontend-key": fk})


def resolve_target(arg):
    """accept a link slug, a numeric id, or any womp url containing either."""
    arg = arg.strip().rstrip("/")
    m = re.search(r"(?:aph=|preview/|projects/|shared/)([0-9a-z]+)", arg, re.I)
    if m and ("." in arg or "/" in arg):
        return m.group(1)
    return arg


def fetch_image(url, dest):
    try:
        http(url, dest=dest)
        return True
    except Exception as e:
        print("  image failed: %s (%s)" % (url.rsplit("/", 1)[-1][:40], e))
        return False


def cmd_search(args):
    query = args[0]
    limit = int(args[1]) if len(args) > 1 else 30
    res = search(query, limit)
    items = res.get("data") or []
    items = items[:limit]
    print("%d results (of %d total) for %r" % (len(items), res.get("totalCount", 0), query))
    for it in items:
        lic = (it.get("ext") or {}).get("license") or "?"
        print("  %-16s %-28s by %-12s %s  likes %-4d views %-5d %s"
              % (it.get("link", "?")[:16], (it.get("title") or "?")[:28],
                 (it.get("username") or "?")[:12], "license " + lic,
                 it.get("likes", 0), it.get("views", 0),
                 SITE + "/discover?aph=" + it.get("link", "")))


def cmd_take(args):
    target = resolve_target(args[0])
    outdir = args[1] if len(args) > 1 else "womp_" + (target or "out")
    outdir = outdir.rstrip("/") or "."
    fk = frontend_key()

    proj = get_project(target, fk)
    if not proj.get("id"):
        sys.exit("project %r not found or not public" % target)

    import os
    os.makedirs(os.path.join(outdir, "images"), exist_ok=True)

    meta = dict(proj)
    meta["takenFrom"] = SITE + "/discover?aph=" + str(proj.get("link", ""))
    with open(os.path.join(outdir, "project.json"), "w") as f:
        json.dump(meta, f, indent=1, sort_keys=True)

    ext = proj.get("ext") or {}
    print("took home: %s" % (proj.get("title") or "(untitled)"))
    print("  author:   %s" % (proj.get("username") or "?"))
    print("  license:  %s" % (ext.get("license") or "?"))
    print("  published:%s  likes %s, views %s"
          % (proj.get("publishedAt", "?")[:10], proj.get("likes", 0), proj.get("views", 0)))
    n_for = proj.get("forkedFromId")
    if n_for:
        print("  forked from project id %s" % n_for)
    for side in (proj.get("remixings") or [], proj.get("remixeds") or []):
        for r in side[:3]:
            print("  remix: %-24s (%s)" % ((r.get("title") or "?")[:24],
                                           (r.get("publishedAt") or "")[:10]))

    got = []
    for tag, url in (("preview", proj.get("preview")),
                     ("thumbnail", proj.get("thumbnailUrl"))):
        if url:
            name = tag + os.path.splitext(urllib.parse.urlparse(url).path)[1]
            if fetch_image(url, os.path.join(outdir, "images", name)):
                got.append("images/" + name)
    if got:
        print("  images:   %s" % ", ".join(got))
    else:
        print("  images:   none published")

    print("  saved:    %s/project.json" % outdir)
    print("note: the 3d scene itself needs a womp login; only public"
          " metadata and renders are fetchable anonymously.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd, args = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "search":
            if not args:
                sys.exit('search needs a query: womptake.py search "cat" 20')
            cmd_search(args)
        elif cmd in ("--help", "-h"):
            print(__doc__)
        else:
            cmd_take([cmd] + args)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            sys.exit("womp rate-limited this ip; wait a minute and retry.")
        sys.exit("womp api error: %s %s" % (e.code, e.reason))


if __name__ == "__main__":
    main()
