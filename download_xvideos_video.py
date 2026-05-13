import asyncio
import zipfile
import os
import re
import shutil
import argparse
import subprocess
import httpx
from email import policy
from email.parser import BytesParser
from pyppeteer import launch
from urllib.parse import urlparse, urljoin, urlencode
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# Helper functions (same as before)
# ----------------------------------------------------------------------
def sanitize_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)

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

def parse_video_page(html_content: str, base_url: str):
    """Extract main video info + suggested videos from a single video page."""
    soup = BeautifulSoup(html_content, 'html.parser')
    main_info = {
        'title': '',
        'duration': '',
        'views': '',
        'thumbnails': [],      # main video's own preview images
        'video_links': []      # (quality, url)
    }

    # Main video title
    title_tag = soup.find('meta', property='og:title')
    if title_tag and title_tag.get('content'):
        main_info['title'] = title_tag['content']
    else:
        h1 = soup.find('h1')
        if h1:
            main_info['title'] = h1.get_text(strip=True)
    if not main_info['title']:
        main_info['title'] = "Untitled"

    # Main duration
    duration_span = soup.find('span', class_='duration')
    if duration_span:
        main_info['duration'] = duration_span.get_text(strip=True)

    # Main views
    views_text = soup.find(string=re.compile(r'[\d.]+[kKmM]?\s*[Vv]iews'))
    if views_text:
        match = re.search(r'([\d.]+[kKmM]?)\s*[Vv]iews', views_text)
        if match:
            main_info['views'] = match.group(1) + ' Views'

    # Main thumbnails
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        main_info['thumbnails'].append(urljoin(base_url, og_image['content']))
    for img in soup.select('img'):
        src = img.get('src')
        if src and ('thumb' in src.lower() or 'preview' in src.lower() or 'poster' in src.lower()):
            abs_url = urljoin(base_url, src)
            if abs_url not in main_info['thumbnails']:
                main_info['thumbnails'].append(abs_url)

    for a in soup.find_all('a', href=True):
	    href = a['href']
	    if href.startswith('https://mp4') and '.mp4' in href:
	        # Try to extract quality from pattern like video_720p
	        match = re.search(r'video_(\d+)p', href)
	        if match:
	            quality = int(match.group(1))
	        else:
	            # Fallback: assign a default quality (e.g., 480 for "sd" or 0 for unknown)
	            # You can also try to parse 'mp4_sd' -> 480, 'mp4_hd' -> 720, etc.
	            quality = 480  # treat unknown as medium quality
	        main_info['video_links'].append((quality, href))

    # ------------------------------------------------------------------
    # Suggested videos – FIXED: iterate each thumb-block container
    # ------------------------------------------------------------------
    suggested = []
    for block in soup.find_all('div', class_='thumb-block'):
        inside = block.find('div', class_='thumb-inside')
        under  = block.find('div', class_='thumb-under')
        if not inside or not under:
            continue

        thumb_div = inside.find('div', class_='thumb')
        if not thumb_div:
            continue
        a_tag = thumb_div.find('a')
        if not a_tag or not a_tag.get('href'):
            continue
        img_tag = a_tag.find('img')
        if not img_tag:
            continue

        # Prefer real thumbnail from data-sfwthumb, fallback to src
        thumb_src = img_tag.get('data-sfwthumb') or img_tag.get('src')
        if not thumb_src:
            continue

        video_url = urljoin(base_url, a_tag['href'])
        thumb_url = urljoin(base_url, thumb_src)

        # Title
        title_tag = under.find('p', class_='title')
        title = ''
        if title_tag:
            a_title = title_tag.find('a')
            if a_title:
                title = a_title.get('title', '')
                if not title:
                    a_clone = BeautifulSoup(str(a_title), 'html.parser')
                    dur_span = a_clone.find('span', class_='duration')
                    if dur_span:
                        dur_span.decompose()
                    title = a_clone.get_text(strip=True)

        # Duration
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

        # Views
        views = ''
        metadata = under.find('p', class_='metadata')
        if metadata:
            meta_text = metadata.get_text(strip=True)
            match = re.search(r'([\d,.]+[kKmM]?)\s*Views', meta_text)
            if match:
                views = match.group(1) + ' Views'
            elif 'Views' in meta_text:
                parts = meta_text.split('Views')
                if parts[0].strip():
                    views = parts[0].strip() + ' Views'

        suggested.append({
            'video_url': video_url,
            'thumb_url': thumb_url,
            'title': title,
            'duration': duration,
            'views': views,
        })

    return main_info, suggested

def select_best_quality(video_links, desired_quality):
    if not video_links:
        return None
    video_links.sort(key=lambda x: x[0], reverse=True)
    for q, url in video_links:
        if q == desired_quality:
            return url
    return video_links[0][1]

async def download_file(url, output_path, description="file"):
    async with httpx.AsyncClient(timeout=120.0) as client:
        async with client.stream('GET', url) as response:
            response.raise_for_status()
            total = int(response.headers.get('content-length', 0))
            with open(output_path, 'wb') as f:
                downloaded = 0
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        percent = (downloaded / total) * 100
                        print(f"Downloading {description}: {percent:.1f}%", end='\r')
            print(f"\nDownloaded {description} to {output_path}")

# ----------------------------------------------------------------------
# HTML generator for a single video + suggested videos grid
# ----------------------------------------------------------------------
def generate_video_html(main_info, main_video_local_path, suggested_items, original_url) -> str:
    """
    suggested_items: list of dicts with keys:
        video_url, thumb_local_path, title, duration, views
    """
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .video-section {{ background: black; border-radius: 12px; margin-bottom: 20px; overflow: hidden; }}
        video {{ width: 100%; max-height: 70vh; }}
        .info-section {{ background: white; padding: 20px; border-radius: 12px; margin-bottom: 20px; }}
        h1 {{ margin-bottom: 10px; }}
        .metadata {{ margin-bottom: 20px; color: #666; display: flex; gap: 15px; flex-wrap: wrap; }}
        .controls {{ background: #f0f0f0; padding: 15px; border-radius: 8px; margin-bottom: 20px; display: flex; gap: 15px; align-items: center; flex-wrap: wrap; }}
        .controls label {{ font-weight: bold; }}
        .controls select, .controls button {{ padding: 8px 12px; border-radius: 4px; border: 1px solid #ccc; cursor: pointer; }}
        .controls button {{ background: #007bff; color: white; border: none; }}
        .controls button:hover {{ background: #0056b3; }}
        .grid {{ display: grid; gap: 20px; transition: 0.2s; }}
        .grid.cols-2 {{ grid-template-columns: repeat(2, 1fr); }}
        .grid.cols-3 {{ grid-template-columns: repeat(3, 1fr); }}
        .grid.cols-4 {{ grid-template-columns: repeat(4, 1fr); }}
        .card {{ background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.1); cursor: pointer; transition: transform 0.2s; border: 2px solid transparent; }}
        .card.selected {{ border-color: #007bff; background: #e7f3ff; }}
        .card:hover {{ transform: scale(1.02); }}
        .card img {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; }}
        .caption {{ padding: 12px; font-size: 14px; }}
        .title {{ font-weight: bold; margin-bottom: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .details {{ display: flex; justify-content: space-between; color: #666; font-size: 12px; }}
        .duration {{ background: #e0e0e0; padding: 2px 6px; border-radius: 4px; }}
        .toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 12px 24px; border-radius: 8px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 1000; }}
        @media (max-width: 1024px) {{ .grid.cols-4 {{ grid-template-columns: repeat(3, 1fr); }} }}
        @media (max-width: 768px) {{ .grid.cols-3, .grid.cols-4 {{ grid-template-columns: repeat(2, 1fr); }} }}
        @media (max-width: 480px) {{ .grid {{ grid-template-columns: 1fr !important; }} }}
    </style>
</head>
<body>
<div class="container">
    <div class="video-section">
        <video controls autoplay loop>
            <source src="{video_local}" type="video/mp4">
        </video>
    </div>
    <div class="info-section">
        <h1>{title}</h1>
        <div class="metadata">
            <span>⏱️ {duration}</span>
            <span>👁️ {views}</span>
        </div>
    </div>
    <div class="controls">
        <label>Suggested videos columns: </label>
        <select id="columnSelect">
            <option value="2">2 columns</option>
            <option value="3" selected>3 columns</option>
            <option value="4">4 columns</option>
        </select>
        <button id="copyBtn">📋 Copy Selected URLs (as array)</button>
        <span id="selectionCount" style="margin-left: auto;">Selected: 0</span>
    </div>
    <div class="grid cols-3" id="grid">
        {suggested_grid}
    </div>
</div>
<div class="toast" id="toast">Copied to clipboard!</div>
<script>
    const grid = document.getElementById('grid');
    const columnSelect = document.getElementById('columnSelect');
    const copyBtn = document.getElementById('copyBtn');
    const toast = document.getElementById('toast');
    const selectionCountSpan = document.getElementById('selectionCount');
    let selectedCards = new Set();

    function updateSelectionCount() {{
        selectionCountSpan.textContent = `Selected: ${{selectedCards.size}}`;
    }}
    function showToast(message) {{
        toast.textContent = message;
        toast.style.opacity = '1';
        setTimeout(() => {{ toast.style.opacity = '0'; }}, 1500);
    }}
    async function copySelectedUrls() {{
        if (selectedCards.size === 0) {{
            showToast('No videos selected');
            return;
        }}
        const urls = Array.from(selectedCards);
        const jsonStr = JSON.stringify(urls);
        try {{
            await navigator.clipboard.writeText(jsonStr);
            showToast(`Copied ${{selectedCards.size}} URLs as JSON array`);
        }} catch (err) {{
            alert('Manual copy:\\n' + jsonStr);
        }}
    }}
    copyBtn.addEventListener('click', copySelectedUrls);
    document.addEventListener('keydown', (e) => {{
        if ((e.ctrlKey || e.metaKey) && e.key === 'c' && selectedCards.size > 0) {{
            e.preventDefault();
            copySelectedUrls();
        }}
    }});
    columnSelect.addEventListener('change', (e) => {{
        grid.className = `grid cols-${{e.target.value}}`;
    }});
    const cards = document.querySelectorAll('.card');
    cards.forEach(card => {{
        card.addEventListener('click', (e) => {{
            e.stopPropagation();
            const url = card.getAttribute('data-url');
            if (selectedCards.has(url)) {{
                selectedCards.delete(url);
                card.classList.remove('selected');
            }} else {{
                selectedCards.add(url);
                card.classList.add('selected');
            }}
            updateSelectionCount();
        }});
    }});
</script>
</body>
</html>"""
    # Build suggested videos grid
    suggest_html = []
    for item in suggested_items:
        suggest_html.append(f'''
            <div class="card" data-url="{item['video_url']}">
                <img src="{item['thumb_local_path']}" alt="{item['title']}" loading="lazy">
                <div class="caption">
                    <div class="title" title="{item['title']}">{item['title']}</div>
                    <div class="details">
                        <span class="duration">{item['duration']}</span>
                        <span class="views">{item['views']}</span>
                    </div>
                </div>
            </div>
        ''')
    return html_template.format(
        title=main_info['title'],
        duration=main_info['duration'] or 'Unknown',
        views=main_info['views'] or 'Unknown',
        video_local=main_video_local_path,
        suggested_grid='\n'.join(suggest_html)
    )

# ----------------------------------------------------------------------
# Process a single video URL (download main video + suggested thumbnails)
# ----------------------------------------------------------------------
async def process_video(url, desired_quality, download_dir, images_dir, video_index):
    temp_mhtml = f"temp_video_{video_index}.mhtml"
    try:
        print(f"\n--- Processing {url} ---")
        await save_mhtml(url, temp_mhtml)
        html_content = extract_html_from_mhtml(temp_mhtml)
        main_info, suggested = parse_video_page(html_content, url)

        if not main_info['video_links']:
            print(f"No mp4 links for {url}")
            os.remove(temp_mhtml)
            return None

        best_url = select_best_quality(main_info['video_links'], desired_quality)
        best_quality = next((q for q, u in main_info['video_links'] if u == best_url), None)
        print(f"Selected quality: {best_quality}p (desired {desired_quality}p)")

        # Download main video
        title_safe = sanitize_filename(main_info['title'] or f"video_{video_index}")
        video_filename = f"{title_safe}_{best_quality}p.mp4"
        video_path = os.path.join(download_dir, video_filename)
        await download_file(best_url, video_path, "video")

        # Download thumbnails for suggested videos (deduplicate across all suggested)
        suggested_items = []
        unique_thumb_urls = {item['thumb_url'] for item in suggested}
        thumb_url_to_local = {}
        async with httpx.AsyncClient(timeout=30.0) as client:
            for thumb_url in unique_thumb_urls:
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
                        print(f"Failed {thumb_url}: {e}")
                        local_path = None
                thumb_url_to_local[thumb_url] = local_path if local_path and os.path.exists(local_path) else None

        # Build suggested_items with local paths
        for v in suggested:
            local_img = thumb_url_to_local.get(v['thumb_url'])
            if local_img:
                v['thumb_local_path'] = os.path.join("images", os.path.basename(local_img))
                suggested_items.append(v)

        # Generate HTML for this video
        html_filename = f"{title_safe}.html"
        html_path = os.path.join(download_dir, html_filename)
        html_content = generate_video_html(main_info, video_filename, suggested_items, url)
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        os.remove(temp_mhtml)
        return {
            'title': main_info['title'],
            'html': html_filename,
            'video': video_filename,
            'suggested_count': len(suggested_items)
        }
    except Exception as e:
        print(f"Error processing {url}: {e}")
        if os.path.exists(temp_mhtml):
            os.remove(temp_mhtml)
        return None

# ----------------------------------------------------------------------
# Main (supports single URL or multiple via --urls)
# ----------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="Download one or more xvideos.com videos, including suggested videos grid.")
    parser.add_argument("--url", help="Single video URL")
    parser.add_argument("--urls", help="Comma-separated list of video URLs")
    parser.add_argument("--quality", choices=['240', '360', '720', '1080'], required=True)
    args = parser.parse_args()

    urls = []
    if args.url:
        urls = [args.url]
    elif args.urls:
        urls = [u.strip() for u in args.urls.split(',') if u.strip()]
    else:
        print("Error: Provide either --url or --urls")
        return

    desired_quality = int(args.quality)

    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)
    images_dir = os.path.join(download_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    results = []
    for idx, url in enumerate(urls):
        result = await process_video(url, desired_quality, download_dir, images_dir, idx)
        if result:
            results.append(result)

    if not results:
        print("No videos downloaded successfully.")
        return

    # Create master index HTML if multiple videos
    if len(results) > 1:
        master_html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Downloaded Videos</title><style>
body { font-family: Arial; padding: 20px; }
ul { list-style: none; display: grid; grid-template-columns: repeat(auto-fill, minmax(200px,1fr)); gap: 20px; }
li { border: 1px solid #ccc; padding: 10px; border-radius: 8px; }
a { text-decoration: none; color: #007bff; }
</style></head>
<body><h1>Downloaded Videos</h1><ul>"""
        for r in results:
            master_html += f'<li><a href="{r["html"]}">{r["title"]}</a><br>({r["video"]})</li>'
        master_html += "</ul></body></html>"
        master_path = os.path.join(download_dir, "index.html")
        with open(master_path, 'w', encoding='utf-8') as f:
            f.write(master_html)
        print("Created master index.html")

    # Create final ZIP
    title_safe = sanitize_filename("_".join([r['title'][:20] for r in results[:3]])) if len(results) > 1 else sanitize_filename(results[0]['title'])
    final_zip_name = f"{title_safe}_{desired_quality}p_{len(results)}videos.zip"
    final_zip_path = os.path.join(download_dir, final_zip_name)

    with zipfile.ZipFile(final_zip_path, 'w') as zf:
        for root, _, files in os.walk(download_dir):
            for file in files:
                if file == final_zip_name:
                    continue
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, download_dir)
                zf.write(full_path, arcname=arcname)

    # Split if > 90 MB
    zip_size = os.path.getsize(final_zip_path)
    max_size = 90 * 1024 * 1024
    if zip_size > max_size:
        print(f"ZIP size is {zip_size / (1024*1024):.2f} MB, splitting into 90 MB parts...")
        split_prefix = final_zip_path + ".part."
        subprocess.run(['split', '-b', '90m', final_zip_path, split_prefix], check=True)
        os.remove(final_zip_path)
        part_files = sorted([f for f in os.listdir(download_dir) if f.startswith(os.path.basename(final_zip_path) + ".part.")])
        for i, part in enumerate(part_files, start=1):
            old_path = os.path.join(download_dir, part)
            new_name = f"{final_zip_name}.{i:03d}"
            new_path = os.path.join(download_dir, new_name)
            os.rename(old_path, new_path)
            print(f"Created part: {new_name}")
        print("Split completed.")
    else:
        print(f"ZIP size is {zip_size / (1024*1024):.2f} MB, no splitting needed.")

    # Cleanup: remove all files except the final artifacts (zip or split parts)
    artifacts = []
    if os.path.exists(final_zip_path):
        artifacts.append(final_zip_path)
    else:
        for f in os.listdir(download_dir):
            if f.startswith(title_safe) and (f.endswith('.zip.001') or ('.zip.' in f and f.split('.')[-1].isdigit())):
                artifacts.append(os.path.join(download_dir, f))
    temp_dir = "temp_artifacts"
    os.makedirs(temp_dir, exist_ok=True)
    for art in artifacts:
        shutil.move(art, temp_dir)
    shutil.rmtree(download_dir)
    os.makedirs(download_dir, exist_ok=True)
    for art in artifacts:
        shutil.move(os.path.join(temp_dir, os.path.basename(art)), download_dir)
    shutil.rmtree(temp_dir)

    print(f"✅ Final artifacts: {', '.join(os.listdir(download_dir))}")

if __name__ == "__main__":
    asyncio.run(main())
