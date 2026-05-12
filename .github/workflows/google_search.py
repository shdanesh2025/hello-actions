import sys
import json
import os
from datetime import datetime
from googlesearch import search

def sanitize_filename(text):
    safe = "".join(c for c in text if c.isalnum() or c in " _-")
    return safe.strip().replace(" ", "_")[:100]

def google_search(query, num_results):
    print(f"Searching Google for: {query}")
    results = []
    try:
        # googlesearch library already rotates user agents and adds delays
        for url in search(query, num_results=int(num_results), lang="en"):
            results.append({"url": url})  # you can extract title/snippet if needed
        print(f"Found {len(results)} results.")
    except Exception as e:
        print(f"Search failed: {e}")
        sys.exit(1)

    # Save to JSON
    output_dir = "google_results"
    os.makedirs(output_dir, exist_ok=True)
    safe_query = sanitize_filename(query)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_query}_{timestamp}.json"
    filepath = os.path.join(output_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"Results saved to {filepath}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python google_search.py <query> [num_results]")
        sys.exit(1)
    query = sys.argv[1]
    num_results = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    google_search(query, num_results)
