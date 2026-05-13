import os
import sys
import re
import zipfile
import argparse
import asyncio
import shutil
from datetime import datetime
from urllib.parse import urlparse, unquote
import httpx


def sanitize_filename(name: str) -> str:
    """Remove characters that are unsafe in filenames."""
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def get_filename_from_url(url: str) -> str:
    """Extract a plausible filename from the URL path."""
    parsed = urlparse(url)
    path = unquote(parsed.path)
    basename = os.path.basename(path.strip('/'))
    if not basename or '.' not in basename:
        parts = [p for p in path.split('/') if p]
        basename = parts[-1] if parts else 'download'
        if '.' not in basename:
            basename += '.download'
    return sanitize_filename(basename)


async def download_file(client: httpx.AsyncClient, url: str, download_dir: str) -> str | None:
    """
    Download a file from `url` into `download_dir`.
    Returns the absolute path to the saved file, or None on failure.
    """
    try:
        response = await client.get(url, follow_redirects=True, timeout=60.0)
        response.raise_for_status()

        # Determine final filename (Content-Disposition has priority)
        content_disp = response.headers.get('content-disposition')
        if content_disp:
            match = re.search(r'filename[^;=\n]*=((["\'])(.*?)\2|[^;\n]*)', content_disp)
            if match:
                fname = match.group(3) or match.group(1)
                if fname:
                    fname = fname.strip('"\'')
                    filename = sanitize_filename(fname)
                else:
                    filename = get_filename_from_url(url)
            else:
                filename = get_filename_from_url(url)
        else:
            filename = get_filename_from_url(url)

        # Avoid overwriting existing files
        out_path = os.path.join(download_dir, filename)
        base, ext = os.path.splitext(filename)
        counter = 1
        while os.path.exists(out_path):
            out_path = os.path.join(download_dir, f"{base}_{counter}{ext}")
            counter += 1

        with open(out_path, 'wb') as f:
            f.write(response.content)
        print(f"✅ Downloaded: {os.path.basename(out_path)}")
        return out_path

    except Exception as e:
        print(f"❌ Failed to download {url}: {e}")
        return None


async def main():
    parser = argparse.ArgumentParser(description="Download a list of URLs and zip them.")
    parser.add_argument("--urls", required=True, help="Multi‑line string of URLs")
    args = parser.parse_args()

    # Split input into individual URLs
    urls = [line.strip() for line in args.urls.split('\n') if line.strip()]
    if not urls:
        print("No valid URLs provided.")
        sys.exit(1)

    # Temporary download folder
    tmp_dir = "temp_downloads"
    os.makedirs(tmp_dir, exist_ok=True)

    downloaded = []
    async with httpx.AsyncClient() as client:
        for url in urls:
            result = await download_file(client, url, tmp_dir)
            if result:
                downloaded.append(result)

    if not downloaded:
        print("Nothing was downloaded successfully.")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        sys.exit(1)

    # Create final ZIP inside direct_downloads/
    output_dir = "direct_downloads"
    os.makedirs(output_dir, exist_ok=True)

    first_fname = os.path.basename(downloaded[0])
    base_name = os.path.splitext(first_fname)[0][:50]  # truncate for safety
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_name = f"{sanitize_filename(base_name)}_{timestamp}.zip"
    zip_path = os.path.join(output_dir, zip_name)

    with zipfile.ZipFile(zip_path, 'w') as zf:
        for file_path in downloaded:
            arcname = os.path.basename(file_path)
            zf.write(file_path, arcname=arcname)

    print(f"📦 Created: {zip_path}")

    # Clean up temporary folder
    shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
