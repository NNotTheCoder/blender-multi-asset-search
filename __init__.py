bl_info = {
    "name": "Multi-Platform Asset Search Aggregator",
    "author": "Omar Abdelfattah",
    "version": (3, 5, 0),
    "blender": (3, 4, 0),
    "location": "View3D > Sidebar > Asset Search",
    "description": "Search Sketchfab, Poly Haven, and Poly Pizza — auto-import glTF/glb/obj directly into Blender.",
    "category": "3D View",
}

import bpy
import urllib.request
import urllib.error
import urllib.parse
import json
import threading
import tempfile
import os
import ssl
import zipfile
import concurrent.futures
import webbrowser

# ---------------------------------------------------------------------------
# No default keys ship in this file. Each user enters their own Sketchfab
# token and Poly Pizza key in the addon Preferences — nothing to leak, and
# no shared quota between users.
# ---------------------------------------------------------------------------
_DEFAULT_SF_TOKEN = ""  # get one at sketchfab.com/settings/password (API tab)
_DEFAULT_PP_KEY   = ""  # get one at poly.pizza/settings/api

# ---------------------------------------------------------------------------
# Global State
# ---------------------------------------------------------------------------
_state = {
    "all_results":     [],
    "filtered":        [],
    "is_searching":    False,
    "status":          "Ready",
    "error":           "",
    "page":            0,
    "api_offset":      0,
    "platform_counts": {},
}

RESULTS_PER_PAGE = 20
_PH_CACHE = {}

# Poly Pizza uses opaque cursor-based pagination (not numeric offsets).
# Keyed by query string so a fresh search always starts from page 1.
_PP_CURSORS = {}

# ---------------------------------------------------------------------------
# Addon Preferences
# ---------------------------------------------------------------------------

class AGGREGATOR_Preferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    sketchfab_token: bpy.props.StringProperty(
        name="Sketchfab API Token",
        default=_DEFAULT_SF_TOKEN,
        subtype="PASSWORD",
    )
    poly_pizza_key: bpy.props.StringProperty(
        name="Poly Pizza API Key",
        default=_DEFAULT_PP_KEY,
        subtype="PASSWORD",
    )
    results_per_platform: bpy.props.IntProperty(
        name="API Fetch Batch Size",
        default=48, min=10, max=100, step=10,
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="API Keys:", icon="LOCKED")
        col = layout.column(align=True)
        col.prop(self, "sketchfab_token")
        col.prop(self, "poly_pizza_key")
        layout.separator()
        layout.prop(self, "results_per_platform")
        layout.label(text="Poly Haven needs no key.", icon="INFO")

def _prefs(ctx=None):
    try:
        return (ctx or bpy.context).preferences.addons[__name__].preferences
    except Exception:
        return None

def _sf_token(ctx=None):
    p = _prefs(ctx)
    return (p.sketchfab_token.strip() if p else "") or _DEFAULT_SF_TOKEN

def _pp_key(ctx=None):
    p = _prefs(ctx)
    return (p.poly_pizza_key.strip() if p else "") or _DEFAULT_PP_KEY

def _rpp(ctx=None):
    p = _prefs(ctx)
    return p.results_per_platform if p else 48

# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _ssl():
    return ssl._create_unverified_context()

def _get(url, headers=None, timeout=12):
    h = {"User-Agent": "Blender-Asset-Aggregator/3.5.0"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, context=_ssl(), timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        # urlopen raises on 4xx/5xx instead of returning them — catch it so
        # callers can actually see and react to status codes like 401/429.
        return e.code, e.read()

def _download(url, dest, headers=None):
    try:
        _, raw = _get(url, headers=headers, timeout=30)
        with open(dest, "wb") as f:
            f.write(raw)
        return True
    except Exception as e:
        print(f"[DL] {url}: {e}")
        return False

# ---------------------------------------------------------------------------
# Import pipeline
# ---------------------------------------------------------------------------

def _tmpdir():
    d = os.path.join(tempfile.gettempdir(), "baa350")
    os.makedirs(d, exist_ok=True)
    return d

def _do_import(filepath):
    ext = os.path.splitext(filepath)[1].lower()
    if ext in (".gltf", ".glb"):
        if hasattr(bpy.ops.import_scene, "gltf"):
            bpy.ops.import_scene.gltf(filepath=filepath)
        elif hasattr(bpy.ops.wm, "gltf_import"):
            bpy.ops.wm.gltf_import(filepath=filepath)
        else:
            raise RuntimeError("Enable 'Import-Export: glTF 2.0' addon first.")
    elif ext == ".obj":
        if hasattr(bpy.ops.wm, "obj_import"):
            bpy.ops.wm.obj_import(filepath=filepath)
        elif hasattr(bpy.ops.import_scene, "obj"):
            bpy.ops.import_scene.obj(filepath=filepath)
        else:
            raise RuntimeError("Enable 'Import-Export: Wavefront OBJ' addon first.")

def import_asset(url, deps: dict) -> bool:
    td  = _tmpdir()
    raw = os.path.join(td, f"dl_{abs(hash(url))}")

    if not _download(url, raw):
        return False

    target = None

    if zipfile.is_zipfile(raw):
        out = raw + "_x"
        os.makedirs(out, exist_ok=True)
        with zipfile.ZipFile(raw, "r") as zf:
            zf.extractall(out)
        for root, _, files in os.walk(out):
            for fn in files:
                if fn.lower().endswith((".gltf", ".glb", ".obj")):
                    target = os.path.join(root, fn)
                    break
            if target:
                break
    else:
        for fname, dep_url in deps.items():
            _download(dep_url, os.path.join(td, fname))
        ext    = ".glb" if ".glb" in url.lower() else ".gltf"
        target = raw + ext
        if os.path.exists(raw):
            os.replace(raw, target)

    if target and os.path.exists(target):
        try:
            _do_import(target)
            return True
        except Exception as e:
            print(f"[Import] {target}: {e}")

    return False

# ---------------------------------------------------------------------------
# Platform fetchers
# ---------------------------------------------------------------------------

def fetch_sketchfab(query, token, count, offset):
    out = []
    if not token:
        return out
    try:
        url = (
            "https://api.sketchfab.com/v3/search"
            f"?type=models&q={urllib.parse.quote(query)}"
            f"&downloadable=true&count={count}&cursor={offset}"
        )
        status, raw = _get(url, {"Authorization": f"Token {token}"})
        if status == 200:
            for m in json.loads(raw).get("results", []):
                uid   = m.get("uid", "")
                imgs  = m.get("thumbnails", {}).get("images", [])
                thumb = max(imgs, key=lambda i: i.get("width", 0), default={}).get("url", "")
                out.append({
                    "uid":          f"sf_{uid}",
                    "title":        m.get("name", "Untitled"),
                    "platform":     "Sketchfab",
                    "author":       m.get("user", {}).get("username", "Unknown"),
                    "download_url": m.get("viewerUrl", f"https://sketchfab.com/3d-models/{uid}"),
                    "thumbnail_url": thumb,
                    "is_direct":    False,
                    "dependencies": {},
                    "tags":         [t.get("name", "") for t in m.get("tags", [])[:6]],
                    "face_count":   m.get("faceCount", 0),
                    "license":      m.get("license", {}).get("label", ""),
                })
        elif status == 401:
            _state["error"] = "Sketchfab: bad token (401)"
        elif status == 429:
            _state["error"] = "Sketchfab: rate limited (429)"
    except Exception as e:
        print(f"[Sketchfab] {e}")
    return out


def fetch_poly_pizza(query, key, count, offset):
    """Poly Pizza API v1.1: GET /search/{query} (query is a PATH segment, not
    a `q=` param), auth via X-Auth-Token header, and pagination via an opaque
    `cursor` string returned in the response — NOT a numeric offset. We keep
    a per-query cursor cache so "Load More" (offset > 0) resumes correctly,
    while a fresh search (offset == 0) always starts clean.
    """
    out = []
    if not key:
        return out
    try:
        cursor = _PP_CURSORS.get(query) if offset > 0 else None
        # Path-encode the query (safe='' so slashes etc. get escaped too).
        url = f"https://api.poly.pizza/v1/search/{urllib.parse.quote(query, safe='')}?limit={min(count, 50)}"
        if cursor:
            url += f"&cursor={urllib.parse.quote(cursor, safe='')}"

        status, raw = _get(url, {"X-Auth-Token": key})

        if status == 200:
            data = json.loads(raw)
            # Remember the cursor for this query so the next "Load More" continues
            # instead of re-fetching page 1. A null/missing cursor means no more pages.
            _PP_CURSORS[query] = data.get("cursor")

            for item in data.get("results", []):
                iid     = item.get("ID", "")
                dl      = item.get("Download", "") or ""
                creator = item.get("Creator") or {}
                out.append({
                    "uid":          f"pp_{iid}",
                    "title":        item.get("Title", "Untitled"),
                    "platform":     "Poly Pizza",
                    "author":       creator.get("Username", "Poly Pizza"),
                    "download_url": dl or f"https://poly.pizza/m/{iid}",
                    "thumbnail_url": item.get("Thumbnail", ""),
                    "is_direct":    dl.lower().endswith((".glb", ".gltf")) if dl else False,
                    "dependencies": {},
                    "tags":         item.get("Tags", []),
                    "face_count":   item.get("TriangleCount", 0),
                    "license":      item.get("Licence", "CC0"),
                })
        elif status == 401:
            _state["error"] = "Poly Pizza: bad key (401)"
        elif status == 404:
            # No matches for this query — not an error, just an empty page.
            pass
        elif status == 429:
            _state["error"] = "Poly Pizza: rate limited (429)"
        else:
            _state["error"] = f"Poly Pizza: HTTP {status}"
    except Exception as e:
        print(f"[Poly Pizza] {e}")
    return out


def _ph_single(aid):
    if aid in _PH_CACHE:
        return _PH_CACHE[aid]
    try:
        _, raw    = _get(f"https://api.polyhaven.com/files/{aid}")
        fdata     = json.loads(raw)
        gltf_info = (
            fdata.get("gltf", {}).get("1k", {}).get("gltf", {}) or
            fdata.get("gltf", {}).get("2k", {}).get("gltf", {})
        )
        main_url  = gltf_info.get("url")
        if not main_url:
            return None
        deps = {
            os.path.basename(k): v["url"]
            for k, v in gltf_info.get("include", {}).items() if "url" in v
        }
        r = {
            "uid":          f"ph_{aid}",
            "title":        aid.replace("_", " ").title(),
            "platform":     "Poly Haven",
            "author":       "Poly Haven",
            "download_url": main_url,
            "thumbnail_url": f"https://cdn.polyhaven.com/asset_img/thumbs/{aid}.png?width=128",
            "is_direct":    True,
            "dependencies": deps,
            "tags":         [],
            "face_count":   0,
            "license":      "CC0",
        }
        _PH_CACHE[aid] = r
        return r
    except Exception as e:
        print(f"[Poly Haven] {aid}: {e}")
        return None

def fetch_poly_haven(query, count, offset):
    out = []
    try:
        _, raw  = _get("https://api.polyhaven.com/assets?type=models")
        catalog = json.loads(raw)
        q       = query.lower().strip()
        words   = q.split()
        stems   = list(set(words + [q]))

        def score(k):
            name = k.lower()
            tags = [t.lower() for t in catalog[k].get("tags", [])]
            best = 0
            for stem in stems:
                if name == stem:                        best = max(best, 5)
                elif name.startswith(stem):             best = max(best, 4)
                elif stem in name:                      best = max(best, 3)
                elif any(stem == t for t in tags):      best = max(best, 2)
                elif any(stem in t for t in tags):      best = max(best, 1)
            return best

        ranked = sorted((k for k in catalog if score(k) > 0), key=score, reverse=True)
        chunk  = ranked[offset : offset + count]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            for r in ex.map(_ph_single, chunk):
                if r:
                    out.append(r)
    except Exception as e:
        print(f"[Poly Haven] {e}")
    return out

# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------

def _worker(query, sf_tok, pp_key, rpp, offset, append=False):
    if not append:
        _state.update(all_results=[], page=0, platform_counts={})
        _PP_CURSORS.clear()

    _state.update(is_searching=True, status="Fetching from web...", error="")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            f_sf = ex.submit(fetch_sketchfab,  query, sf_tok, rpp, offset)
            f_pp = ex.submit(fetch_poly_pizza, query, pp_key, rpp, offset)
            f_ph = ex.submit(fetch_poly_haven, query,         rpp, offset)

            new_results = f_ph.result() + f_sf.result() + f_pp.result()

        bpy.app.timers.register(lambda: _finish(new_results, append), first_interval=0.0)
    except Exception as e:
        err = f"Search error: {e}"
        print(f"[Worker] {err}")
        bpy.app.timers.register(lambda: _finish([], False, err), first_interval=0.0)

def _finish(new_results, append=False, error=""):
    if append:
        _state["all_results"].extend(new_results)
    else:
        _state["all_results"] = new_results

    counts = {}
    for r in _state["all_results"]:
        counts[r["platform"]] = counts.get(r["platform"], 0) + 1

    total_loaded = len(_state["all_results"])
    _state.update(
        platform_counts = counts,
        error           = error,
        is_searching    = False,
        status          = f"Loaded {total_loaded} results" if not error else "Error — see System Console",
    )
    _apply_filter()
    _redraw()
    return None

def _apply_filter():
    try:
        scene = bpy.context.scene
        mode  = scene.asset_filter_mode
        plat  = scene.asset_platform_filter
        pool  = _state["all_results"]

        if mode == "DIRECT":
            pool = [i for i in pool if i["is_direct"]]
        elif mode == "WEB":
            pool = [i for i in pool if not i["is_direct"]]

        if plat != "ALL":
            pmap = {"SKETCHFAB": "Sketchfab", "POLYHAVEN": "Poly Haven", "POLYPIZZA": "Poly Pizza"}
            pool = [i for i in pool if i["platform"] == pmap.get(plat)]

        _state["filtered"] = pool
        _sync_page(pool)
    except Exception as e:
        print(f"[Filter] {e}")

def _sync_page(pool):
    scene = bpy.context.scene
    scene.asset_list_collection.clear()
    start = _state["page"] * RESULTS_PER_PAGE
    for item in pool[start: start + RESULTS_PER_PAGE]:
        e = scene.asset_list_collection.add()
        e.title         = item["title"][:80]
        e.platform      = item["platform"]
        e.author        = item["author"][:48]
        e.download_url  = item["download_url"]
        e.thumbnail_url = item.get("thumbnail_url", "")
        e.is_direct     = item["is_direct"]
        e.uid           = item["uid"]
        e.deps_json     = json.dumps(item.get("dependencies", {}))
        e.tags          = ", ".join(item.get("tags", [])[:5])
        e.face_count    = item.get("face_count", 0)
        e.license       = item.get("license", "")

def _redraw():
    for win in bpy.context.window_manager.windows:
        for area in win.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()

# ---------------------------------------------------------------------------
# Property Group
# ---------------------------------------------------------------------------

class AGGREGATOR_PG_AssetItem(bpy.types.PropertyGroup):
    title:         bpy.props.StringProperty()
    platform:      bpy.props.StringProperty()
    author:        bpy.props.StringProperty()
    download_url:  bpy.props.StringProperty()
    thumbnail_url: bpy.props.StringProperty()
    is_direct:     bpy.props.BoolProperty()
    uid:           bpy.props.StringProperty()
    deps_json:     bpy.props.StringProperty(default="{}")
    tags:          bpy.props.StringProperty()
    face_count:    bpy.props.IntProperty()
    license:       bpy.props.StringProperty()

# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class AGGREGATOR_OT_Search(bpy.types.Operator):
    """Search Sketchfab, Poly Haven, and Poly Pizza"""
    bl_idname  = "aggregator.search"
    bl_label   = "Search"
    bl_options = {"REGISTER"}

    def execute(self, context):
        query = context.scene.asset_search_query.strip()
        if not query:
            self.report({"WARNING"}, "Enter a search term first.")
            return {"CANCELLED"}
        if _state["is_searching"]:
            self.report({"WARNING"}, "Search already running.")
            return {"CANCELLED"}

        _state["page"] = 0
        _state["api_offset"] = 0

        threading.Thread(
            target=_worker,
            args=(query, _sf_token(context), _pp_key(context), _rpp(context), 0, False),
            daemon=True,
        ).start()
        return {"FINISHED"}

class AGGREGATOR_OT_LoadMore(bpy.types.Operator):
    """Fetch next batch of results from APIs"""
    bl_idname = "aggregator.load_more"
    bl_label  = "Load More from Web"

    def execute(self, context):
        query = context.scene.asset_search_query.strip()
        if _state["is_searching"] or not query:
            return {"CANCELLED"}

        batch_size = _rpp(context)
        _state["api_offset"] += batch_size

        threading.Thread(
            target=_worker,
            args=(query, _sf_token(context), _pp_key(context), batch_size, _state["api_offset"], True),
            daemon=True,
        ).start()
        return {"FINISHED"}

class AGGREGATOR_OT_PageNext(bpy.types.Operator):
    """Next page of results"""
    bl_idname = "aggregator.page_next"
    bl_label  = "Next"

    def execute(self, context):
        total    = len(_state["filtered"])
        max_page = max(0, (total - 1) // RESULTS_PER_PAGE) if total else 0
        if _state["page"] < max_page:
            _state["page"] += 1
            _sync_page(_state["filtered"])
            _redraw()
        return {"FINISHED"}

class AGGREGATOR_OT_PagePrev(bpy.types.Operator):
    """Previous page of results"""
    bl_idname = "aggregator.page_prev"
    bl_label  = "Prev"

    def execute(self, context):
        if _state["page"] > 0:
            _state["page"] -= 1
            _sync_page(_state["filtered"])
            _redraw()
        return {"FINISHED"}

class AGGREGATOR_OT_HandleAsset(bpy.types.Operator):
    """Import asset into scene or open in browser"""
    bl_idname  = "aggregator.handle_asset"
    bl_label   = "Get Asset"
    bl_options = {"REGISTER", "UNDO"}

    uid:       bpy.props.StringProperty(options={"HIDDEN"})
    url:       bpy.props.StringProperty(options={"HIDDEN"})
    platform:  bpy.props.StringProperty(options={"HIDDEN"})
    is_direct: bpy.props.BoolProperty(options={"HIDDEN"})
    title:     bpy.props.StringProperty(options={"HIDDEN"})
    deps_json: bpy.props.StringProperty(options={"HIDDEN"}, default="{}")

    def execute(self, context):
        if not self.url:
            self.report({"ERROR"}, "No URL available.")
            return {"CANCELLED"}

        target = self.url
        deps   = json.loads(self.deps_json) if self.deps_json else {}

        if self.platform == "Sketchfab":
            token = _sf_token(context)
            if not token:
                self.report({"WARNING"}, "No Sketchfab token — opening browser.")
                webbrowser.open(self.url)
                return {"FINISHED"}
            try:
                raw_uid = self.uid.replace("sf_", "")
                status, raw = _get(
                    f"https://api.sketchfab.com/v3/models/{raw_uid}/download",
                    {"Authorization": f"Token {token}"},
                )
                if status == 200:
                    dl     = json.loads(raw)
                    target = dl.get("gltf", {}).get("url") or dl.get("glb", {}).get("url") or self.url
                elif status == 403:
                    self.report({"WARNING"}, "Not downloadable on your Sketchfab plan — opening browser.")
                    webbrowser.open(self.url)
                    return {"FINISHED"}
                else:
                    raise RuntimeError(f"HTTP {status}")
            except Exception as e:
                self.report({"WARNING"}, f"Sketchfab resolve failed ({e}) — opening browser.")
                webbrowser.open(self.url)
                return {"FINISHED"}

        looks_gltf_or_obj = any(x in target.lower() for x in ("glb", "gltf", "obj", "sketchfab-prod-media", "poly.pizza"))
        if self.is_direct or looks_gltf_or_obj:
            self.report({"INFO"}, f"Downloading '{self.title}'…")
            if import_asset(target, deps):
                self.report({"INFO"}, f"Imported '{self.title}'.")
                return {"FINISHED"}
            self.report({"WARNING"}, "Import failed — opening browser.")

        webbrowser.open(self.url)
        self.report({"INFO"}, f"Opened browser for '{self.title}'.")
        return {"FINISHED"}

class AGGREGATOR_OT_CopyURL(bpy.types.Operator):
    """Copy asset URL to clipboard"""
    bl_idname = "aggregator.copy_url"
    bl_label  = "Copy URL"
    url: bpy.props.StringProperty(options={"HIDDEN"})

    def execute(self, context):
        context.window_manager.clipboard = self.url
        self.report({"INFO"}, "URL copied.")
        return {"FINISHED"}

class AGGREGATOR_OT_ClearResults(bpy.types.Operator):
    """Clear all search results"""
    bl_idname = "aggregator.clear"
    bl_label  = "Clear"

    def execute(self, context):
        _state.update(all_results=[], filtered=[], page=0, api_offset=0,
                      status="Ready", error="", platform_counts={})
        _PP_CURSORS.clear()
        context.scene.asset_list_collection.clear()
        _redraw()
        return {"FINISHED"}

# ---------------------------------------------------------------------------
# Filter callback
# ---------------------------------------------------------------------------

def _filter_cb(self, context):
    _state["page"] = 0
    _apply_filter()
    _redraw()

# ---------------------------------------------------------------------------
# UI Panel
# ---------------------------------------------------------------------------

class AGGREGATOR_PT_Panel(bpy.types.Panel):
    bl_label       = "Asset Search"
    bl_idname      = "AGGREGATOR_PT_panel"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "Asset Search"

    def draw(self, context):
        layout = self.layout
        scene  = context.scene

        row = layout.row(align=True)
        row.prop(scene, "asset_search_query", text="", icon="VIEWZOOM")
        go = row.row(align=True)
        go.enabled = not _state["is_searching"]
        go.operator("aggregator.search", text="Go", icon="VIEWZOOM")
        row.operator("aggregator.clear", text="", icon="X")

        if _state["error"]:
            layout.label(text=_state["error"][:64], icon="ERROR")
        elif _state["is_searching"]:
            layout.label(text=_state["status"], icon="TIME")
        elif _state["status"] != "Ready":
            layout.label(text=_state["status"], icon="CHECKMARK")

        prefs = _prefs(context)
        missing = []
        if not _sf_token(context):
            missing.append("Sketchfab")
        if not _pp_key(context):
            missing.append("Poly Pizza")
        if missing and prefs:
            box = layout.box()
            box.label(text=f"Enter your {', '.join(missing)} key to search that platform:", icon="KEYINGSET")
            col = box.column(align=True)
            if not _sf_token(context):
                col.prop(prefs, "sketchfab_token", text="Sketchfab")
            if not _pp_key(context):
                col.prop(prefs, "poly_pizza_key", text="Poly Pizza")
            row = box.row()
            row.operator("wm.url_open", text="Get Sketchfab key", icon="URL").url = "https://sketchfab.com/settings/password"
            row.operator("wm.url_open", text="Get Poly Pizza key", icon="URL").url = "https://poly.pizza/settings/api"
            box.label(text="Saved automatically — enter once.", icon="INFO")

        box = layout.box()
        row = box.row(align=True)
        row.label(text="Filter:", icon="FILTER")
        row.prop(scene, "asset_filter_mode",     text="")
        row.prop(scene, "asset_platform_filter", text="")

        if _state["platform_counts"]:
            row = box.row(align=True)
            short = {"Sketchfab": "SF", "Poly Haven": "PH", "Poly Pizza": "PP"}
            for plat, cnt in _state["platform_counts"].items():
                row.label(text=f"{short.get(plat, plat)}: {cnt}")

        if _state["is_searching"]:
            return

        layout.separator(factor=0.5)

        total    = len(_state["filtered"])
        page     = _state["page"]
        max_page = max(0, (total - 1) // RESULTS_PER_PAGE) if total else 0

        if not total:
            layout.label(
                text="No results match filters." if _state["all_results"] else "Search to get started.",
                icon="INFO",
            )
            return

        row = layout.row(align=True)
        prev = row.row(align=True)
        prev.enabled = page > 0
        prev.operator("aggregator.page_prev", text="", icon="TRIA_LEFT")
        row.label(text=f"  Page {page + 1}/{max_page + 1}  ({total} results)  ")
        nxt = row.row(align=True)
        nxt.enabled = page < max_page
        nxt.operator("aggregator.page_next", text="", icon="TRIA_RIGHT")

        for item in scene.asset_list_collection:
            box = layout.box()
            col = box.column(align=True)

            row = col.row(align=True)
            icon = "IMPORT" if item.is_direct else "WORLD"
            op   = row.operator("aggregator.handle_asset", text=item.title[:36], icon=icon)
            op.uid       = item.uid
            op.url       = item.download_url
            op.platform  = item.platform
            op.is_direct = item.is_direct
            op.title     = item.title
            op.deps_json = item.deps_json

            cp_row = row.row(align=True)
            cp_row.scale_x = 0.28
            cp = cp_row.operator("aggregator.copy_url", text="", icon="COPYDOWN")
            cp.url = item.download_url

            parts = [f"[{item.platform}]", item.author]
            if item.face_count:
                parts.append(f"{item.face_count:,} tri")
            if item.license:
                parts.append(item.license)
            col.label(text="  ".join(parts))

            if item.tags:
                col.label(text=f"# {item.tags}")

        if page == max_page and total > 0:
            layout.separator()
            layout.operator("aggregator.load_more", icon="IMPORT")

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_classes = (
    AGGREGATOR_Preferences,
    AGGREGATOR_PG_AssetItem,
    AGGREGATOR_OT_Search,
    AGGREGATOR_OT_LoadMore,
    AGGREGATOR_OT_PageNext,
    AGGREGATOR_OT_PagePrev,
    AGGREGATOR_OT_HandleAsset,
    AGGREGATOR_OT_CopyURL,
    AGGREGATOR_OT_ClearResults,
    AGGREGATOR_PT_Panel,
)

def register():
    for cls in _classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)

    bpy.types.Scene.asset_search_query = bpy.props.StringProperty(name="Search", default="")
    bpy.types.Scene.asset_list_collection = bpy.props.CollectionProperty(type=AGGREGATOR_PG_AssetItem)
    bpy.types.Scene.asset_filter_mode = bpy.props.EnumProperty(
        name="Type",
        items=[
            ("ALL",    "All",    "Show all results"),
            ("DIRECT", "Import", "Auto-import only"),
            ("WEB",    "Web",    "Browser-open only"),
        ],
        default="ALL",
        update=_filter_cb,
    )
    bpy.types.Scene.asset_platform_filter = bpy.props.EnumProperty(
        name="Platform",
        items=[
            ("ALL",       "All",        ""),
            ("SKETCHFAB", "Sketchfab",  ""),
            ("POLYHAVEN", "Poly Haven", ""),
            ("POLYPIZZA", "Poly Pizza", ""),
        ],
        default="ALL",
        update=_filter_cb,
    )


def unregister():
    _state["is_searching"] = False
    for cls in reversed(_classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    for attr in ("asset_search_query", "asset_list_collection",
                 "asset_filter_mode", "asset_platform_filter"):
        try:
            delattr(bpy.types.Scene, attr)
        except AttributeError:
            pass


if __name__ == "__main__":
    register()
