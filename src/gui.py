#!/usr/bin/env python3
"""PyQt6 GUI to preview and selectively download Alpha Coders wallpapers."""

import hashlib
import json
import os
import re
import sys
import threading
import time
import shutil
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse
from io import BytesIO
from PIL import Image, ImageOps

import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QPixmap, QIcon
from PyQt6.QtCore import QUrl
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget
)

BASE = "https://wall.alphacoders.com"
TAG_BASE = "https://alphacoders.com"
MAX_IMAGES = 50
MAX_SEARCH_PAGES = 5
REQUEST_DELAY = 0.5
TIMEOUT = 30
CHUNK_SIZE = 128 * 1024
THUMBNAIL_SIZE = (210, 135)

# Persistent cache configuration.
APP_DIR = Path(__file__).resolve().parent

if sys.platform.startswith("linux"):
    # Linux: store writable app data outside read-only AppImage mounts.
    CACHE_HOME = Path(
        os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    )
    CONFIG_HOME = Path(
        os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    )

    CACHE_DIR = CACHE_HOME / APP_NAME
    SETTINGS_DIR = CONFIG_HOME / APP_NAME

elif sys.platform == "darwin":
    # macOS: use standard user cache and application support directories.
    CACHE_DIR = Path.home() / "Library" / "Caches" / APP_NAME
    SETTINGS_DIR = (
        Path.home() / "Library" / "Application Support" / APP_NAME
    )

else:
    # Windows and other platforms: preserve the existing directory-based setup.
    CACHE_DIR = APP_DIR / "cache"
    SETTINGS_DIR = APP_DIR / "settings"

THUMBNAIL_CACHE_DIR = CACHE_DIR / "thumbnails"
METADATA_CACHE_DIR = CACHE_DIR / "metadata"

SETTINGS_FILE = SETTINGS_DIR / "settings.json"
ROLE_ASSIGNMENTS_FILE = SETTINGS_DIR / "role_assignments.json"

SEARCH_CACHE_TTL = 10 * 60
DETAIL_CACHE_TTL = 30 * 24 * 60 * 60
MAX_THUMBNAIL_BYTES = 12 * 1024 * 1024
MAX_CACHED_THUMBNAIL_BYTES = 300 * 1024
THUMBNAIL_CACHE_SIZE = (420, 270)
THUMBNAIL_JPEG_QUALITY = 80
MAX_THUMBNAIL_CACHE_BYTES = 100 * 1024 * 1024

THUMBNAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
METADATA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

# Protect cache-file operations if multiple workers are added later.
_CACHE_LOCK = threading.Lock()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0 Safari/537.36"
    )
}

class DownloadError(Exception):
    pass

class SearchCancelled(Exception):
    """Raised when the user cancels a search."""
    pass

# Thumbnail cleanup to keep the thumbnail folder under 300 mb

def trim_thumbnail_cache():
    """Remove the oldest cached thumbnails until the cache is under its limit."""
    THUMBNAIL_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    with _CACHE_LOCK:
        files = []

        for path in THUMBNAIL_CACHE_DIR.iterdir():
            try:
                if path.is_file() and not path.name.endswith(".tmp"):
                    files.append((path, path.stat().st_size, path.stat().st_mtime))
            except OSError:
                continue

        total_size = sum(size for _, size, _ in files)

        # Oldest files are removed first.
        files.sort(key=lambda item: item[2])

        for path, size, _ in files:
            if total_size <= MAX_THUMBNAIL_CACHE_BYTES:
                break

            try:
                path.unlink()
                total_size -= size
            except OSError:
                pass

# ------------------------------------------------------------
# Cache helpers
# ------------------------------------------------------------

def _cache_key(value):
    """Create a stable filename from a URL or other string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def _metadata_cache_path(category, key):
    return METADATA_CACHE_DIR / f"{category}_{_cache_key(key)}.json"

def _read_metadata_cache(category, key, ttl):
    """Read a non-expired JSON cache entry, or return None."""
    path = _metadata_cache_path(category, key)

    try:
        with _CACHE_LOCK:
            if not path.is_file():
                return None

            if time.time() - path.stat().st_mtime > ttl:
                return None

            return json.loads(path.read_text(encoding="utf-8"))

    except (OSError, ValueError, TypeError):
        return None

def _write_metadata_cache(category, key, value):
    """Write JSON using atomic replacement."""
    path = _metadata_cache_path(category, key)
    temp_path = path.with_name(
        f"{path.name}.{threading.get_ident()}.tmp"
    )

    try:
        serialized = json.dumps(value, ensure_ascii=False)

        with _CACHE_LOCK:
            temp_path.write_text(serialized, encoding="utf-8")
            os.replace(temp_path, path)

    except (OSError, TypeError, ValueError):
        pass

    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

def normalize_tag(value):
    return re.sub(r"[^\w]+", " ", value.casefold()).strip()


def tag_matches(actual_tag, requested_tag):
    """Match exact tags or base names with parenthetical qualifiers."""
    actual = normalize_tag(actual_tag)
    requested = normalize_tag(requested_tag)

    if actual == requested:
        return True

    match = re.fullmatch(r"\s*(.+?)\s*\(([^()]*)\)\s*", actual_tag)
    return bool(match and normalize_tag(match.group(1)) == requested)

def safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip())
    name = name.rstrip(" .")[:100]
    name = name or "wallpapers"

    # Windows reserved device names are invalid even with extensions.
    reserved = {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }

    if name.split(".")[0].upper() in reserved:
        name = f"_{name}"

    return name

# Compress the thumbnails for faster loading and reducing the disk space footprint
def compress_thumbnail(data):
    """Resize and compress an image for use as a GUI thumbnail."""
    try:
        with Image.open(BytesIO(data)) as image:
            # Correct photos with EXIF orientation metadata.
            image = ImageOps.exif_transpose(image)

            # Preserve aspect ratio while reducing dimensions.
            image.thumbnail(
                THUMBNAIL_CACHE_SIZE,
                Image.Resampling.LANCZOS,
            )

            # JPEG does not support transparency.
            image = image.convert("RGB")

            output = BytesIO()
            image.save(
                output,
                format="JPEG",
                quality=THUMBNAIL_JPEG_QUALITY,
                optimize=True,
            )

            compressed = output.getvalue()

            if len(compressed) > MAX_CACHED_THUMBNAIL_BYTES:
                return b""

            return compressed

    except (OSError, ValueError, Image.DecompressionBombError):
        return b""

class AlphaCoders:
    def __init__(self, cancel_event=None):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.cancel_event = cancel_event
        self.last_request = 0.0

    def check_cancelled(self):
        if self.cancel_event and self.cancel_event.is_set():
            raise SearchCancelled()

    def get(self, url, stream=False):
        self.check_cancelled()

        delay = REQUEST_DELAY - (time.monotonic() - self.last_request)

        if delay > 0 and self.cancel_event:
            if self.cancel_event.wait(delay):
                raise SearchCancelled()
        elif delay > 0:
            time.sleep(delay)

        self.check_cancelled()

        try:
            response = self.session.get(
                url,
                timeout=TIMEOUT,
                stream=stream,
            )
            self.last_request = time.monotonic()

        except requests.RequestException as exc:
            raise DownloadError(f"Network error: {exc}") from exc

        if self.cancel_event and self.cancel_event.is_set():
            response.close()
            raise SearchCancelled()

        if response.status_code == 429:
            response.close()
            raise DownloadError(
                "Alpha Coders rate-limited the request (HTTP 429). "
                "Wait before trying again."
            )

        if response.status_code in (401, 403):
            status = response.status_code
            response.close()
            raise DownloadError(f"Access denied (HTTP {status}).")

        try:
            response.raise_for_status()

        except requests.HTTPError as exc:
            status = response.status_code
            response.close()
            raise DownloadError(f"HTTP {status}: {url}") from exc

        return response

    def soup(self, url):
        response = self.get(url)

        try:
            return BeautifulSoup(response.text, "html.parser")
        finally:
            response.close()

    def _extract_detail_links(self, soup):
        results, seen = [], set()

        for link in soup.find_all("a", href=True):
            full_url = urljoin(BASE + "/", link["href"])
            parsed = urlparse(full_url)

            if parsed.hostname not in (
                "wall.alphacoders.com",
                "alphacoders.com",
            ):
                continue

            match = re.search(r"/big\.php\?i=(\d+)", full_url)

            if match:
                image_id = match.group(1)

                if image_id not in seen:
                    seen.add(image_id)
                    results.append((image_id, full_url))

        return results

    def _cached_search(self, url):
        """Return cached search results when the entry is still fresh."""
        cached = _read_metadata_cache(
            "search",
            url,
            SEARCH_CACHE_TTL,
        )

        if isinstance(cached, list):
            try:
                return [
                    (str(image_id), str(detail_url))
                    for image_id, detail_url in cached
                ]
            except (TypeError, ValueError):
                pass

        results = self._extract_detail_links(self.soup(url))

        # Cache only non-empty results so temporary site issues aren't
        # remembered as valid empty searches.
        if results:
            _write_metadata_cache("search", url, results)

        return results

    def search(self, term, page=1):
        url = f"{BASE}/search.php?search={quote_plus(term)}&page={page}"
        return self._cached_search(url)

    def search_by_tag(self, tag, page=1):
        normalized = normalize_tag(tag)

        known_slugs = {
            "saber": "saber-(fate-series)",
            "saber fate series": "saber-(fate-series)",
        }

        slug = known_slugs.get(normalized)

        if slug is None:
            slug = re.sub(r"\s+", "-", tag.strip().casefold())
            slug = quote_plus(slug).replace("+", "-")

        url = f"{TAG_BASE}/{slug}-wallpapers"

        if page > 1:
            url += f"?page={page}"

        return self._cached_search(url)

    def page_tags(self, detail_url):
        """Read cached tags or extract them from the detail page."""
        cached = _read_metadata_cache(
            "tags",
            detail_url,
            DETAIL_CACHE_TTL,
        )

        if isinstance(cached, list):
            return [str(tag) for tag in cached]

        soup = self.soup(detail_url)

        label = soup.find(
            string=re.compile(r"^\s*Tags:\s*$", re.IGNORECASE)
        )

        if label is None:
            return []

        tags, seen = [], set()

        for element in label.next_elements:
            if isinstance(element, NavigableString):
                text = " ".join(str(element).split()).casefold()

                if text in {"related:", "related"}:
                    break

            elif isinstance(element, Tag) and element.name == "a":
                name = element.get_text(" ", strip=True)

                if name and name.casefold() not in seen:
                    seen.add(name.casefold())
                    tags.append(name)

        if tags:
            _write_metadata_cache("tags", detail_url, tags)

        return tags

    def preview_image(self, detail_url):
        """Return a cached or freshly extracted preview image URL."""
        cached = _read_metadata_cache(
            "preview",
            detail_url,
            DETAIL_CACHE_TTL,
        )

        if isinstance(cached, str) and cached:
            return cached

        soup = self.soup(detail_url)
        meta = soup.select_one('meta[property="og:image"]')

        if meta and meta.get("content"):
            candidate = urljoin(detail_url, meta["content"].strip())

            if self.is_image_host(candidate):
                _write_metadata_cache("preview", detail_url, candidate)
                return candidate

        # Fall back to image URLs found in links/data attributes.
        for element in soup.select("[data-href], a[href]"):
            href = element.get("data-href") or element.get("href")

            if href:
                candidate = urljoin(detail_url, href)

                if self.is_image_host(candidate):
                    _write_metadata_cache(
                        "preview",
                        detail_url,
                        candidate,
                    )
                    return candidate

        return ""

    def fetch_thumbnail(self, url):
        """Load a thumbnail from disk or download and cache it."""
        if not url:
            return b""

        cache_path = THUMBNAIL_CACHE_DIR / _cache_key(url)

        # Disk-cache hit: no HTTP request or network delay required.
        if cache_path.is_file():
            try:
                data = cache_path.read_bytes()

                if data and len(data) <= MAX_CACHED_THUMBNAIL_BYTES:
                    cache_path.touch()
                    return data

                cache_path.unlink(missing_ok=True)

            except OSError:
                pass

        try:
            with self.get(url, stream=True) as response:
                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

                if not content_type.startswith("image/"):
                    return b""

                data = bytearray()

                for chunk in response.iter_content(32 * 1024):
                    self.check_cancelled()

                    if not chunk:
                        continue

                    data.extend(chunk)

                    if len(data) > MAX_THUMBNAIL_BYTES:
                        return b""

                if not data:
                    return b""

                image_bytes = compress_thumbnail(bytes(data))

                if not image_bytes:
                    return b""

                # Write the cache file only after the full image was read.
                temp_path = cache_path.with_name(
                    f"{cache_path.name}.{threading.get_ident()}.tmp"
                )

                try:
                    with _CACHE_LOCK:
                        temp_path.write_bytes(image_bytes)
                        os.replace(temp_path, cache_path)

                finally:
                    try:
                        temp_path.unlink(missing_ok=True)
                    except OSError:
                        pass

                trim_thumbnail_cache()

                return image_bytes

        except (DownloadError, OSError):
            return b""

    def original_image_url(self, detail_url):
        soup = self.soup(detail_url)
        candidates = []

        meta = soup.select_one('meta[property="og:image"]')

        if meta and meta.get("content"):
            candidate = urljoin(detail_url, meta["content"].strip())

            if self.is_image_host(candidate):
                candidates.append(candidate)

        for element in soup.select("[data-href], a[href]"):
            href = element.get("data-href") or element.get("href")

            if not href:
                continue

            candidate = urljoin(detail_url, href)

            if (
                self.is_image_host(candidate)
                and candidate not in candidates
            ):
                candidates.append(candidate)

        for candidate in candidates:
            try:
                with self.get(candidate, stream=True) as response:
                    content_type = response.headers.get(
                        "Content-Type", ""
                    ).lower()

                    if content_type.startswith("image/"):
                        return candidate

            except DownloadError:
                continue

        raise DownloadError(
            "Could not verify an image URL on the detail page."
        )

    @staticmethod
    def is_image_host(url):
        try:
            host = urlparse(url).hostname or ""

            return (
                host == "alphacoders.com"
                or host.endswith(".alphacoders.com")
            )

        except ValueError:
            return False

    def download(self, url, destination):
        temp = destination.with_suffix(destination.suffix + ".part")

        try:
            with self.get(url, stream=True) as response:
                content_type = response.headers.get(
                    "Content-Type", ""
                ).lower()

                if not content_type.startswith("image/"):
                    raise DownloadError(
                        "URL did not return an image "
                        f"(Content-Type: {content_type or 'unknown'})."
                    )

                with temp.open("wb") as file:
                    for chunk in response.iter_content(CHUNK_SIZE):
                        if chunk:
                            file.write(chunk)

            if not temp.exists() or temp.stat().st_size == 0:
                raise DownloadError("Downloaded file was empty.")

            temp.replace(destination)
            return destination.stat().st_size

        finally:
            if temp.exists():
                temp.unlink()

class SearchWorker(QObject):
    result_found = pyqtSignal(dict)
    status = pyqtSignal(str)
    finished = pyqtSignal(int, str)

    def __init__(self, series, tag, limit):
        super().__init__()
        self.series = series
        self.tag = tag
        self.limit = limit
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        client = AlphaCoders(self.cancel_event)
        seen = set()
        count = 0

        try:
            self.status.emit(
                f'Searching Alpha Coders for "{self.series}"...'
            )

            for page in range(1, MAX_SEARCH_PAGES + 1):
                client.check_cancelled()

                self.status.emit(f"Searching results page {page}...")

                results = (
                    client.search_by_tag(self.tag, page)
                    if self.tag
                    else client.search(self.series, page)
                )

                client.check_cancelled()

                if not results:
                    if page == 1:
                        raise DownloadError(
                            "No image detail links found. Check the search term "
                            "or Alpha Coders page markup."
                        )
                    break

                for image_id, detail_url in results:
                    client.check_cancelled()

                    if image_id in seen:
                        continue

                    seen.add(image_id)

                    try:
                        tags = client.page_tags(detail_url)
                    except SearchCancelled:
                        raise
                    except DownloadError as exc:
                        self.status.emit(f"Skipping {image_id}: {exc}")
                        continue

                    client.check_cancelled()

                    if self.tag and not any(
                        tag_matches(t, self.tag) for t in tags
                    ):
                        continue

                    if not any(
                        tag_matches(t, self.series) for t in tags
                    ):
                        continue

                    self.status.emit(
                        f"Preparing preview for image {image_id}..."
                    )

                    thumb_url = client.preview_image(detail_url)
                    client.check_cancelled()

                    thumb_data = client.fetch_thumbnail(thumb_url)
                    client.check_cancelled()

                    self.result_found.emit({
                        "id": image_id,
                        "detail_url": detail_url,
                        "thumbnail": thumb_data,
                        "tags": tags,
                    })

                    count += 1

                    if count >= self.limit:
                        self.finished.emit(count, "")
                        return

            self.finished.emit(count, "")

        except SearchCancelled:
            self.finished.emit(count, "CANCELLED")

        except DownloadError as exc:
            self.finished.emit(count, str(exc))



class DownloadWorker(QObject):
    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(int, int, str)

    VALID_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    def __init__(self, items, folder):
        super().__init__()
        self.items = items
        self.folder = folder

    def get_destination_folder(self, item):
        """Return the folder where the original should be stored."""
        if item.get("cover"):
            return self.folder / "Cover Images"

        if item.get("donation"):
            return self.folder / "Donation Images"

        return self.folder

    def find_existing_image(self, image_id):
        """Find an existing original in the series or role folders."""
        search_folders = [
            self.folder,
            self.folder / "Cover Images",
            self.folder / "Donation Images",
        ]

        for directory in search_folders:
            if not directory.is_dir():
                continue

            for path in directory.glob(f"alphacoders_{image_id}.*"):
                try:
                    if (
                        path.is_file()
                        and path.stat().st_size > 0
                        and path.suffix.lower() in self.VALID_SUFFIXES
                    ):
                        return path
                except OSError:
                    continue

        return None

    def place_image(self, source, item):
        """Move/copy an image into its required role destinations."""
        roles = []

        if item.get("cover"):
            roles.append(self.folder / "Cover Images")

        if item.get("donation"):
            roles.append(self.folder / "Donation Images")

        # No special role: keep the image in the series folder.
        if not roles:
            destination = self.folder / source.name

            if source.resolve() != destination.resolve():
                shutil.copy2(source, destination)

            return [destination]

        destinations = []

        for role_folder in roles:
            role_folder.mkdir(parents=True, exist_ok=True)
            destination = role_folder / source.name

            if source.resolve() != destination.resolve():
                if not destination.exists():
                    shutil.copy2(source, destination)

            destinations.append(destination)

        # Remove the series-folder copy when the image has roles.
        series_copy = self.folder / source.name

        if (
            series_copy.exists()
            and series_copy.resolve() != source.resolve()
        ):
            series_copy.unlink()

        # If the source itself is in the series folder, remove it only
        # after its role-folder copies have been created successfully.
        if (
            source.parent.resolve() == self.folder.resolve()
            and source.exists()
        ):
            source.unlink()

        return destinations

    def run(self):
        client = AlphaCoders()
        success = 0
        failed = 0
        total = len(self.items)

        try:
            self.folder.mkdir(parents=True, exist_ok=True)

            for index, item in enumerate(self.items, 1):
                image_id = str(item["id"])

                try:
                    destination_folder = self.get_destination_folder(item)
                    destination_folder.mkdir(parents=True, exist_ok=True)

                    existing = self.find_existing_image(image_id)

                    if existing:
                        source = existing
                        message = f"Reusing {source.name}"
                    else:
                        image_url = client.original_image_url(
                            item["detail_url"]
                        )

                        suffix = Path(
                            urlparse(image_url).path
                        ).suffix.lower()

                        if suffix not in self.VALID_SUFFIXES:
                            suffix = ".jpg"

                        source = destination_folder / (
                            f"alphacoders_{image_id}{suffix}"
                        )

                        size = client.download(image_url, source)
                        message = f"Downloaded {source.name} ({size:,} bytes)"

                    destinations = self.place_image(source, item)

                    message += " | Saved to: " + ", ".join(
                        str(path.parent.name)
                        for path in destinations
                    )

                    success += 1
                    self.progress.emit(index, total, message)

                except (DownloadError, OSError, shutil.Error) as exc:
                    failed += 1
                    self.progress.emit(
                        index,
                        total,
                        f"Failed {image_id}: {exc}",
                    )

                    if "rate-limit" in str(exc).casefold():
                        break

            self.finished.emit(
                success,
                failed,
                str(self.folder.resolve()),
            )

        except OSError as exc:
            self.finished.emit(
                success,
                failed + 1,
                f"Output-folder error: {exc}",
            )

class PreviewCard(QGroupBox):
    selection_changed = pyqtSignal()

    def __init__(self, item, main_window, series):
        super().__init__()
        self.item = item
        self.main_window = main_window
        self.series = series
        self.image_id = str(item["id"])

        self.setCheckable(False)
        self.setMinimumWidth(230)
        self.setMaximumWidth(280)

        layout = QVBoxLayout(self)

        self.image_label = QLabel("Thumbnail unavailable")
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label.setFixedSize(*THUMBNAIL_SIZE)
        self.image_label.setStyleSheet(
            "background: #252525; color: #dddddd;"
        )

        data = item.get("thumbnail", b"")

        if data:
            pixmap = QPixmap()
            if pixmap.loadFromData(data):
                self.image_label.setPixmap(
                    pixmap.scaled(
                        *THUMBNAIL_SIZE,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )

        layout.addWidget(
            self.image_label,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )

        self.checkbox = QCheckBox(f"Select image {self.image_id}")
        self.checkbox.setChecked(False)
        self.checkbox.stateChanged.connect(
            lambda _state: self.selection_changed.emit()
        )
        layout.addWidget(self.checkbox)

        # Independent, persistent image roles.
        roles = self.main_window.get_image_roles(
            self.series,
            self.image_id,
        )

        self.cover_checkbox = QCheckBox("Cover image")
        self.cover_checkbox.setChecked(roles.get("cover", False))
        self.cover_checkbox.toggled.connect(
            lambda checked: self._set_role("cover", checked)
        )
        layout.addWidget(self.cover_checkbox)

        self.donation_checkbox = QCheckBox("Donation image")
        self.donation_checkbox.setChecked(roles.get("donation", False))
        self.donation_checkbox.toggled.connect(
            lambda checked: self._set_role("donation", checked)
        )
        layout.addWidget(self.donation_checkbox)

        open_button = QPushButton("Open wallpaper page")
        open_button.clicked.connect(
            lambda: QDesktopServices.openUrl(
                QUrl(self.item["detail_url"])
            )
        )
        layout.addWidget(open_button)

    def _set_role(self, role, enabled):
        self.main_window.set_image_role(
            self.series,
            self.image_id,
            role,
            enabled,
        )
        self.selection_changed.emit()

    def is_selected(self):
        return self.checkbox.isChecked()

    def is_cover(self):
        return self.cover_checkbox.isChecked()

    def is_donation(self):
        return self.donation_checkbox.isChecked()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Hi10 Cover & Donation Image Picker")
        self.setWindowIcon(QIcon("icon.ico"))

        # Fall back to icons beside the script when launched from another folder.
        if self.windowIcon().isNull():
            ico_path = APP_DIR / "icon.ico"
            if ico_path.is_file():
                self.setWindowIcon(QIcon(str(ico_path)))

        # macOS applications conventionally use .icns icons.
        if sys.platform == "darwin":
            icns_path = APP_DIR / "icon.icns"
            if icns_path.is_file():
                self.setWindowIcon(QIcon(str(icns_path)))

        self.resize(1050, 760)

        self.cards = []
        self.current_series = ""
        self.search_thread = None
        self.search_worker = None
        self.search_active = False
        self.download_active = False
        self.download_thread = None
        self.download_worker = None
        if sys.platform.startswith("linux") or sys.platform == "darwin":
            self.output_root = Path.home() / "Downloads"
        else:
            self.output_root = APP_DIR / "downloads"

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)

        controls = QGroupBox("Search parameters")
        controls_layout = QGridLayout(controls)

        controls_layout.addWidget(QLabel("Series:"), 0, 0)
        self.series_edit = QLineEdit()
        self.series_edit.setPlaceholderText("e.g. Fate/Grand Order")
        controls_layout.addWidget(self.series_edit, 0, 1)

        controls_layout.addWidget(QLabel("Optional tags:"), 1, 0)
        self.tag_edit = QLineEdit()
        self.tag_edit.setPlaceholderText("e.g. Saber")
        controls_layout.addWidget(self.tag_edit, 1, 1)

        controls_layout.addWidget(QLabel("Maximum results:"), 0, 2)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(1, MAX_IMAGES)
        self.limit_spin.setValue(50)
        controls_layout.addWidget(self.limit_spin, 0, 3)

        controls_layout.addWidget(QLabel("Output folder:"), 1, 2)
        self.output_edit = QLineEdit(str(self.output_root))
        controls_layout.addWidget(self.output_edit, 1, 3)

        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self.choose_folder)
        controls_layout.addWidget(browse_button, 1, 4)

        search_buttons = QHBoxLayout()

        self.search_button = QPushButton("Search and Preview")
        self.search_button.clicked.connect(self.start_search)
        search_buttons.addWidget(self.search_button)

        self.cancel_search_button = QPushButton("Cancel Search")
        self.cancel_search_button.setEnabled(False)
        self.cancel_search_button.clicked.connect(self.cancel_search)
        search_buttons.addWidget(self.cancel_search_button)

        controls_layout.addLayout(search_buttons, 0, 4)

        main_layout.addWidget(controls)

        toolbar = QHBoxLayout()

        self.select_all_button = QPushButton("Select all")
        self.select_all_button.clicked.connect(
            lambda: self.set_all_selected(True)
        )

        self.deselect_all_button = QPushButton("Deselect all")
        self.deselect_all_button.clicked.connect(
            lambda: self.set_all_selected(False)
        )

        self.selected_label = QLabel("Selected for download: 0")
        self.cover_count_label = QLabel("Cover images: 0")
        self.donation_count_label = QLabel("Donation images: 0")

        toolbar.addWidget(self.select_all_button)
        toolbar.addWidget(self.deselect_all_button)
        toolbar.addStretch(1)
        toolbar.addWidget(self.selected_label)
        toolbar.addWidget(self.cover_count_label)
        toolbar.addWidget(self.donation_count_label)

        main_layout.addLayout(toolbar)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)

        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setAlignment(
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
        )
        self.grid.setSpacing(12)

        self.scroll.setWidget(self.grid_host)
        main_layout.addWidget(self.scroll, 1)

        bottom = QHBoxLayout()

        self.status_label = QLabel(
            "Enter a series and click Search and Preview."
        )
        self.status_label.setWordWrap(True)
        bottom.addWidget(self.status_label, 1)

        self.progress = QProgressBar()
        self.progress.setMinimumWidth(180)
        self.progress.setValue(0)
        bottom.addWidget(self.progress)

        self.download_button = QPushButton("Download Selected")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.start_download)
        bottom.addWidget(self.download_button)

        main_layout.addLayout(bottom)
        self.load_settings()
        self.role_assignments = self.load_role_assignments()

        self.setStyleSheet("""
            QGroupBox { font-weight: bold; margin-top: 8px; }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 8px;
                padding: 0 4px;
            }
            QPushButton { padding: 6px 10px; }
        """)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self,
            "Choose download folder",
            self.output_edit.text() or ".",
        )

        if folder:
            self.output_edit.setText(folder)

    def clear_cards(self):
        while self.grid.count():
            item = self.grid.takeAt(0)
            widget = item.widget()

            if widget:
                widget.deleteLater()

        self.cards.clear()
        self.update_selection_count()

    def cancel_search(self):
        if not self.search_active or not self.search_worker:
            return

        self.search_worker.cancel()
        self.cancel_search_button.setEnabled(False)
        self.status_label.setText(
            "Cancelling search… waiting for the current request to finish."
        )

    def start_search(self):
        series = self.series_edit.text().strip()
        self.current_series = series
        tag = self.tag_edit.text().strip()

        if not series:
            QMessageBox.warning(
                self,
                "Missing series",
                "Enter a series/search term.",
            )
            return

        # The thread may already have finished and been deleted.
        if self.search_active:
            return

        self.search_active = True
        self.clear_cards()
        self.cancel_search_button.setEnabled(True)

        self.progress.setRange(0, 0)
        self.status_label.setText("Starting search…")
        self.search_button.setEnabled(False)
        self.download_button.setEnabled(False)

        self.search_thread = QThread(self)
        self.search_worker = SearchWorker(
            series,
            tag,
            self.limit_spin.value(),
        )

        self.search_worker.moveToThread(self.search_thread)
        self.search_thread.started.connect(self.search_worker.run)
        self.search_worker.result_found.connect(self.add_result)
        self.search_worker.status.connect(self.status_label.setText)
        self.search_worker.finished.connect(self.search_finished)
        self.search_worker.finished.connect(self.search_thread.quit)
        self.search_thread.finished.connect(
            self.search_worker.deleteLater
        )
        self.search_thread.finished.connect(
            self.search_thread.deleteLater
        )

        self.search_thread.start()

    def add_result(self, item):
        card = PreviewCard(
            item,
            main_window=self,
            series=self.current_series,
        )
        card.selection_changed.connect(self.update_selection_count)

        index = len(self.cards)
        columns = 4

        self.grid.addWidget(
            card,
            index // columns,
            index % columns,
        )

        self.cards.append(card)
        self.update_selection_count()

    def search_finished(self, count, error):
        cancelled = error == "CANCELLED"

        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.search_button.setEnabled(True)
        self.cancel_search_button.setEnabled(False)
        self.search_active = False

        if cancelled:
            self.status_label.setText(
                f"Search cancelled. Found {count} wallpaper(s) before cancellation."
            )

        elif error:
            self.status_label.setText(
                f"Search finished with an error: {error}"
            )

            if count == 0:
                QMessageBox.warning(self, "Search error", error)

        elif count:
            self.status_label.setText(
                f"Found {count} matching wallpaper(s). "
                "Review the thumbnails and select what to download."
            )

        else:
            self.status_label.setText("No matching wallpapers found.")

        self.update_selection_count()

    def set_all_selected(self, selected):
        for card in self.cards:
            card.checkbox.setChecked(selected)

        self.update_selection_count()

    def update_selection_count(self):
        cover_count = sum(card.is_cover() for card in self.cards)
        donation_count = sum(card.is_donation() for card in self.cards)

        download_count = sum(
            card.is_selected()
            or card.is_cover()
            or card.is_donation()
            for card in self.cards
        )

        self.selected_label.setText(
            f"Selected for download: {download_count}"
        )
        self.cover_count_label.setText(
            f"Cover images: {cover_count}"
        )
        self.donation_count_label.setText(
            f"Donation images: {donation_count}"
        )

        self.download_button.setEnabled(
            download_count > 0
            and not self.search_active
            and not self.download_active
        )

    def start_download(self):
        if self.download_active or self.search_active:
            return

        selected = []

        for card in self.cards:
            cover = card.is_cover()
            donation = card.is_donation()

            if not (card.is_selected() or cover or donation):
                continue

            item = dict(card.item)
            item["cover"] = cover
            item["donation"] = donation
            selected.append(item)

        if not selected:
            QMessageBox.information(
                self,
                "Nothing selected",
                "Select an image or assign it a role.",
            )
            return

        series = self.current_series or self.series_edit.text().strip()
        if not series:
            QMessageBox.warning(
                self,
                "Missing series",
                "Enter a series name before downloading.",
            )
            return

        output_root = Path(
            self.output_edit.text().strip() or "downloads"
        )
        folder = output_root / safe_name(series)

        self.download_button.setEnabled(False)
        self.search_button.setEnabled(False)
        self.progress.setRange(0, len(selected))
        self.progress.setValue(0)
        self.status_label.setText(
            f"Downloading {len(selected)} image(s)…"
        )

        self.download_active = True
        self.download_thread = QThread(self)
        self.download_worker = DownloadWorker(selected, folder)

        self.download_worker.moveToThread(self.download_thread)
        self.download_thread.started.connect(self.download_worker.run)
        self.download_worker.progress.connect(self.download_progress)
        self.download_worker.finished.connect(self.download_finished)
        self.download_worker.finished.connect(self.download_thread.quit)
        self.download_thread.finished.connect(
            self.download_worker.deleteLater
        )
        self.download_thread.finished.connect(
            self.download_thread.deleteLater
        )

        self.download_thread.start()

    def download_progress(self, current, total, message):
        self.progress.setRange(0, total)
        self.progress.setValue(current)
        self.status_label.setText(message)

    def download_finished(self, success, failed, folder):
        self.search_button.setEnabled(True)
        self.download_active = False

        self.progress.setRange(0, 100)
        self.progress.setValue(100 if success and not failed else 0)

        self.status_label.setText(
            f"Finished. Successful/already present: {success}; "
            f"failed: {failed}. Saved in: {folder}"
        )

        QMessageBox.information(
            self,
            "Download complete",
            f"Successful/already present: {success}\n"
            f"Failed: {failed}\n\n"
            f"Saved in:\n{folder}",
        )

        self.update_selection_count()

    def closeEvent(self, event):
        self.save_settings()
        super().closeEvent(event)

    def save_settings(self):
        """Save the current GUI values to a JSON settings file."""
        settings = {
            "series": self.series_edit.text(),
            "tags": self.tag_edit.text(),
            "max_results": self.limit_spin.value(),
            "output_folder": self.output_edit.text(),
        }

        try:
            SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps(settings, indent=4),
                encoding="utf-8",
            )
        except OSError as exc:
            print(f"Could not save settings: {exc}")

    def load_settings(self):
        """Restore GUI values from the JSON settings file."""
        if not SETTINGS_FILE.is_file():
            return

        try:
            settings = json.loads(
                SETTINGS_FILE.read_text(encoding="utf-8")
            )

            self.series_edit.setText(settings.get("series", ""))
            self.tag_edit.setText(settings.get("tags", ""))
            self.limit_spin.setValue(
                settings.get("max_results", MAX_IMAGES)
            )
            self.output_edit.setText(
                settings.get("output_folder", str(self.output_root))
            )

        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            print(f"Could not load settings: {exc}")

    def load_role_assignments(self):
        try:
            if ROLE_ASSIGNMENTS_FILE.exists():
                with ROLE_ASSIGNMENTS_FILE.open("r", encoding="utf-8") as file:
                    data = json.load(file)
                    return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def save_role_assignments(self):
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)

        temp_file = ROLE_ASSIGNMENTS_FILE.with_suffix(".tmp")
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(self.role_assignments, file, indent=2, ensure_ascii=False)

        temp_file.replace(ROLE_ASSIGNMENTS_FILE)

    def get_image_roles(self, series, image_id):
        series_roles = self.role_assignments.get(series, {})
        return series_roles.get(str(image_id), {})

    def set_image_role(self, series, image_id, role, enabled):
        series = series.strip()
        image_id = str(image_id)

        self.role_assignments.setdefault(series, {})
        roles = self.role_assignments[series].setdefault(
            image_id,
            {"cover": False, "donation": False},
        )

        roles[role] = bool(enabled)

        # Remove unused assignments to keep the file tidy.
        if not roles.get("cover") and not roles.get("donation"):
            del self.role_assignments[series][image_id]

        if not self.role_assignments[series]:
            del self.role_assignments[series]

        self.save_role_assignments()

def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())
