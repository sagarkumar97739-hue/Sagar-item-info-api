import io
import json
import logging
import os
import re
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, redirect, request, send_file
from PIL import Image, UnidentifiedImageError

app = Flask(__name__)
BASE_DIR = Path(__file__).resolve().parent
ITEM_DATA_FILE = BASE_DIR / "main.json"
BACKGROUND_FOLDER = BASE_DIR / "background"
DEFAULT_BACKGROUND_IMAGE = BACKGROUND_FOLDER / "Default.png"
REQUEST_TIMEOUT = (5, 15)

_item_data = None


def load_item_data():
    """Load item data once per serverless instance."""
    global _item_data
    if _item_data is not None:
        return _item_data

    try:
        with ITEM_DATA_FILE.open("r", encoding="utf-8") as file:
            data = json.load(file)
        if not isinstance(data, list):
            raise ValueError("main.json must contain a JSON array")
        _item_data = data
        app.logger.info("Loaded %s items from main.json", len(data))
        return _item_data
    except FileNotFoundError:
        app.logger.exception("main.json is missing: %s", ITEM_DATA_FILE)
    except (json.JSONDecodeError, ValueError):
        app.logger.exception("main.json is invalid")
    except OSError:
        app.logger.exception("Could not read main.json")
    return None


def find_item_image(item_id):
    """Find an item image in the configured GitHub repositories."""
    session = requests.Session()
    for repo_num in range(1, 7):
        first_batch = 1 if repo_num == 1 else (repo_num - 1) * 6 + 1
        last_batch = 6 if repo_num == 1 else first_batch + 5
        for batch_num in range(first_batch, last_batch + 1):
            url = (
                f"https://raw.githubusercontent.com/djdndbdjfi/"
                f"free-fire-items-{repo_num}/main/items/"
                f"batch-{batch_num:02d}/{item_id}.png"
            )
            try:
                response = session.head(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                if response.status_code == 200:
                    return url
            except requests.RequestException:
                app.logger.warning("Image lookup failed for %s", url)
    return None


@app.get("/item-image")
def get_item_image():
    """Redirect to the original item PNG hosted on jsDelivr."""
    item_id = request.args.get("id", "").strip()
    key = request.args.get("key", "")

    if key != os.environ.get("ITEM_IMAGE_API_KEY", "SAGAR"):
        abort(403, description="Invalid key")
    if not item_id:
        abort(400, description="Item ID is required")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", item_id):
        abort(400, description="Invalid item ID")

    image_url = f"https://cdn.jsdelivr.net/gh/ShahGCreator/icon@main/PNG/{item_id}.png"
    try:
        response = requests.head(image_url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if response.status_code != 200:
            abort(404, description="Item image not found")
        return redirect(image_url, code=302)
    except requests.RequestException:
        abort(502, description="Failed to connect to image server")


@app.get("/iteminfo")
def get_item_info():
    """Return item details from main.json using ?id=."""
    item_id = request.args.get("id", type=int)
    if item_id is None:
        return jsonify({"error": "Missing or invalid required parameter: id"}), 400

    data = load_item_data()
    if data is None:
        return jsonify({"error": "Item data not loaded. Check that main.json is deployed and valid."}), 500

    item = next((entry for entry in data if entry.get("Id") == item_id), None)
    if item is None:
        return jsonify({"error": f"Item with ID {item_id} not found."}), 404
    return jsonify(item), 200


@app.get("/main/ICON/<int:itemid>.png")
def get_combined_item_image(itemid):
    """Generate a composite PNG using the item's rarity background."""
    data = load_item_data()
    if data is None:
        return jsonify({"error": "Item data not loaded. Check that main.json is deployed and valid."}), 500

    item = next((entry for entry in data if entry.get("itemID") == itemid), None)
    if item is None:
        return jsonify({"error": f"Item with ID {itemid} not found."}), 404

    rare_type = str(item.get("Rare", "Default"))
    # Only allow a simple filename to prevent paths escaping the background folder.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", rare_type):
        rare_type = "Default"

    background_path = BACKGROUND_FOLDER / f"{rare_type}.png"
    if not background_path.is_file():
        background_path = DEFAULT_BACKGROUND_IMAGE
    if not background_path.is_file():
        return jsonify({"error": "Background image not found and default is missing."}), 500

    try:
        with Image.open(background_path) as source:
            background = source.convert("RGBA")
    except (OSError, UnidentifiedImageError):
        app.logger.exception("Could not load background image: %s", background_path)
        return jsonify({"error": "Could not load background image."}), 500

    image_url = find_item_image(itemid)
    if not image_url:
        return jsonify({"error": f"Item image for ID {itemid} not found."}), 404

    try:
        response = requests.get(image_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        with Image.open(io.BytesIO(response.content)) as source:
            item_image = source.convert("RGBA")
    except requests.RequestException:
        app.logger.exception("Could not fetch item image for ID %s", itemid)
        return jsonify({"error": "Could not fetch item image from GitHub."}), 502
    except (OSError, UnidentifiedImageError):
        app.logger.exception("Invalid image returned for item ID %s", itemid)
        return jsonify({"error": "External API did not return a valid image."}), 502

    bg_width, bg_height = background.size
    target_width, target_height = int(bg_width * 0.8), int(bg_height * 0.8)
    item_width, item_height = item_image.size
    scale = min(target_width / item_width, target_height / item_height)
    new_size = (max(1, int(item_width * scale)), max(1, int(item_height * scale)))
    item_image = item_image.resize(new_size, Image.Resampling.LANCZOS)

    x = (bg_width - new_size[0]) // 2
    y = (bg_height - new_size[1]) // 2
    layer = Image.new("RGBA", background.size, (0, 0, 0, 0))
    layer.paste(item_image, (x, y), item_image)
    combined = Image.alpha_composite(background, layer)

    output = io.BytesIO()
    combined.save(output, format="PNG")
    output.seek(0)
    return send_file(output, mimetype="image/png", download_name=f"item_{itemid}.png")


@app.get("/")
def health_check():
    return jsonify({"status": "ok", "message": "Item API is running on Vercel."}), 200


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(502)
def handle_http_error(error):
    return jsonify({"error": error.description}), error.code


@app.errorhandler(500)
def handle_server_error(error):
    app.logger.exception("Internal server error")
    return jsonify({"error": "Internal server error"}), 500
