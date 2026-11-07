import os
import json
import io
from flask import Flask, request, send_file, jsonify, redirect, abort
from PIL import Image
import requests

app = Flask(__name__)

# Configuration
ITEM_DATA_FILE = 'main.json'
BACKGROUND_FOLDER = 'background'
DEFAULT_BACKGROUND_IMAGE = os.path.join(BACKGROUND_FOLDER, 'Default.png')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Global state
item_data = None
item_data_error = None

def load_item_data(force=False):
    """Loads item data from main.json with clear error reporting."""
    global item_data, item_data_error

    if item_data is not None and not force:
        return item_data

    file_path = os.path.join(BASE_DIR, ITEM_DATA_FILE)
    app.logger.info(f"[loader] Looking for {ITEM_DATA_FILE} at {file_path}")
    print(f"[loader] Looking for {ITEM_DATA_FILE} at {file_path}")

    if not os.path.exists(file_path):
        item_data_error = f"{ITEM_DATA_FILE} not found at {file_path}"
        app.logger.error(f"[loader] {item_data_error}")
        print(f"[loader] {item_data_error}")
        return None

    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:  # utf-8-sig strips BOM
            item_data = json.load(f)
    except json.JSONDecodeError as e:
        item_data_error = f"Invalid JSON in {ITEM_DATA_FILE}: {e}"
        app.logger.error(f"[loader] {item_data_error}")
        print(f"[loader] {item_data_error}")
        return None
    except Exception as e:
        item_data_error = f"Unexpected error reading {ITEM_DATA_FILE}: {e}"
        app.logger.error(f"[loader] {item_data_error}")
        print(f"[loader] {item_data_error}")
        return None

    # Guard against main.json containing `null`
    if item_data is None:
        item_data_error = f"{ITEM_DATA_FILE} parsed to null (root must be an array of items)"
        print(f"[loader] {item_data_error}")
        return None

    if not isinstance(item_data, list):
        item_data_error = f"{ITEM_DATA_FILE} must be a JSON array, got {type(item_data).__name__}"
        print(f"[loader] {item_data_error}")
        item_data = None
        return None

    item_data_error = None
    app.logger.info(f"[loader] Loaded {len(item_data)} items from {ITEM_DATA_FILE}")
    print(f"[loader] Loaded {len(item_data)} items from {ITEM_DATA_FILE}")
    return item_data


def find_item(item_id):
    """Find an item by ID, tolerating both 'Id' and 'itemID' key spellings."""
    if item_data is None:
        return None
    for item in item_data:
        if item.get("Id") == item_id or item.get("itemID") == item_id:
            return item
    return None


# Load on startup
with app.app_context():
    load_item_data()


# ---------- DIAGNOSTIC ENDPOINT ----------
@app.route('/health')
def health():
    """Hit this to see exactly why item data isn't loading."""
    return jsonify({
        "item_data_loaded": item_data is not None,
        "item_count": len(item_data) if isinstance(item_data, list) else 0,
        "error": item_data_error,
        "base_dir": BASE_DIR,
        "main_json_path": os.path.join(BASE_DIR, ITEM_DATA_FILE),
        "main_json_exists": os.path.exists(os.path.join(BASE_DIR, ITEM_DATA_FILE)),
        "background_dir_exists": os.path.isdir(os.path.join(BASE_DIR, BACKGROUND_FOLDER)),
    }), 200


@app.route('/reload')
def reload_data():
    """Force a reload (useful after fixing main.json)."""
    load_item_data(force=True)
    return jsonify({"ok": item_data is not None, "count": len(item_data) if item_data else 0,
                    "error": item_data_error})


# ---------- EXISTING ENDPOINTS ----------
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
        response = requests.head(image_url, timeout=5)
        if response.status_code != 200:
            abort(404, description="Item image not found")
        return redirect(image_url)
    except requests.RequestException:
        abort(502, description="Failed to connect to image server")


@app.route('/iteminfo', methods=['GET'])
def get_item_info():
    item_id = request.args.get('id', type=int)
    if item_id is None:
        return jsonify({"error": "Missing required parameter: id"}), 400

    if item_data is None:
        # Retry once — main.json may have been fixed without a restart
        load_item_data(force=True)
    if item_data is None:
        return jsonify({
            "error": "Item data not loaded.",
            "detail": item_data_error,
        }), 500

    item = find_item(item_id)
    if not item:
        return jsonify({"error": f"Item with ID {item_id} not found."}), 404
    return jsonify(item), 200


@app.route('/main/ICON/<int:itemid>.png', methods=['GET'])
def get_combined_item_image(itemid):
    if item_data is None:
        load_item_data(force=True)
    if item_data is None:
        return jsonify({
            "error": "Item data not loaded.",
            "detail": item_data_error,
        }), 500

    item_found = find_item(itemid)
    if not item_found:
        app.logger.warning(f"Item {itemid} not in main.json")
        return jsonify({"error": f"Item with ID {itemid} not found."}), 404

    rare_type = item_found.get("Rare", "Default") or "Default"
    background_image_path = os.path.join(BASE_DIR, BACKGROUND_FOLDER, f'{rare_type}.png')

    if not os.path.exists(background_image_path):
        app.logger.warning(f"Background for Rare='{rare_type}' missing; using default.")
        background_image_path = os.path.join(BASE_DIR, DEFAULT_BACKGROUND_IMAGE)
        if not os.path.exists(background_image_path):
            return jsonify({"error": "Background image not found and default is missing."}), 500

    try:
        background = Image.open(background_image_path).convert("RGBA")
    except Exception as e:
        app.logger.error(f"Background load error: {e}")
        return jsonify({"error": "Could not load background image."}), 500

    # Find item image across repos
    item_image_url = None
    for repo_num in range(1, 7):
        if repo_num == 1:
            batch_range = range(1, 7)
        else:
            start_batch = (repo_num - 1) * 6 + 1
            batch_range = range(start_batch, start_batch + 6)
        for batch_num in batch_range:
            batch_str = f"{batch_num:02d}"
            url = (f"https://raw.githubusercontent.com/djdndbdjfi/free-fire-items-{repo_num}"
                   f"/main/items/batch-{batch_str}/{itemid}.png")
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

    # Resize to 80% with preserved aspect ratio
    bg_w, bg_h = background.size
    target_w, target_h = int(bg_w * 0.8), int(bg_h * 0.8)
    iw, ih = item_image.size
    item_ratio = iw / ih
    target_ratio = target_w / target_h
    if item_ratio > target_ratio:
        new_w, new_h = target_w, int(target_w / item_ratio)
    else:
        new_h, new_w = target_h, int(target_h * item_ratio)
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
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    print(f"[🚀] Starting JWT-API on port {port} ...")
    app.run(host='0.0.0.0', port=port, debug=False)