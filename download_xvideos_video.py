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
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup

# ----------------------------------------------------------------------
# Helper functions
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
    soup = BeautifulSoup(html_content, 'html.parser')
    result = {
        'title': '',
        'duration': '',
        'views': '',
        'thumbnails': [],
        'video_links': []
    }

    # Title
    title_tag = soup.find('meta', property='og:title')
    if title_tag and title_tag.get('content'):
        result['title'] = title_tag['content']
    else:
        h1 = soup.find('h1')
        if h1:
            result['title'] = h1.get_text(strip=True)
    if not result['title']:
        result['title'] = "Untitled"

    # Duration
    duration_span = soup.find('span', class_='duration')
    if duration_span:
        result['duration'] = duration_span.get_text(strip=True)

    # Views
    views_text = soup.find(string=re.compile(r'[\d.]+[kKmM]?\s*[Vv]iews'))
    if views_text:
        match = re.search(r'([\d.]+[kKmM]?)\s*[Vv]iews', views_text)
        if match:
            result['views'] = match.group(1) + ' Views'

    # Thumbnails
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        result['thumbnails'].append(urljoin(base_url, og_image['content']))
    for img in soup.select('img'):
        src = img.get('src')
        if src and ('thumb' in src.lower() or 'preview' in src.lower() or 'poster' in src.lower()):
            abs_url = urljoin(base_url, src)
            if abs_url not in result['thumbnails']:
                result['thumbnails'].append(abs_url)

    # Video links
    for a in soup.find_all('a', href=True):
        href = a['href']
        if href.startswith('https://mp4'):
            match = re.search(r'video_(\d+)p', href)
            if match:
                quality = int(match.group(1))
                result['video_links'].append((quality, href))
    return result

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
# HTML generator for a single video (with thumbnail grid control)
# ----------------------------------------------------------------------
def generate_video_html(video_info, video_local_path, thumb_local_paths, original_url, video_index=0):
    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .video-section {{ padding: 20px; background: black; }}
        video {{ width: 100%; max-height: 70vh; }}
        .info-section {{ padding: 20px; }}
        h1 {{ margin-bottom: 10px; }}
        .metadata {{ margin-bottom: 20px; color: #666; display: flex; gap: 15px; flex-wrap: wrap; }}
        .controls {{ background: #f0f0f0; padding: 10px; border-radius: 8px; margin-bottom: 20px; display: flex; gap: 15px; align-items: center; }}
        .controls label {{ font-weight: bold; }}
        .controls select {{ padding: 6px 10px; border-radius: 4px; border: 1px solid #ccc; }}
        button {{ background: #007bff; color: white; border: none; padding: 8px 16px; border-radius: 5px; cursor: pointer; }}
        button:hover {{ background: #0056b3; }}
        .thumb-gallery {{ display: grid; gap: 10px; margin-top: 20px; }}
        .thumb-gallery.cols-2 {{ grid-template-columns: repeat(2, 1fr); }}
        .thumb-gallery.cols-3 {{ grid-template-columns: repeat(3, 1fr); }}
        .thumb-gallery.cols-4 {{ grid-template-columns: repeat(4, 1fr); }}
        .thumb-gallery img {{ width: 100%; aspect-ratio: 16/9; object-fit: cover; cursor: pointer; border-radius: 4px; transition: transform 0.2s; }}
        .thumb-gallery img:hover {{ transform: scale(1.02); }}
        .toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 12px 24px; border-radius: 8px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 1000; }}
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
        <div class="controls">
            <label>Thumbnail columns: </label>
            <select id="columnSelect">
                <option value="2">2 columns</option>
                <option value="3" selected>3 columns</option>
                <option value="4">4 columns</option>
            </select>
            <button id="copyBtn">📋 Copy Video URL</button>
        </div>
        <div class="thumb-gallery cols-3" id="gallery">
            {thumbnails_html}
        </div>
    </div>
</div>
<div class="toast" id="toast">Copied to clipboard!</div>
<script>
    const columnSelect = document.getElementById('columnSelect');
    const gallery = document.getElementById('gallery');
    const copyBtn = document.getElementById('copyBtn');
    const toast = document.getElementById('toast');
    const originalUrl = "{original_url}";

    columnSelect.addEventListener('change', (e) => {{
        gallery.className = `thumb-gallery cols-${{e.target.value}}`;
    }});

    function showToast(message) {{
        toast.textContent = message;
        toast.style.opacity = '1';
        setTimeout(() => {{ toast.style.opacity = '0'; }}, 1500);
    }}

    async function copyToClipboard(text) {{
        try {{
            await navigator.clipboard.writeText(text);
            showToast('Copied!');
        }} catch (err) {{
            alert('Press Ctrl+C to copy:\\n' + text);
        }}
    }}
    copyBtn.addEventListener('click', () => copyToClipboard(originalUrl));
    // Make thumbnails clickable to copy their URLs
    const thumbs = document.querySelectorAll('.thumb-gallery img');
    thumbs.forEach(img => {{
        img.addEventListener('click', () => copyToClipboard(img.src));
    }});
</script>
</body>
</html>"""
    thumb_html = '\n'.join([f'<img src="{path}" alt="Thumbnail">' for path in thumb_local_paths])
    return html_template.format(
        title=video_info['title'],
        duration=video_info['duration'] or 'Unknown',
        views=video_info['views'] or 'Unknown',
        video_local=video_local_path,
        thumbnails_html=thumb_html,
        original_url=original_url
    )

# ----------------------------------------------------------------------
# Process a single video URL (download video & assets, return info)
# ----------------------------------------------------------------------
async def process_video(url, desired_quality, download_dir, images_dir, video_index):
    temp_mhtml = f"temp_video_{video_index}.mhtml"
    try:
        print(f"\n--- Processing {url} ---")
        await save_mhtml(url, temp_mhtml)
        html_content = extract_html_from_mhtml(temp_mhtml)
        video_info = parse_video_page(html_content, url)

        if not video_info['video_links']:
            print(f"No mp4 links for {url}")
            os.remove(temp_mhtml)
            return None

        best_url = select_best_quality(video_info['video_links'], desired_quality)
        best_quality = next((q for q, u in video_info['video_links'] if u == best_url), None)
        print(f"Selected quality: {best_quality}p (desired {desired_quality}p)")

        # Download video
        title_safe = sanitize_filename(video_info['title'] or f"video_{video_index}")
        video_filename = f"{title_safe}_{best_quality}p.mp4"
        video_path = os.path.join(download_dir, video_filename)
        await download_file(best_url, video_path, "video")

        # Download thumbnails (unique per video, but store in shared images/ with unique names)
        thumb_local_paths = []
        for i, thumb_url in enumerate(video_info['thumbnails']):
            parsed = urlparse(thumb_url)
            ext = os.path.splitext(parsed.path)[1] or '.jpg'
            # Use video title hash + index to avoid name collisions
            thumb_filename = f"{title_safe}_thumb_{i+1}{ext}"
            thumb_path = os.path.join(images_dir, thumb_filename)
            if not os.path.exists(thumb_path):
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.get(thumb_url)
                        resp.raise_for_status()
                        with open(thumb_path, 'wb') as f:
                            f.write(resp.content)
                    print(f"Downloaded thumbnail {i+1}")
                except Exception as e:
                    print(f"Failed thumb {thumb_url}: {e}")
                    continue
            thumb_local_paths.append(os.path.join("images", thumb_filename))

        # Generate HTML for this video
        html_filename = f"{title_safe}.html"
        html_path = os.path.join(download_dir, html_filename)
        html_content = generate_video_html(video_info, video_filename, thumb_local_paths, url, video_index)
        with open(html_path, 'w', encoding='utf-8') as f:
            f.write(html_content)

        # Cleanup temp
        os.remove(temp_mhtml)
        return {
            'title': video_info['title'],
            'html': html_filename,
            'video': video_filename,
            'thumb_count': len(thumb_local_paths)
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
    parser = argparse.ArgumentParser(description="Download one or more xvideos.com videos.")
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
<html>
<head><meta charset="UTF-8"><title>Downloaded Videos</title><style>
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
                    continue  # don't zip itself
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, download_dir)
                zf.write(full_path, arcname=arcname)

    # Cleanup: remove all files except the zip? Actually we keep the zip; the workflow will commit it.
    # But we need to leave only the zip in download_dir for the GitHub action to commit.
    for root, _, files in os.walk(download_dir):
        for file in files:
            if file != final_zip_name:
                os.remove(os.path.join(root, file))
    # Remove empty subdirs
    for root, dirs, _ in os.walk(download_dir, topdown=False):
        for d in dirs:
            try:
                os.rmdir(os.path.join(root, d))
            except OSError:
                pass

    print(f"✅ Created {final_zip_path} with {len(results)} videos.")

if __name__ == "__main__":
    asyncio.run(main())
