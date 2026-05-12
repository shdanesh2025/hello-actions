import asyncio
import zipfile
import os
import re
import shutil
import argparse
import httpx
from email import policy
from email.parser import BytesParser
from pyppeteer import launch
from urllib.parse import urlencode, urljoin, urlparse
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# Helper functions
# ----------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    """Remove invalid characters for filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def parse_pages(pages_spec: str):
    """Parse page range/list (e.g., '0-4', '0,2,5', '3')."""
    pages = set()
    parts = pages_spec.split(',')
    for part in parts:
        if '-' in part:
            start, end = map(int, part.split('-'))
            pages.update(range(start, end + 1))
        else:
            pages.add(int(part))
    return sorted(pages)

def get_durf_value(durf_option: str) -> str:
    mapping = {
        'short': '1-3min',
        'medium': '3-10min',
        'long': '10min_more',
        'extralong': '20min_more'
    }
    return mapping.get(durf_option, '')

async def save_mhtml(url: str, output_file: str):
    """Save webpage as MHTML using pyppeteer."""
    browser = await launch(headless=True, args=['--no-sandbox'])
    page = await browser.newPage()
    await page.goto(url, waitUntil='networkidle0')
    mhtml_data = await page._client.send('Page.captureSnapshot', {})
    with open(output_file, 'wb') as f:
        f.write(mhtml_data['data'].encode())
    await browser.close()

def extract_html_from_mhtml(mhtml_path: str) -> str:
    """
    Extract the root HTML document from an MHTML file.
    Returns the HTML as a string.
    """
    with open(mhtml_path, 'rb') as f:
        msg = BytesParser(policy=policy.default).parse(f)

    # The main HTML part usually has Content-Type text/html
    for part in msg.walk():
        if part.get_content_type() == 'text/html':
            payload = part.get_payload(decode=True)
            # Decode using the charset if present, else utf-8
            charset = part.get_content_charset() or 'utf-8'
            return payload.decode(charset, errors='replace')
    raise ValueError(f"No HTML part found in {mhtml_path}")

def parse_search_page(html_content: str, base_url: str):
    """
    Parse HTML of a xvideos search results page.
    Returns a list of tuples (video_url, thumbnail_url).
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    results = []
    for thumb_inside in soup.find_all('div', class_='thumb-inside'):
        thumb_div = thumb_inside.find('div', class_='thumb')
        if not thumb_div:
            continue
        a_tag = thumb_div.find('a')
        if not a_tag or not a_tag.get('href'):
            continue
        img_tag = a_tag.find('img')
        if not img_tag or not img_tag.get('src'):
            continue

        video_url = urljoin(base_url, a_tag['href'])
        thumb_url = urljoin(base_url, img_tag['src'])
        results.append((video_url, thumb_url))
    return results

def generate_grid_html(video_data: list, images_folder: str = "images") -> str:
    """
    Generate a standalone HTML file with a 4‑column image grid.
    Clicking an image copies the associated video URL to clipboard.
    video_data: list of (video_url, local_image_path_relative)
    """
    # Create HTML content
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>xvideos Search Results</title>
    <style>
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: Arial, sans-serif;
            background: #f5f5f5;
            padding: 20px;
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        h1 {{
            text-align: center;
            margin-bottom: 30px;
            color: #333;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
        }}
        .card {{
            background: white;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            cursor: pointer;
            transition: transform 0.2s;
        }}
        .card:hover {{
            transform: scale(1.02);
        }}
        .card img {{
            width: 100%;
            aspect-ratio: 16 / 9;
            object-fit: cover;
            display: block;
        }}
        .toast {{
            position: fixed;
            bottom: 20px;
            left: 50%;
            transform: translateX(-50%);
            background: #333;
            color: white;
            padding: 12px 24px;
            border-radius: 8px;
            font-size: 14px;
            opacity: 0;
            transition: opacity 0.3s;
            pointer-events: none;
            z-index: 1000;
        }}
        @media (max-width: 1024px) {{
            .grid {{
                grid-template-columns: repeat(3, 1fr);
            }}
        }}
        @media (max-width: 768px) {{
            .grid {{
                grid-template-columns: repeat(2, 1fr);
            }}
        }}
        @media (max-width: 480px) {{
            .grid {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
<div class="container">
    <h1>xvideos Search Results</h1>
    <div class="grid" id="grid">
        {items}
    </div>
</div>
<div class="toast" id="toast">Copied to clipboard!</div>

<script>
    const cards = document.querySelectorAll('.card');
    const toast = document.getElementById('toast');

    function showToast() {{
        toast.style.opacity = '1';
        setTimeout(() => {{
            toast.style.opacity = '0';
        }}, 1500);
    }}

    async function copyToClipboard(text) {{
        try {{
            await navigator.clipboard.writeText(text);
            showToast();
        }} catch (err) {{
            console.error('Failed to copy: ', err);
            alert('Press Ctrl+C to copy: ' + text);
        }}
    }}

    cards.forEach(card => {{
        card.addEventListener('click', () => {{
            const url = card.getAttribute('data-url');
            copyToClipboard(url);
        }});
    }});
</script>
</body>
</html>"""

    items_html = []
    for idx, (video_url, img_path) in enumerate(video_data):
        items_html.append(
            f'<div class="card" data-url="{video_url}">'
            f'<img src="{img_path}" alt="Thumbnail {idx+1}" loading="lazy">'
            f'</div>'
        )

    return html_template.format(items='\n'.join(items_html))

# ----------------------------------------------------------------------
# Main async function
# ----------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="Download xvideos.com search results and create a clickable image grid.")
    parser.add_argument("--query", required=True, help="Search query (e.g., 'sophie dee anal')")
    parser.add_argument("--durf", choices=['short', 'medium', 'long', 'extralong'], default=None, help="Duration filter")
    parser.add_argument("--pages", required=True, help="Page numbers: e.g., '0', '0-4', '0,2,5'")
    args = parser.parse_args()

    # Build search URLs
    base_url = "https://www.xvideos.com/"
    query_plus = args.query.replace(' ', '+')
    durf_value = get_durf_value(args.durf) if args.durf else ''
    params = {
        'k': query_plus,
        'sort': 'relevance',
        'datef': '',
        'durf': durf_value,
        'quality': ''
    }
    qs = urlencode(params)
    base_search_url = f"{base_url}?{qs}"
    pages_to_download = parse_pages(args.pages)

    # Temporary directories
    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)
    temp_mhtml_dir = "temp_mhtml"
    os.makedirs(temp_mhtml_dir, exist_ok=True)

    # Step 1: Download all pages as MHTML
    saved_mhtmls = []
    for page_num in pages_to_download:
        page_url = f"{base_search_url}&p={page_num}"
        query_safe = sanitize_filename(args.query.replace(' ', '_'))
        durf_safe = args.durf if args.durf else "allduration"
        mhtml_filename = f"{query_safe}_{durf_safe}_p{page_num}.mhtml"
        mhtml_path = os.path.join(temp_mhtml_dir, mhtml_filename)

        print(f"Downloading page {page_num}: {page_url}")
        try:
            await save_mhtml(page_url, mhtml_path)
            saved_mhtmls.append(mhtml_path)
        except Exception as e:
            print(f"Error downloading page {page_num}: {e}")
            continue

    if not saved_mhtmls:
        print("No pages downloaded successfully. Exiting.")
        shutil.rmtree(temp_mhtml_dir, ignore_errors=True)
        return

    # Step 2: Extract video URLs and thumbnail URLs from each MHTML
    all_video_data = []          # (video_url, thumb_original_url)
    for mhtml_path in saved_mhtmls:
        html_content = extract_html_from_mhtml(mhtml_path)
        page_results = parse_search_page(html_content, base_url)
        all_video_data.extend(page_results)
        print(f"Extracted {len(page_results)} items from {os.path.basename(mhtml_path)}")

    if not all_video_data:
        print("No video items found. Exiting.")
        shutil.rmtree(temp_mhtml_dir, ignore_errors=True)
        return

    print(f"Total unique video entries: {len(all_video_data)}")

    # Step 3: Download unique thumbnail images
    images_dir = os.path.join(download_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    # Deduplicate thumbnails (same original URL)
    unique_thumbs = {}
    for video_url, thumb_url in all_video_data:
        if thumb_url not in unique_thumbs:
            unique_thumbs[thumb_url] = video_url  # keep one video per thumb (first)

    # But we need to keep all video entries, even if thumb URLs duplicate?
    # For the grid, each video should be a separate card; if multiple videos share the same thumbnail,
    # we still need separate cards. So we must keep all (video_url, thumb_url) pairs.
    # We'll download each distinct thumb_url only once, but map each video entry to the local file.
    thumb_url_to_local = {}
    async with httpx.AsyncClient(timeout=30.0) as client:
        for thumb_url in set(t[1] for t in all_video_data):
            # Create a safe filename from the URL
            parsed = urlparse(thumb_url)
            filename = os.path.basename(parsed.path)
            if not filename or '.' not in filename:
                filename = f"{hash(thumb_url)}.jpg"
            local_path = os.path.join(images_dir, filename)
            if not os.path.exists(local_path):
                try:
                    resp = await client.get(thumb_url)
                    resp.raise_for_status()
                    with open(local_path, 'wb') as f:
                        f.write(resp.content)
                    print(f"Downloaded thumbnail: {filename}")
                except Exception as e:
                    print(f"Failed to download {thumb_url}: {e}")
                    # Use a placeholder? We'll skip this item later.
                    local_path = None
            thumb_url_to_local[thumb_url] = local_path if local_path and os.path.exists(local_path) else None

    # Build final list with local image paths (skip those with missing image)
    final_items = []
    for video_url, thumb_url in all_video_data:
        local_img = thumb_url_to_local.get(thumb_url)
        if local_img:
            # Store relative path (images/filename)
            rel_path = os.path.join("images", os.path.basename(local_img))
            final_items.append((video_url, rel_path))

    if not final_items:
        print("No valid thumbnails downloaded. Exiting.")
        shutil.rmtree(temp_mhtml_dir, ignore_errors=True)
        return

    # Step 4: Generate the combined HTML grid
    html_content = generate_grid_html(final_items, "images")
    index_html_path = os.path.join(download_dir, "index.html")
    with open(index_html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    # Step 5: Create final ZIP containing index.html and images/ folder
    query_safe = sanitize_filename(args.query.replace(' ', '_'))
    durf_safe = args.durf if args.durf else "allduration"
    pages_range_str = f"p{min(pages_to_download)}-{max(pages_to_download)}" if len(pages_to_download) > 1 else f"p{pages_to_download[0]}"
    final_zip_name = f"{query_safe}_{durf_safe}_{pages_range_str}.zip"
    final_zip_path = os.path.join(download_dir, final_zip_name)

    with zipfile.ZipFile(final_zip_path, 'w') as zf:
        # Add index.html
        zf.write(index_html_path, arcname="index.html")
        # Add all images
        for root, _, files in os.walk(images_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, download_dir)
                zf.write(full_path, arcname=arcname)

    # Step 6: Cleanup – remove the original MHTML temp folder and the intermediate HTML/images?
    # We keep only the final ZIP. The GitHub action will commit the download folder.
    # Delete index.html and images folder from download dir (they are already inside the zip)
    os.remove(index_html_path)
    shutil.rmtree(images_dir)
    shutil.rmtree(temp_mhtml_dir)

    print(f"✅ Successfully created {final_zip_path} containing index.html and {len(final_items)} images.")

if __name__ == "__main__":
    asyncio.run(main())
