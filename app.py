import os
import json
import io
from flask import Flask, request, send_file, jsonify, redirect, abort
from PIL import Image
import requests

app = Flask(__name__)

ITEM_DATA_FILE = 'main.json'
BACKGROUND_FOLDER = 'background'
DEFAULT_BACKGROUND_IMAGE = os.path.join(BACKGROUND_FOLDER, 'Default.png')
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

item_data = None
item_data_error = None


def load_item_data(force=False):
    """Load main.json once. utf-8-sig strips any BOM that editors add."""
    global item_data, item_data_error
    if item_data is not None and not force:
        return item_data

    file_path = os.path.join(BASE_DIR, ITEM_DATA_FILE)
    print(f"[loader] Looking for {ITEM_DATA_FILE} at {file_path}")

    if not os.path.exists(file_path):
        item_data_error = f"{ITEM_DATA_FILE} not found at {file_path}"
        print(f"[loader] {item_data_error}")
        return None

    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            item_data = json.load(f)
    except json.JSONDecodeError as e:
        item_data_error = f"Invalid JSON: {e}"
        print(f"[loader] {item_data_error}")
        return None
    except Exception as e:
        item_data_error = f"Read error: {e}"
        print(f"[loader] {item_data_error}")
        return None

    if not isinstance(item_data, list):
        item_data_error = f"main.json must be a JSON array, got {type(item_data).__name__}"
        print(f"[loader] {item_data_error}")
        item_data = None
        return None

    item_data_error = None
    print(f"[loader] Loaded {len(item_data)} items from {ITEM_DATA_FILE}")
    return item_data


def find_item(item_id):
    """Tolerant lookup: matches 'Id' OR 'itemID' so both old keys work."""
    if item_data is None:
        return None
    for item in item_data:
        if item.get("Id") == item_id or item.get("itemID") == item_id:
            return item
    return None


# Load once at import time
load_item_data()


# ── Health check ──────────────────────────────────────────────────────────
@app.route('/health')
def health():
    return jsonify({
        "item_data_loaded": item_data is not None,
        "item_count": len(item_data) if isinstance(item_data, list) else 0,
        "error": item_data_error,
        "main_json_path": os.path.join(BASE_DIR, ITEM_DATA_FILE),
        "main_json_exists": os.path.exists(os.path.join(BASE_DIR, ITEM_DATA_FILE)),
        "background_dir_exists": os.path.isdir(os.path.join(BASE_DIR, BACKGROUND_FOLDER)),
    })


# ── Direct CDN image ──────────────────────────────────────────────────────
@app.route('/item-image')
def get_item_image():
    item_id = request.args.get('id')
    key = request.args.get('key')

    if key != 'SAGAR':
        abort(403, description="Invalid key")
    if not item_id:
        abort(400, description="Item ID is required")

    image_url = f"https://cdn.jsdelivr.net/gh/ShahGCreator/icon@main/PNG/{item_id}.png"
    try:
        if requests.head(image_url, timeout=5).status_code != 200:
            abort(404, description="Item image not found")
        return redirect(image_url)
    except requests.RequestException:
        abort(502, description="Failed to connect to image server")


# ── Item info from main.json ──────────────────────────────────────────────
@app.route('/iteminfo', methods=['GET'])
def get_item_info():
    item_id = request.args.get('id', type=int)
    if item_id is None:
        return jsonify({"error": "Missing required parameter: id"}), 400

    if item_data is None:
        load_item_data(force=True)
    if item_data is None:
        return jsonify({"error": "Item data not loaded.", "detail": item_data_error}), 500

    item = find_item(item_id)
    if not item:
        return jsonify({"error": f"Item with ID {item_id} not found."}), 404
    return jsonify(item), 200


# ── Composite image ───────────────────────────────────────────────────────
@app.route('/main/ICON/<int:itemid>.png', methods=['GET'])
def get_combined_item_image(itemid):
    if item_data is None:
        load_item_data(force=True)
    if item_data is None:
        return jsonify({"error": "Item data not loaded.", "detail": item_data_error}), 500

    item_found = find_item(itemid)
    if not item_found:
        return jsonify({"error": f"Item with ID {itemid} not found."}), 404

    rare_type = item_found.get("Rare", "Default") or "Default"
    bg_path = os.path.join(BASE_DIR, BACKGROUND_FOLDER, f'{rare_type}.png')

    if not os.path.exists(bg_path):
        bg_path = os.path.join(BASE_DIR, DEFAULT_BACKGROUND_IMAGE)
        if not os.path.exists(bg_path):
            return jsonify({"error": "Background image not found and default is missing."}), 500

    try:
        background = Image.open(bg_path).convert("RGBA")
    except Exception:
        return jsonify({"error": "Could not load background image."}), 500

    # Search repos 1-6 for the item icon
    item_image_url = None
    for repo_num in range(1, 7):
        batch_range = range(1, 7) if repo_num == 1 else range((repo_num - 1) * 6 + 1, (repo_num - 1) * 6 + 7)
        for batch_num in batch_range:
            url = (f"https://raw.githubusercontent.com/djdndbdjfi/free-fire-items-{repo_num}"
                   f"/main/items/batch-{batch_num:02d}/{itemid}.png")
            try:
                if requests.head(url, timeout=5).status_code == 200:
                    item_image_url = url
                    break
            except requests.RequestException:
                continue
        if item_image_url:
            break

    if not item_image_url:
        return jsonify({"error": f"Item image for ID {itemid} not found."}), 404

    try:
        r = requests.get(item_image_url, timeout=15)
        r.raise_for_status()
        item_image = Image.open(io.BytesIO(r.content)).convert("RGBA")
    except requests.RequestException as e:
        return jsonify({"error": f"Could not fetch item image: {e}"}), 502
    except Image.UnidentifiedImageError:
        return jsonify({"error": "External source did not return a valid image."}), 502

    # Resize to 80% of background, preserving aspect ratio
    bg_w, bg_h = background.size
    target_w, target_h = int(bg_w * 0.8), int(bg_h * 0.8)
    iw, ih = item_image.size
    ratio = iw / ih
    if ratio > target_w / target_h:
        new_w, new_h = target_w, int(target_w / ratio)
    else:
        new_h, new_w = target_h, int(target_h * ratio)
    item_image = item_image.resize((new_w, new_h), Image.LANCZOS)

    layer = Image.new("RGBA", background.size, (0, 0, 0, 0))
    layer.paste(item_image, ((bg_w - new_w) // 2, (bg_h - new_h) // 2), item_image)
    combined = Image.alpha_composite(background, layer)

    buf = io.BytesIO()
    combined.save(buf, format='PNG')
    buf.seek(0)
    return send_file(buf, mimetype='image/png', as_attachment=False,
                     download_name=f'item_{itemid}.png')


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)