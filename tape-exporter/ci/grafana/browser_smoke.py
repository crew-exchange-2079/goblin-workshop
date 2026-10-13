"""Headless-browser smoke check for the provisioned dashboard (optional part
of smoke.py --browser). Needs the `playwright` Python package and a Chromium
(CHROME_PATH, else Playwright's own).

What it does prove: the real Grafana frontend loads the dashboard, renders
every panel header, substitutes the variables into the queries it sends,
and shows no panel error indicator; the same check DOES report errors when
the dashboard is pointed at the unreachable datasource (negative control).
Screenshots are kept as evidence and checked to be non-blank, unobstructed
PNGs of the expected size. What it does NOT prove: that the layout, colours
or readability are right. Nobody should read a pass here as a visual review.

All requests other than to the local Grafana are aborted.
"""

import os
import re
import struct
import urllib.parse
import zlib

import smoke

VIEWPORT = {"width": 1600, "height": 1600}
ERROR_SEL = '[data-testid="data-testid Panel status error"]'
HEADER_SEL = '[data-testid^="data-testid Panel header "]'


# --------------------------------------------------------------------------
# PNG check (stdlib decoder for the 8-bit RGB/RGBA PNGs Chromium writes)
# --------------------------------------------------------------------------

def png_pixels(data):
    """Return (width, height, rows) where rows are lists of RGB tuples."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, idat, ihdr = 8, b"", None
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        if kind == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        elif kind == b"IEND":
            break
        pos += 12 + n
    if not ihdr:
        raise ValueError("PNG without IHDR")
    w, h, depth, ctype, _, _, interlace = ihdr
    if depth != 8 or ctype not in (2, 6) or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, colour type {ctype}, interlace {interlace})")
    bpp = 3 if ctype == 2 else 4
    raw = zlib.decompress(idat)
    stride = w * bpp
    prev = bytearray(stride)
    rows, i = [], 0
    for _ in range(h):
        f, line = raw[i], bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                line[x] = (line[x] + a) & 255
            elif f == 2:
                line[x] = (line[x] + b) & 255
            elif f == 3:
                line[x] = (line[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
            elif f != 0:
                raise ValueError(f"bad PNG filter {f}")
        rows.append([tuple(line[x:x + 3]) for x in range(0, stride, bpp)])
        prev = line
    return w, h, rows


def check_png(path, width, height, min_colours=32):
    with open(path, "rb") as fh:
        data = fh.read()
    try:
        w, h, rows = png_pixels(data)
    except (ValueError, zlib.error, struct.error) as e:
        return [f"{os.path.basename(path)}: unreadable PNG ({e})"]
    errs = []
    if (w, h) != (width, height):
        errs.append(f"{os.path.basename(path)}: {w}x{h}, expected {width}x{height}")
    sample = {px for row in rows[::7] for px in row[::7]}
    if len(sample) < min_colours:
        errs.append(f"{os.path.basename(path)}: only {len(sample)} distinct colours (blank page?)")
    return errs


# --------------------------------------------------------------------------
# Browser views
# --------------------------------------------------------------------------

def dashboard_url(base, ds, instance="All", device="All"):
    q = [("orgId", "1"), ("from", smoke.QUERY_RANGE[0]), ("to", smoke.QUERY_RANGE[1]),
         ("refresh", ""), ("var-datasource", ds)]
    for name, val in (("instance", instance), ("device", device)):
        for v in (["$__all"] if val == "All" else val):
            q.append((f"var-{name}", v))
    return f"{base}/d/{smoke.DASHBOARD_UID}/?{urllib.parse.urlencode(q)}"


def load_view(ctx, url, titles, shot, timeout_ms=60000):
    """Open one dashboard view and return what the browser saw."""
    page = ctx.new_page()
    seen = {"queries": [], "responses": [], "http_errors": []}

    def on_request(req):
        if "/api/ds/query" in req.url and req.method == "POST":
            try:
                seen["queries"] += [q.get("expr", "") for q in (req.post_data_json or {}).get("queries", [])]
            except ValueError:
                pass

    def on_response(resp):
        if "/api/ds/query" in resp.url:
            try:
                seen["responses"].append((resp.status, resp.json()))
            except Exception:  # body unavailable / not JSON
                seen["responses"].append((resp.status, None))
        elif resp.status >= 500:
            seen["http_errors"].append(f"{resp.status} {resp.url.split('?')[0]}")

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        # Grafana 13 may show a "what's new" splash; it must not cover panels
        for _ in range(3):
            dialog = page.locator('[role="dialog"]:visible')
            if not dialog.count():
                break
            close = dialog.first.get_by_role("button", name=re.compile(r"close|dismiss", re.I))
            if close.count():
                close.first.click()
            else:
                page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        for t in titles:
            page.locator(f'[data-testid="data-testid Panel header {t}"]').wait_for(timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        page.wait_for_timeout(1500)
        seen["headers"] = page.locator(HEADER_SEL).count()
        seen["panel_errors"] = page.locator(ERROR_SEL).count()
        seen["dialog_visible"] = page.locator('[role="dialog"]:visible').count() > 0
        seen["no_data"] = page.get_by_text("No data", exact=True).count()
        seen["text"] = page.inner_text("main") if page.locator("main").count() else page.inner_text("body")
        page.screenshot(path=shot)
    finally:
        page.close()
    return seen


def frontend_series(seen):
    out, errs = [], []
    for status, body in seen["responses"]:
        results = (body or {}).get("results", {}) if isinstance(body, dict) else {}
        if status != 200:
            errs.append(f"/api/ds/query HTTP {status}")
        errs += smoke.query_errors(results)
        for r in results.values():
            out += smoke.frames_to_series(r)
    return out, errs


def check_view(name, seen, titles, expect):
    errs = []
    if seen["headers"] < len(titles):
        errs.append(f"{name}: {seen['headers']}/{len(titles)} panel headers rendered")
    if seen["dialog_visible"]:
        errs.append(f"{name}: a dialog still covers the dashboard (screenshot obstructed)")
    series, qerrs = frontend_series(seen)
    if not seen["queries"]:
        errs.append(f"{name}: the frontend sent no /api/ds/query requests")
    if expect.get("errors"):
        if seen["panel_errors"] < len(titles) or not qerrs:
            errs.append(f"{name}: expected every panel in error, saw {seen['panel_errors']} error "
                        f"indicators and {len(qerrs)} query errors: the check would miss failures")
        return errs
    errs += [f"{name}: {e}" for e in seen["http_errors"]]
    if seen["panel_errors"]:
        errs.append(f"{name}: {seen['panel_errors']} panel(s) show an error indicator")
    errs += [f"{name}: {e}" for e in qerrs]
    if "min_no_data" in expect and seen["no_data"] < expect["min_no_data"]:
        errs.append(f"{name}: {seen['no_data']} 'No data' panels, expected >= {expect['min_no_data']}"
                    " (missing counters must not render as values)")
    if "max_no_data" in expect and seen["no_data"] > expect["max_no_data"]:
        errs.append(f"{name}: {seen['no_data']} 'No data' panels, expected <= {expect['max_no_data']}")
    if "device" in expect:
        rx = re.compile(r'device=~"\(?' + re.escape(expect["device"]) + r'\)?"')
        bad = [q for q in seen["queries"] if "$device" in q or ("device=~" in q and not rx.search(q))]
        if bad:
            errs.append(f"{name}: device variable not substituted as expected: {bad[:2]}")
        devs = {s["labels"]["device"] for s in series if "device" in s["labels"]}
        if devs - {expect["device"]}:
            errs.append(f"{name}: frontend results include devices {sorted(devs)}")
        if not devs:
            errs.append(f"{name}: no device series returned at all")
    return errs


def run(base, user, auth_header, dash, art, instances):
    from playwright.sync_api import sync_playwright  # optional dependency

    titles = [p["title"] for p in dash["panels"]]
    views = [
        ("all", dashboard_url(base, smoke.DS_OK), {"max_no_data": 0}),
        ("failing-instance", dashboard_url(base, smoke.DS_OK, [instances["failing"]]),
         {"min_no_data": 5}),
        ("device-drive1", dashboard_url(base, smoke.DS_OK, "All", ["drive1"]), {"device": "drive1"}),
        ("unreachable-datasource", dashboard_url(base, smoke.DS_BROKEN), {"errors": True}),
    ]
    errs, detail, blocked = [], {}, []
    host = urllib.parse.urlsplit(base).netloc
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=os.environ.get("CHROME_PATH") or None)
        try:
            ctx = browser.new_context(viewport=VIEWPORT, locale="en-US", timezone_id="UTC",
                                      extra_http_headers={"Authorization": auth_header})

            def route(r):
                if urllib.parse.urlsplit(r.request.url).netloc == host:
                    r.continue_()
                else:
                    blocked.append(r.request.url.split("?")[0])
                    r.abort()
            ctx.route("**/*", route)
            detail["browser"] = browser.version
            for name, url, expect in views:
                shot = os.path.join(art, f"screenshot-{name}.png")
                try:
                    seen = load_view(ctx, url, titles, shot)
                except Exception as e:  # playwright timeout etc.
                    errs.append(f"{name}: page did not load: {str(e).splitlines()[0][:300]}")
                    continue
                v_errs = check_view(name, seen, titles, expect)
                if os.path.exists(shot):
                    v_errs += check_png(shot, VIEWPORT["width"], VIEWPORT["height"])
                else:
                    v_errs.append(f"{name}: no screenshot")
                errs += v_errs
                detail[name] = {"panel_headers": seen["headers"], "panel_errors": seen["panel_errors"],
                                "no_data_panels": seen["no_data"], "queries_sent": len(seen["queries"]),
                                "screenshot": os.path.basename(shot), "ok": not v_errs}
                with open(os.path.join(art, f"page-text-{name}.txt"), "w", encoding="utf-8") as fh:
                    fh.write(seen.get("text", ""))
            ctx.close()
        finally:
            browser.close()
    detail["blocked_non_local_requests"] = sorted(set(blocked))
    return errs, detail
