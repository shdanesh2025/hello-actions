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
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def parse_pages(pages_spec: str):
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
    browser = await launch(headless=True, args=['--no-sandbox'])
    page = await browser.newPage()
    await page.goto(url, waitUntil='networkidle0')
    mhtml_data = await page._client.send('Page.captureSnapshot', {})
    with open(output_file, 'wb') as f:
        f.write(mhtml_data['data'].encode())
    await browser.close()

def extract_html_from_mhtml(mhtml_path: str) -> str:
    with open(mhtml_path, 'rb') as f:
        msg = BytesParser(policy=policy.default).parse(f)
    for part in msg.walk():
        if part.get_content_type() == 'text/html':
            payload = part.get_payload(decode=True)
            charset = part.get_content_charset() or 'utf-8'
            return payload.decode(charset, errors='replace')
    raise ValueError(f"No HTML part found in {mhtml_path}")

def parse_search_page(html_content: str, base_url: str):
    soup = BeautifulSoup(html_content, 'html.parser')
    results = []

    # Work on each video block – prevents any cross‑block leaking
    for block in soup.find_all('div', class_='thumb-block'):
        inside = block.find('div', class_='thumb-inside')
        under  = block.find('div', class_='thumb-under')
        if not inside or not under:
            continue

        # --- thumbnail & video URL ---
        thumb_div = inside.find('div', class_='thumb')
        if not thumb_div:
            continue
        a_tag = thumb_div.find('a')
        if not a_tag or not a_tag.get('href'):
            continue
        img_tag = a_tag.find('img')
        if not img_tag:
            continue

        # Prefer the real thumbnail from data-sfwthumb, fallback to src
        thumb_src = img_tag.get('data-sfwthumb') or img_tag.get('src')
        if not thumb_src:
            continue

        video_url = urljoin(base_url, a_tag['href'])
        thumb_url = urljoin(base_url, thumb_src)

        # --- title ---
        title = ''
        title_tag = under.find('p', class_='title')
        if title_tag:
            a_title = title_tag.find('a')
            if a_title:
                title = a_title.get('title', '')
                if not title:
                    # Remove duration span before extracting text
                    a_clone = BeautifulSoup(str(a_title), 'html.parser')
                    dur_span = a_clone.find('span', class_='duration')
                    if dur_span:
                        dur_span.decompose()
                    title = a_clone.get_text(strip=True)

        # --- duration ---
        duration = ''
        if title_tag:
            dur_span = title_tag.find('span', class_='duration')
            if dur_span:
                duration = dur_span.get_text(strip=True)
        if not duration:
            metadata = under.find('p', class_='metadata')
            if metadata:
                dur_span_meta = metadata.find('span', class_='duration')
                if dur_span_meta:
                    duration = dur_span_meta.get_text(strip=True)

        # --- views ---
        views = ''
        metadata = under.find('p', class_='metadata')
        if metadata:
            meta_text = metadata.get_text(strip=True)
            match = re.search(r'([\d,.]+[kKmM]?)\s*Views', meta_text)
            if match:
                views = match.group(1) + ' Views'
            elif 'Views' in meta_text:
                # Fallback cleanup
                parts = meta_text.split('Views')
                if parts[0].strip():
                    views = parts[0].strip() + ' Views'

        results.append({
            'video_url': video_url,
            'thumb_url': thumb_url,
            'title': title,
            'duration': duration,
            'views': views,
        })

    return results
def generate_grid_html(video_items: list) -> str:
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>xvideos Search Results</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        h1 {{ text-align: center; margin-bottom: 30px; color: #333; }}
        .grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 20px; }}
        .card {{ background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; }}
        .card:hover {{ transform: scale(1.02); box-shadow: 0 4px 12px rgba(0,0,0,0.15); }}
        .card img {{ width: 100%; aspect-ratio: 16 / 9; object-fit: cover; display: block; }}
        .caption {{ padding: 12px; font-size: 14px; }}
        .title {{ font-weight: bold; margin-bottom: 8px; color: #222; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .details {{ display: flex; justify-content: space-between; color: #666; font-size: 12px; }}
        .duration {{ background: #e0e0e0; padding: 2px 6px; border-radius: 4px; }}
        .toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 12px 24px; border-radius: 8px; font-size: 14px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 1000; }}
        @media (max-width: 1024px) {{ .grid {{ grid-template-columns: repeat(3, 1fr); }} }}
        @media (max-width: 768px) {{ .grid {{ grid-template-columns: repeat(2, 1fr); }} }}
        @media (max-width: 480px) {{ .grid {{ grid-template-columns: 1fr; }} }}
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
    function showToast() {{ toast.style.opacity = '1'; setTimeout(() => {{ toast.style.opacity = '0'; }}, 1500); }}
    async function copyToClipboard(text) {{
        try {{ await navigator.clipboard.writeText(text); showToast(); }}
        catch (err) {{ console.error(err); alert('Press Ctrl+C to copy:\\n' + text); }}
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
    for item in video_items:
        items_html.append(f'''
            <div class="card" data-url="{item['video_url']}">
                <img src="{item['thumb_local_path']}" alt="Thumbnail" loading="lazy">
                <div class="caption">
                    <div class="title" title="{item['title']}">{item['title']}</div>
                    <div class="details">
                        <span class="duration">{item['duration']}</span>
                        <span class="views">{item['views']}</span>
                    </div>
                </div>
            </div>
        ''')
    return html_template.format(items='\n'.join(items_html))
# ----------------------------------------------------------------------
# Main async function
# ----------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", required=True)
    parser.add_argument("--durf", choices=['short', 'medium', 'long', 'extralong'], default=None)
    parser.add_argument("--pages", required=True)
    args = parser.parse_args()

    base_url = "https://www.xvideos.com/"
    query_plus = args.query.replace(' ', '+')
    durf_value = get_durf_value(args.durf) if args.durf else ''
    params = {'k': query_plus, 'sort': 'relevance', 'datef': '', 'durf': durf_value, 'quality': ''}
    qs = urlencode(params)
    base_search_url = f"{base_url}?{qs}"
    pages_to_download = parse_pages(args.pages)

    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)
    temp_mhtml_dir = "temp_mhtml"
    os.makedirs(temp_mhtml_dir, exist_ok=True)

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
            print(f"Error page {page_num}: {e}")

    if not saved_mhtmls:
        print("No pages downloaded.")
        shutil.rmtree(temp_mhtml_dir, ignore_errors=True)
        return

    all_video_items = []
    for mhtml_path in saved_mhtmls:
        html_content = extract_html_from_mhtml(mhtml_path)
        page_items = parse_search_page(html_content, base_url)
        all_video_items.extend(page_items)
        print(f"Extracted {len(page_items)} from {os.path.basename(mhtml_path)}")

    if not all_video_items:
        print("No video items found.")
        shutil.rmtree(temp_mhtml_dir, ignore_errors=True)
        return

    print(f"Total items: {len(all_video_items)}")

    # Download thumbnails
    images_dir = os.path.join(download_dir, "images")
    os.makedirs(images_dir, exist_ok=True)
    unique_thumbs = {item['thumb_url'] for item in all_video_items}
    thumb_url_to_local = {}

    async with httpx.AsyncClient(timeout=30.0) as client:
        for thumb_url in unique_thumbs:
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
                    print(f"Downloaded: {filename}")
                except Exception as e:
                    print(f"Failed {thumb_url}: {e}")
                    local_path = None
            thumb_url_to_local[thumb_url] = local_path if local_path and os.path.exists(local_path) else None

    final_items = []
    for item in all_video_items:
        local_img = thumb_url_to_local.get(item['thumb_url'])
        if local_img:
            item['thumb_local_path'] = os.path.join("images", os.path.basename(local_img))
            final_items.append(item)

    if not final_items:
        print("No valid thumbnails.")
        shutil.rmtree(temp_mhtml_dir, ignore_errors=True)
        return

    # Generate HTML
    html_content = generate_grid_html(final_items)
    index_html_path = os.path.join(download_dir, "index.html")
    with open(index_html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    # Create final ZIP
    query_safe = sanitize_filename(args.query.replace(' ', '_'))
    durf_safe = args.durf if args.durf else "allduration"
    pages_range = f"p{min(pages_to_download)}-{max(pages_to_download)}" if len(pages_to_download) > 1 else f"p{pages_to_download[0]}"
    final_zip_name = f"{query_safe}_{durf_safe}_{pages_range}.zip"
    final_zip_path = os.path.join(download_dir, final_zip_name)

    with zipfile.ZipFile(final_zip_path, 'w') as zf:
        zf.write(index_html_path, arcname="index.html")
        for root, _, files in os.walk(images_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, download_dir)
                zf.write(full_path, arcname=arcname)

    # Cleanup
    os.remove(index_html_path)
    shutil.rmtree(images_dir)
    shutil.rmtree(temp_mhtml_dir)

    print(f"✅ Created {final_zip_path} with {len(final_items)} videos.")

if __name__ == "__main__":
    asyncio.run(main())
