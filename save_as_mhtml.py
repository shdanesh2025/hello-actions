import asyncio
import zipfile
import os
import re
import sys
import argparse
from pyppeteer import launch
from urllib.parse import urlparse

def sanitize_filename(name: str) -> str:
    """Remove invalid characters for filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

async def save_mhtml_and_requests(url: str, mhtml_output: str, requests_output: str):
    """
    Save webpage as MHTML and capture all HTTPS request URLs.
    Steps:
      1. Wait for and click disclaimer button (id="disclaimer-over18btn")
      2. Wait for and click play button (class="play-btn")
      3. Capture snapshot & requests
    """
    browser = await launch(headless=True, args=['--no-sandbox'])
    page = await browser.newPage()

    # List to store captured HTTPS URLs
    https_requests = []

    # Intercept every request
    async def capture_request(request):
        req_url = request.url
        if req_url.startswith('https://'):
            https_requests.append(req_url)

    page.on('request', capture_request)

    # Navigate and wait for initial network idle
    await page.goto(url, waitUntil='networkidle0')
    print("Page loaded")

    # Step 1: Wait for disclaimer button and click it
    try:
        print("Waiting for disclaimer button '#disclaimer-over18btn'...")
        await page.waitForSelector('#disclaimer-over18btn', timeout=10000)  # 10 seconds
        await page.click('#disclaimer-over18btn')
        print("Clicked disclaimer button")
        # Give a moment for any DOM updates after click
        await asyncio.sleep(1)
    except Exception as e:
        print(f"Disclaimer button not found or click failed: {e}")

    # Step 2: Wait for play button and click it
    try:
        print("Waiting for play button '.play-btn'...")
        await page.waitForSelector('.play-btn', timeout=10000)
        await page.click('.play-btn')
        print("Clicked play button")
        # Wait for video/stream requests to be made
        await asyncio.sleep(3)
    except Exception as e:
        print(f"Play button not found or click failed: {e}")

    # Capture MHTML snapshot (after both clicks)
    mhtml_data = await page._client.send('Page.captureSnapshot', {})
    with open(mhtml_output, 'wb') as f:
        f.write(mhtml_data['data'].encode())

    # Write all captured HTTPS URLs to a text file
    with open(requests_output, 'w', encoding='utf-8') as f:
        for req_url in https_requests:
            f.write(req_url + '\n')

    await browser.close()
    print(f"Captured {len(https_requests)} HTTPS request URLs")

def main():
    parser = argparse.ArgumentParser(description="Download a webpage as MHTML + capture HTTPS requests.")
    parser.add_argument("--url", required=True, help="URL of the page to download")
    parser.add_argument("--title", help="Optional title for the output file (without extension)")
    args = parser.parse_args()

    # Determine output base name
    if args.title:
        base_name = sanitize_filename(args.title)
    else:
        parsed = urlparse(args.url)
        path = parsed.path.strip('/').replace('/', '_')
        if path:
            base_name = sanitize_filename(path)
        else:
            base_name = sanitize_filename(parsed.netloc)
        if not base_name:
            base_name = "webpage"

    mhtml_filename = f"{base_name}.mhtml"
    requests_filename = f"{base_name}_requests.txt"
    zip_filename = f"{base_name}.zip"

    # Create download directory
    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)

    # Temporary folder for files
    os.makedirs("temp", exist_ok=True)
    mhtml_path = os.path.join("temp", mhtml_filename)
    requests_path = os.path.join("temp", requests_filename)

    print(f"Processing {args.url} → {mhtml_filename} and capturing HTTPS requests")
    asyncio.run(save_mhtml_and_requests(args.url, mhtml_path, requests_path))

    # Create ZIP inside download folder containing both files
    zip_path = os.path.join(download_dir, zip_filename)
    with zipfile.ZipFile(zip_path, 'w') as zf:
        zf.write(mhtml_path, arcname=mhtml_filename)
        zf.write(requests_path, arcname=requests_filename)

    # Cleanup temp
    import shutil
    shutil.rmtree("temp", ignore_errors=True)

    print(f"✅ Created {zip_path} (contains {mhtml_filename} and {requests_filename})")

if __name__ == "__main__":
    main()
