import sys
import os
import json
from datetime import datetime
from xvideos_api import Client        # ← CHANGE THIS if the module name differs

def sanitize_filename(text):
    safe = "".join(c for c in text if c.isalnum() or c in " _-")
    return safe.strip().replace(" ", "_")[:100]

def search_and_save(query, pages):
    client = Client()                   # Add authentication if needed (see note below)
    print(f"Searching for '{query}' with {pages} page(s)...")
    videos = client.search(query, pages=pages)
    print(f"Found {len(videos)} results.")

    # Convert video objects to dictionaries
    try:
        results = [v.to_dict() if hasattr(v, 'to_dict') else v.__dict__ for v in videos]
    except Exception:
        results = [str(v) for v in videos]

    output_dir = "search_results"
    os.makedirs(output_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_query = sanitize_filename(query)
    filename = f"{safe_query}_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"Saved to {filepath}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python search_and_save.py <query> [pages]")
        sys.exit(1)
    query = sys.argv[1]
    pages = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    search_and_save(query, pages)
