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
    """Save webpage as MHTML (to get dynamic content)."""
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
    """Extract video info from a single video page."""
    soup = BeautifulSoup(html_content, 'html.parser')
    result = {
        'title': '',
        'duration': '',
        'views': '',
        'thumbnails': [],      # list of absolute URLs
        'video_links': []      # list of (quality, url)
    }

    # Title – try meta tags first
    title_tag = soup.find('meta', property='og:title')
    if title_tag and title_tag.get('content'):
        result['title'] = title_tag['content']
    else:
        h1 = soup.find('h1')
        if h1:
            result['title'] = h1.get_text(strip=True)

    # Duration – look for span with class 'duration'
    duration_span = soup.find('span', class_='duration')
    if duration_span:
        result['duration'] = duration_span.get_text(strip=True)

    # Views – look for text containing 'Views'
    views_text = soup.find(string=re.compile(r'[\d.]+[kKmM]?\s*[Vv]iews'))
    if views_text:
        match = re.search(r'([\d.]+[kKmM]?)\s*[Vv]iews', views_text)
        if match:
            result['views'] = match.group(1) + ' Views'

    # Thumbnails – collect all reasonable images
    # Main thumbnail from meta og:image
    og_image = soup.find('meta', property='og:image')
    if og_image and og_image.get('content'):
        result['thumbnails'].append(urljoin(base_url, og_image['content']))
    # Also collect any img inside .thumb or .video-preview
    for img in soup.select('img'):
        src = img.get('src')
        if src and ('thumb' in src.lower() or 'preview' in src.lower() or 'poster' in src.lower()):
            abs_url = urljoin(base_url, src)
            if abs_url not in result['thumbnails']:
                result['thumbnails'].append(abs_url)

    # Video download links (mp4 CDN)
    all_links = soup.find_all('a', href=True)
    for a in all_links:
        href = a['href']
        if href.startswith('https://mp4'):
            # Extract quality from e.g. video_360p.mp4
            match = re.search(r'video_(\d+)p', href)
            if match:
                quality = int(match.group(1))
                result['video_links'].append((quality, href))
    return result

def select_best_quality(video_links, desired_quality):
    """
    video_links: list of (quality, url)
    desired_quality: int (240, 360, 720, 1080)
    Returns the URL of the best available quality:
        - exact match if exists
        - else the highest available (>= desired? or just highest? We'll take highest available)
    """
    if not video_links:
        return None
    # Sort by quality descending
    video_links.sort(key=lambda x: x[0], reverse=True)
    # If exact match exists, return it
    for q, url in video_links:
        if q == desired_quality:
            return url
    # Otherwise return the highest (first after sorting)
    return video_links[0][1]

async def download_file(url, output_path, description="file"):
    """Download a file with httpx, showing progress."""
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

def generate_video_html(video_info, video_local_path, thumb_local_paths, original_url):
    """
    Generate HTML that displays the video, thumbnails, and metadata.
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
        .container {{ max-width: 1200px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); overflow: hidden; }}
        .video-section {{ padding: 20px; background: black; }}
        video {{ width: 100%; max-height: 70vh; }}
        .info-section {{ padding: 20px; }}
        h1 {{ margin-bottom: 10px; }}
        .metadata {{ margin-bottom: 20px; color: #666; display: flex; gap: 15px; flex-wrap: wrap; }}
        .thumb-gallery {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(150px,1fr)); gap: 10px; margin-top: 20px; }}
        .thumb-gallery img {{ width: 100%; cursor: pointer; border-radius: 4px; transition: transform 0.2s; }}
        .thumb-gallery img:hover {{ transform: scale(1.02); }}
        button {{ background: #007bff; color: white; border: none; padding: 10px 20px; border-radius: 5px; cursor: pointer; font-size: 16px; }}
        button:hover {{ background: #0056b3; }}
        .toast {{ position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: #333; color: white; padding: 12px 24px; border-radius: 8px; opacity: 0; transition: opacity 0.3s; pointer-events: none; z-index: 1000; }}
    </style>
</head>
<body>
<div class="container">
    <div class="video-section">
        <video controls autoplay loop>
            <source src="{video_local}" type="video/mp4">
            Your browser does not support the video tag.
        </video>
    </div>
    <div class="info-section">
        <h1>{title}</h1>
        <div class="metadata">
            <span>⏱️ {duration}</span>
            <span>👁️ {views}</span>
        </div>
        <button id="copyBtn">📋 Copy Video URL</button>
        <div class="thumb-gallery">
            {thumbnails_html}
        </div>
    </div>
</div>
<div class="toast" id="toast">Copied to clipboard!</div>
<script>
    const copyBtn = document.getElementById('copyBtn');
    const toast = document.getElementById('toast');
    const originalUrl = "{original_url}";
    function showToast() {{
        toast.style.opacity = '1';
        setTimeout(() => {{ toast.style.opacity = '0'; }}, 1500);
    }}
    async function copyToClipboard(text) {{
        try {{
            await navigator.clipboard.writeText(text);
            showToast();
        }} catch (err) {{
            console.error(err);
            alert('Press Ctrl+C to copy:\\n' + text);
        }}
    }}
    copyBtn.addEventListener('click', () => copyToClipboard(originalUrl));
    // Make thumbnails clickable to copy their source image URL? Not required, but nice.
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
        duration=video_info['duration'],
        views=video_info['views'],
        video_local=video_local_path,
        thumbnails_html=thumb_html,
        original_url=original_url
    )

# ----------------------------------------------------------------------
# Main async function
# ----------------------------------------------------------------------
async def main():
    parser = argparse.ArgumentParser(description="Download a single xvideos.com video with best quality.")
    parser.add_argument("--url", required=True, help="URL of the video page")
    parser.add_argument("--quality", choices=['240', '360', '720', '1080'], required=True,
                        help="Desired quality (auto picks best available if exact not found)")
    args = parser.parse_args()
    desired_quality = int(args.quality)

    # Step 1: Download the video page as MHTML (to handle JS)
    temp_mhtml = "temp_video.mhtml"
    print(f"Fetching page: {args.url}")
    await save_mhtml(args.url, temp_mhtml)
    html_content = extract_html_from_mhtml(temp_mhtml)
    video_info = parse_video_page(html_content, args.url)

    if not video_info['video_links']:
        print("No mp4 download links found!")
        shutil.rmtree("temp", ignore_errors=True)
        if os.path.exists(temp_mhtml):
            os.remove(temp_mhtml)
        return

    # Step 2: Select best quality URL
    best_url = select_best_quality(video_info['video_links'], desired_quality)
    best_quality = next((q for q, u in video_info['video_links'] if u == best_url), None)
    print(f"Selected quality: {best_quality}p (desired {desired_quality}p)")

    # Step 3: Prepare output directories
    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)
    images_dir = os.path.join(download_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    # Step 4: Download MP4 video
    title_safe = sanitize_filename(video_info['title'] or "video")
    video_filename = f"{title_safe}_{best_quality}p.mp4"
    video_path = os.path.join(download_dir, video_filename)
    print(f"Downloading video ({best_quality}p)...")
    await download_file(best_url, video_path, "video")

    # Step 5: Download all thumbnails
    thumb_local_paths = []
    for i, thumb_url in enumerate(video_info['thumbnails']):
        parsed = urlparse(thumb_url)
        ext = os.path.splitext(parsed.path)[1] or '.jpg'
        thumb_filename = f"thumb_{i+1}{ext}"
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
                print(f"Failed thumbnail {thumb_url}: {e}")
                continue
        thumb_local_paths.append(os.path.join("images", thumb_filename))

    # Step 6: Generate HTML
    html_content = generate_video_html(video_info, video_filename, thumb_local_paths, args.url)
    index_html_path = os.path.join(download_dir, "index.html")
    with open(index_html_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

    # Step 7: Create final ZIP
    final_zip_name = f"{title_safe}_{best_quality}p.zip"
    final_zip_path = os.path.join(download_dir, final_zip_name)
    with zipfile.ZipFile(final_zip_path, 'w') as zf:
        zf.write(index_html_path, arcname="index.html")
        zf.write(video_path, arcname=video_filename)
        for root, _, files in os.walk(images_dir):
            for file in files:
                full_path = os.path.join(root, file)
                arcname = os.path.relpath(full_path, download_dir)
                zf.write(full_path, arcname=arcname)

    # Cleanup: remove intermediate files (keep only the ZIP)
    os.remove(index_html_path)
    os.remove(video_path)
    shutil.rmtree(images_dir)
    os.remove(temp_mhtml)

    print(f"✅ Created {final_zip_path} with video, HTML, and thumbnails.")

if __name__ == "__main__":
    asyncio.run(main())
