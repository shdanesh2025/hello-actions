import asyncio
import zipfile
import os
import re
import shutil
import argparse
from pyppeteer import launch
from urllib.parse import urlencode

def sanitize_filename(name: str) -> str:
    """Remove invalid characters for filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)

def parse_pages(pages_spec: str):
    """
    Parse pages argument. Examples:
    '0'        -> [0]
    '0-4'      -> [0,1,2,3,4]
    '0,2,5'    -> [0,2,5]
    '1-3,7-9'  -> [1,2,3,7,8,9]
    """
    pages = set()
    parts = pages_spec.split(',')
    for part in parts:
        if '-' in part:
            start, end = map(int, part.split('-'))
            pages.update(range(start, end+1))
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
    return mapping.get(durf_option, '')  # empty = all durations

async def save_mhtml(url: str, output_file: str):
    """Save webpage as MHTML."""
    browser = await launch(headless=True, args=['--no-sandbox'])
    page = await browser.newPage()
    await page.goto(url, waitUntil='networkidle0')
    mhtml_data = await page._client.send('Page.captureSnapshot', {})
    with open(output_file, 'wb') as f:
        f.write(mhtml_data['data'].encode())
    await browser.close()

def main():
    parser = argparse.ArgumentParser(description="Download xvideos.com search results pages collectively as one ZIP.")
    parser.add_argument("--query", required=True, help="Search query (e.g., 'sophie dee anal')")
    parser.add_argument("--durf", choices=['short', 'medium', 'long', 'extralong'], default=None,
                        help="Duration filter")
    parser.add_argument("--pages", required=True, help="Page numbers: e.g., '0', '0-4', '0,2,5'")
    args = parser.parse_args()

    # Build base URL
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

    # Prepare directories
    download_dir = "download"
    os.makedirs(download_dir, exist_ok=True)
    temp_dir = "temp_mhtml"
    os.makedirs(temp_dir, exist_ok=True)

    saved_files = []

    for page_num in pages_to_download:
        page_url = f"{base_search_url}&p={page_num}"
        query_safe = sanitize_filename(args.query.replace(' ', '_'))
        durf_safe = args.durf if args.durf else "allduration"
        mhtml_filename = f"{query_safe}_{durf_safe}_p{page_num}.mhtml"
        mhtml_path = os.path.join(temp_dir, mhtml_filename)

        print(f"Downloading page {page_num}: {page_url} -> {mhtml_filename}")
        try:
            await save_mhtml(page_url, mhtml_path)
            saved_files.append(mhtml_path)
        except Exception as e:
            print(f"Error downloading page {page_num}: {e}")
            continue

    if not saved_files:
        print("No pages were successfully downloaded. Exiting.")
        shutil.rmtree(temp_dir, ignore_errors=True)
        return

    # Create a single ZIP file containing all .mhtml files
    pages_range_str = f"p{min(pages_to_download)}-{max(pages_to_download)}" if len(pages_to_download) > 1 else f"p{pages_to_download[0]}"
    zip_filename = f"{query_safe}_{durf_safe}_{pages_range_str}.zip"
    zip_path = os.path.join(download_dir, zip_filename)

    with zipfile.ZipFile(zip_path, 'w') as zf:
        for filepath in saved_files:
            arcname = os.path.basename(filepath)
            zf.write(filepath, arcname=arcname)

    print(f"✅ Successfully created {zip_path} containing {len(saved_files)} MHTML files.")

    # Cleanup temp directory
    shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    asyncio.run(main())
