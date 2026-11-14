import json
import os
import pathlib
import sys
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

DRY_RUN = os.environ.get("DRY_RUN") == "1"
TOKEN = os.environ.get("DISCORD_TOKEN", "")
FORUM_ID = os.environ.get("FORUM_CHANNEL_ID", "")
TAG_NAME = os.environ.get("FORUM_TAG_NAME", "").strip().lower()
SKIP_ADULT = os.environ.get("SKIP_ADULT") == "1"

if not DRY_RUN and not (TOKEN and FORUM_ID):
    sys.exit("Нужны секреты DISCORD_TOKEN и FORUM_CHANNEL_ID")

RAW = "https://raw.githubusercontent.com/h6rd/Dota2PornFxWeb/main"
MODS_URL = os.environ.get("MODS_URL") or f"{RAW}/assets/data/mods.json"
PREVIEWS = "https://cdn.jsdelivr.net/gh/h6rd/Dota2PornFxWeb@mirror/assets/previews"
SITE = "https://h6rd.github.io/Dota2PornFxWeb/"
DEFAULT_AUTHOR = "h6rd / Dota2PornFx"

STATE_FILE = pathlib.Path(os.environ.get("STATE_FILE", "state/seen.json"))
MAX_PER_RUN = 5
API = "https://discord.com/api/v10"
HEADERS = {
    "Authorization": f"Bot {TOKEN}",
    "User-Agent": "DiscordBot (mods-feed, 1.0)",
}
SKIP_CATEGORIES = {"news"}
VIDEO_EXT = (".mp4", ".webm")


def load_data() -> dict:
    r = requests.get(MODS_URL, timeout=60)
    r.raise_for_status()
    return r.json()


def find_mod(mods_data: dict, category: str, name: str):
    cat = mods_data.get(category)
    if isinstance(cat, dict) and isinstance(cat.get("groups"), list):
        for g in cat["groups"]:
            for m in g.get("mods", []):
                if m.get("name") == name:
                    return m
    elif isinstance(cat, list):
        for m in cat:
            if isinstance(m, dict) and m.get("name") == name:
                return m
    return None


HF = "https://huggingface.co/datasets/hrdq/Dota2PornFx/resolve/main"


def file_url(category: str, filename: str) -> str:
    if filename.startswith("http"):
        url = filename
    else:
        path = f"assets/files/{category}/{quote(filename, safe='')}"
        url = f"{RAW}/{path}"
        try:
            ok = requests.head(url, timeout=10, allow_redirects=True).ok
        except requests.RequestException:
            ok = False
        if not ok:
            url = f"{HF}/{path}"
    if "huggingface.co" in url and "download=true" not in url:
        url += "?download=true"
    return url


def preview_url(category: str, preview: str):
    if not preview or preview.lower().endswith(VIDEO_EXT):
        return None
    if preview.startswith("http"):
        return preview
    return f"{PREVIEWS}/{category}/{quote(preview, safe='')}"


def get_authors(mod: dict) -> str:
    authors = []
    for link in mod.get("links") or []:
        if isinstance(link, dict) and link.get("type") == "author" and link.get("url"):
            authors.append(str(link["url"]))
    if not authors and mod.get("linkType") == "author" and mod.get("linkUrl"):
        authors.append(str(mod["linkUrl"]))
    return ", ".join(authors) or DEFAULT_AUTHOR


def pretty_category(cat: str) -> str:
    return cat.replace("-", " ").replace("_", " ").title()


def build_entry(category: str, mod: dict) -> dict:
    styles = [s for s in (mod.get("styles") or []) if isinstance(s, dict)]
    first = styles[0] if styles else {}
    file = mod.get("file") or first.get("file")
    preview = mod.get("preview") or first.get("preview")

    downloads = []
    if len(styles) > 1:
        for s in styles[:3]:
            if s.get("file"):
                downloads.append((f"Скачать: {s.get('label', 'стиль')}"[:80], file_url(category, s["file"])))
    elif file:
        label = "Открыть" if category in ("sites", "guides") else "Скачать"
        downloads.append((label, file_url(category, file)))
    if mod.get("multiPlatform"):
        for label, key in (("Windows", "fileWin"), ("Linux", "fileLinux")):
            if mod.get(key):
                downloads.append((f"Скачать: {label}", file_url(category, mod[key])))

    site_link = f"{SITE}?category={quote(category)}&mod={quote(mod['name'])}"
    tags = mod.get("tags") or {}
    date = (mod.get("meta") or {}).get("date")

    desc = mod.get("description") or mod.get("desc")
    return {
        "key": f"{category}/{mod['name']}",
        "title": str(mod["name"]),
        "category": category,
        "author": get_authors(mod),
        "desc": str(desc) if desc else None,
        "cover": preview_url(category, preview),
        "downloads": downloads,
        "site_link": site_link,
        "adult": bool(tags.get("adult")),
        "date": int(date) if date else 0,
    }


def discord(method: str, path: str, **kwargs):
    for _ in range(5):
        r = requests.request(method, f"{API}{path}", headers=HEADERS, timeout=30, **kwargs)
        if r.status_code == 429:
            time.sleep(float(r.json().get("retry_after", 2)) + 0.5)
            continue
        if not r.ok:
            raise RuntimeError(f"Discord {r.status_code}: {r.text[:300]}")
        return r.json()
    raise RuntimeError("Слишком много 429 от Discord")


def get_tag_ids() -> list:
    if not TAG_NAME or DRY_RUN:
        return []
    ch = discord("GET", f"/channels/{FORUM_ID}")
    return [t["id"] for t in ch.get("available_tags", []) if t["name"].strip().lower() == TAG_NAME]


def build_payload(e: dict, tag_ids: list) -> dict:
    prefix = "[18+] " if e["adult"] else ""
    embed = {
        "title": (prefix + e["title"])[:256],
        "url": e["site_link"],
        "color": 0x7B00FF,
        "author": {"name": e["author"][:256]},
        "fields": [{"name": "Категория", "value": pretty_category(e["category"])[:200], "inline": True}],
        "footer": {"text": "Dota2PornFx"},
    }
    if e["desc"]:
        embed["description"] = e["desc"][:1500]
    if e["date"]:
        embed["timestamp"] = datetime.fromtimestamp(e["date"], tz=timezone.utc).isoformat()

    if e["cover"] and not e["adult"]:
        embed["image"] = {"url": e["cover"]}

    buttons = [{"type": 2, "style": 5, "label": label, "url": url} for label, url in e["downloads"][:4]]
    buttons.append({"type": 2, "style": 5, "label": "Открыть на сайте", "url": e["site_link"]})

    body = {
        "name": (prefix + e["title"])[:100],
        "message": {
            "embeds": [embed],
            "components": [{"type": 1, "components": buttons[:5]}],
        },
    }
    if tag_ids:
        body["applied_tags"] = tag_ids
    return body


def main():
    data = load_data()
    mods_data = data.get("modsData", {})
    recent = data.get("recentlyAddedMods", [])

    entries = []
    for item in recent:
        category, name = item.get("category"), item.get("name")
        if not category or not name or category in SKIP_CATEGORIES:
            continue
        mod = find_mod(mods_data, category, name)
        if mod is None or mod.get("type") == "guide":
            continue
        entries.append(build_entry(category, mod))

    if DRY_RUN:
        seen = set()
    elif not STATE_FILE.exists():

        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps([e["key"] for e in entries], ensure_ascii=False, indent=1))
        print(f"Первый запуск: запомнил {len(entries)} модов, ничего не отправлял.")
        return
    else:
        seen = set(json.loads(STATE_FILE.read_text()))

    new = [e for e in entries if e["key"] not in seen]
    new.reverse()
    new.sort(key=lambda e: e["date"])
    print(f"Новых модов: {len(new)}")

    tag_ids = get_tag_ids() if new else []
    for e in new[:MAX_PER_RUN]:
        if e["adult"] and SKIP_ADULT:
            seen.add(e["key"])
            print("Пропущен (18+):", e["title"])
            continue
        payload = build_payload(e, tag_ids)
        if DRY_RUN:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
            continue
        try:
            discord("POST", f"/channels/{FORUM_ID}/threads", json=payload)
            seen.add(e["key"])
            print("Отправлен:", e["title"])
            time.sleep(2)
        except Exception as ex:
            print("Ошибка при отправке", e["title"], ex, file=sys.stderr)

    if not DRY_RUN:
        STATE_FILE.write_text(json.dumps(sorted(seen), ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
