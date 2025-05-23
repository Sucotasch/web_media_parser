#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Webpage parser class for extracting media files and links from webpages
"""

import re
import os
import time
import json
import asyncio
import logging
import platform
import threading
import mimetypes
from typing import Set, List, Tuple, Dict, Any, Optional, Union
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor

# Import for content type detection
import filetype

# Try to import brotli for content-encoding support
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

# Import utility functions
from src.parser.utils import is_image_url, is_media_url, is_valid_url

# Import site pattern manager
from src.parser.site_pattern_manager import SitePatternManager


def get_mime_type(filename=None, buffer=None):
    """Get MIME type using filetype and mimetypes"""
    try:
        if filename:
            # Try filetype first
            kind = filetype.guess(filename)
            if kind:
                return kind.mime
            # Fallback to mimetypes
            mime_type, _ = mimetypes.guess_type(filename)
            return mime_type or "application/octet-stream"
        elif buffer:
            kind = filetype.guess(buffer)
            return kind.mime if kind else "application/octet-stream"
    except Exception:
        return "application/octet-stream"


import aiohttp
import requests
import chardet
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from requests_html import HTMLSession

# Ensure we have consistent brotli support detection
if not globals().get('HAS_BROTLI', False):
    try:
        from src.fix_brotli import BrotliSupportFix
        # Just importing should be enough for aiohttp to use brotli if available
        HAS_BROTLI = BrotliSupportFix.patch()
    except ImportError:
        # Fallback to our previous check if the fix module is not available
        HAS_BROTLI = False

from src.parser.utils import get_domain, is_media_url, is_same_domain, is_image_url

# Configure logging
logger = logging.getLogger(__name__)


class WebpageParser:
    """
    Enhanced webpage parser class for extracting media files and links from webpages
    """

    # Known CDN patterns for media content
    CDN_PATTERNS = {
        "img": [
            r"\.cloudfront\.net",
            r"\.akamaized\.net",
            r"\.cloudinary\.com",
            r"\.fastly\.net",
            r"\.imgix\.net",
            r"\.cdn\.",
            r"images?[0-9]*\.",
            r"cdn[0-9]*\.",
            r"static\.",
            r"media\.",
        ],
        "video": [
            r"\.brightcove\.net",
            r"\.jwplatform\.com",
            r"\.vimeocdn\.com",
            r"\.ytimg\.com",
            r"\.streamable\.com",
            r"video\.",
            r"videos?\.",
            r"media\.",
        ],
    }

    # Enhanced video platform patterns with improved coverage
    VIDEO_PLATFORMS = {
        "youtube": [r"youtube\.com", r"youtu\.be", r"youtube-nocookie\.com"],
        "vimeo": [r"vimeo\.com", r"player\.vimeo\.com", r"vimeocdn\.com"],
        "dailymotion": [r"dailymotion\.com", r"dai\.ly", r"dm-static\.com"],
        "twitch": [r"twitch\.tv", r"ttvnw\.net", r"jtvnw\.net"],
        "facebook": [r"facebook\.com/watch", r"facebook\.com/video", r"fbcdn\.net", r"fb\.watch"],
        "instagram": [r"instagram\.com/tv", r"instagram\.com/reel", r"instagram\.com/p", r"cdninstagram\.com"],
        "tiktok": [r"tiktok\.com", r"musical\.ly", r"tiktokcdn\.com"],
        "vk": [r"vk\.com/video", r"vk\.ru/video"],
        "reddit": [r"reddit\.com/r/.*/video/", r"v\.redd\.it"],
        "twitter": [r"twitter\.com/.*/status/", r"t\.co", r"twimg\.com", r"pbs\.twimg\.com"],
        "redgifs": [r"redgifs\.com", r"gifdeliverynetwork\.com"],
        "bilibili": [r"bilibili\.com", r"bilivideo\.com", r"b23\.tv"],
        "streamable": [r"streamable\.com"],
        "imgur": [r"imgur\.com/a", r"imgur\.com/gallery", r"imgur\.com/\w+\.gifv", r"imgur\.com/\w+\.mp4"],
        "gfycat": [r"gfycat\.com"],
        "soundcloud": [r"soundcloud\.com"],
        "xvideos": [r"xvideos\.com"],
        "xhamster": [r"xhamster\.com"],
        "pornhub": [r"pornhub\.com"],
        "youporn": [r"youporn\.com"],
    }

    # Lazy loading and dynamic content patterns
    LAZY_LOAD_PATTERNS = {
        "data-attributes": [
            "data-src",
            "data-original",
            "data-lazy",
            "data-load",
            "data-source",
            "data-srcset",
            "data-bg",
            "data-poster",
            "data-image",
            "data-original-src",
        ],
        "class-patterns": [
            r"lazy",
            r"lazyload",
            r"b-lazy",
            r"delayed",
            r"deferred",
            r"preload",
            r"progressive",
        ],
        "placeholder-patterns": [
            r"placeholder",
            r"blur-up",
            r"lqip",  # Low Quality Image Placeholder
            r"loading",
        ],
    }

    # Modern web application patterns
    DYNAMIC_PATTERNS = {
        "infinite-scroll": [
            r"infinite[_-]?scroll",
            r"load[_-]?more",
            r"next[_-]?page",
            r"pagination",
        ],
        "ajax-load": [
            r"ajax[_-]?load",
            r"dynamic[_-]?load",
            r"async[_-]?load",
            r"on[_-]?demand",
        ],
        "content-placeholders": [
            r"content[_-]?placeholder",
            r"skeleton[_-]?loader",
            r"loading[_-]?placeholder",
        ],
    }

    # Enhanced media sources
    MEDIA_SOURCES = {
        "img": [
            ("src", "string"),
            ("srcset", "srcset"),
            ("data-src", "string"),
            ("data-srcset", "srcset"),
            ("data-original", "string"),
            ("style", "background"),
        ],
        "video": [
            ("src", "string"),
            ("data-src", "string"),
            ("poster", "string"),
            ("data-poster", "string"),
        ],
        "source": [
            ("src", "string"),
            ("srcset", "srcset"),
            ("data-src", "string"),
            ("data-srcset", "srcset"),
        ],
        "picture": [
            ("source", "nested"),
        ],
    }

    # Enhanced patterns for static JavaScript analysis
    JS_PATTERNS = {
        "image_sources": [
            r'["\'](https?://[^"\']+\.(?:jpg|jpeg|png|gif|webp))["\']',
            r'\.src\s*=\s*["\'](https?://[^"\']+)["\']',
            r'loadImage\s*\(\s*["\'](https?://[^"\']+)["\']',
            r'background(?:-image)?\s*:\s*url\(["\']?(https?://[^"\']+)["\']?\)',
        ],
        "video_sources": [
            r'["\'](https?://[^"\']+\.(?:mp4|webm|ogg))["\']',
            r'\.src\s*=\s*["\'](https?://[^"\']+\.(?:mp4|webm|ogg))["\']',
            r'loadVideo\s*\(\s*["\'](https?://[^"\']+)["\']',
        ],
        "data_attributes": [
            r'data-(?:src|original|lazy|load|image|video|poster|bg|background|url)\s*=\s*["\'](https?://[^"\']+)["\']',
            r'data-srcset\s*=\s*["\'](https?://[^"\']+(?:\s+\d+[wx])?(?:,\s*https?://[^"\']+(?:\s+\d+[wx])?)*)["\']',
        ],
        "framework_patterns": {
            "react": r'className\s*=\s*["\'](lazy-load|image-loader)["\']',
            "vue": r'v-lazy\s*=\s*["\'](https?://[^"\']+)["\']',
            "angular": r'\[lazyLoad\]\s*=\s*["\'](https?://[^"\']+)["\']',
        },
    }

    def __init__(
        self,
        url: str,
        settings: Dict[str, Any],
        process_js: bool = True,
        process_dynamic: bool = True,
        external_session = None,
        pattern_manager = None,
    ):
        """Initialize the parser with enhanced settings"""
        self.url = url
        self.settings = settings
        self.process_js = process_js
        self.process_dynamic = process_dynamic
        self.domain = get_domain(url)

        # Timeout settings
        self.page_timeout = settings.get("page_timeout", 60)
        self.request_timeout = aiohttp.ClientTimeout(
            total=self.page_timeout, connect=20, sock_read=self.page_timeout
        )

        # Initialize sessions
        self.sync_session = self._create_session()
        self.external_session = external_session
        
        # Initialize pattern manager
        self.pattern_manager = pattern_manager
        
        # Initialize results
        self.links = {}  # Dict[str, Dict[str, Any]] - URL to context mapping
        self.media_files: List[Tuple[str, str, Dict[str, Any]]] = []
        self._mime_type = None  # Lazy initialization

    def get_discovered_urls(self) -> Dict[str, Dict[str, Any]]:
        """
        Get the dictionary of discovered URLs and their context information

        Returns:
            Dictionary with URLs as keys and context dictionaries as values
        """
        return self.links

    async def __aenter__(self):
        """Context manager entry that creates the aiohttp session if not provided externally"""
        # Use external session if provided, otherwise create a new one
        if self.external_session is not None:
            self.session = self.external_session
            self.owns_session = False
        else:
            # Define compression options based on available libraries
            if HAS_BROTLI:
                from aiohttp import ClientSession, TCPConnector
                self.session = ClientSession(
                    timeout=self.request_timeout,
                    headers=self._get_headers(),
                    connector=TCPConnector(ssl=False)
                )
            else:
                self.session = aiohttp.ClientSession(
                    timeout=self.request_timeout,
                    headers=self._get_headers()
                )
            self.owns_session = True
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit that ensures the session is closed only if we own it"""
        if hasattr(self, "session") and hasattr(self, "owns_session") and self.owns_session:
            await self.session.close()

    def _get_headers(self) -> Dict[str, str]:
        """Get request headers with enhanced browser simulation"""
        return {
            "User-Agent": self.settings.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            ),
            "Accept-Language": self.settings.get("accept_language", "en-US,en;q=0.9"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Cache-Control": "max-age=0",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",
        }

    def _create_session(self) -> requests.Session:
        """Create an enhanced session with retry mechanism and custom headers"""
        session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=self.settings.get("retry_count", 3),
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
            respect_retry_after_header=True,
        )

        # Set up connection pooling
        adapter = HTTPAdapter(
            max_retries=retry_strategy, pool_connections=10, pool_maxsize=10
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Set up headers
        headers = {
            "User-Agent": self.settings.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            ),
            "Accept-Language": self.settings.get("accept_language", "en-US,en;q=0.9"),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Encoding": "gzip, deflate, br",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1",
        }

        # Set referrer policy
        referrer_policy = self.settings.get("referrer", "auto")
        if referrer_policy == "origin":
            headers["Referer"] = get_domain(self.url)
        elif referrer_policy == "auto":
            headers["Referer"] = self.url

        session.headers.update(headers)
        return session

    async def _get_content(self) -> Optional[str]:
        """Get webpage content with proper encoding detection and bypass mechanisms"""
        try:
            headers = self._get_headers()
            # Add referer if enabled
            referrer_policy = self.settings.get("referrer", "auto")
            if referrer_policy == "origin":
                headers["Referer"] = get_domain(self.url)
            elif referrer_policy == "auto":
                headers["Referer"] = self.url
            
            # Add common cookie consent cookies if bypass is enabled
            cookies = {}
            if self.settings.get("bypass_cookie_consent", True):
                consent_cookies = {
                    'cookieconsent_status': 'dismiss',
                    'gdpr_accepted': 'true',
                    'cookies_accepted': 'true',
                    'euconsent': 'true',
                    'CookieConsent': 'true',
                    'cc_cookie_accept': '1',
                    'cookie_consent': 'true',
                    'privacy_policy_accepted': 'true'
                }
                cookies.update(consent_cookies)
                logger.debug(f"Added cookie consent bypass cookies for: {self.url}")
                
            # Don't manually set Accept-Encoding, let aiohttp handle it based on available libraries
            # It will automatically use brotli if available

            async with self.session.get(
                self.url, headers=headers, cookies=cookies
            ) as response:
                if response.status != 200:
                    logger.error(f"Failed to fetch {self.url}: {response.status}")
                    return None

                # aiohttp should automatically handle content decompression, including brotli if the library is available
                try:
                    content = await response.read()
                    logger.debug(f"Successfully read content from {self.url}, content-encoding: {response.headers.get('Content-Encoding', 'none')}")
                except Exception as e:
                    logger.error(f"Failed to read content from {self.url}: {str(e)}")
                    return None
                
                encoding = await self._detect_encoding(content)
                decoded_content = ""
                
                try:
                    decoded_content = content.decode(encoding, errors="replace")
                except (UnicodeDecodeError, LookupError) as e:
                    logger.warning(
                        f"Failed to decode with {encoding}, falling back to utf-8: {str(e)}"
                    )
                    decoded_content = content.decode("utf-8", errors="replace")
                
                # Check for JS redirects if bypass is enabled
                if self.settings.get("bypass_js_redirects", True):
                    redirect_url = self._extract_js_redirect(decoded_content)
                    if redirect_url:
                        logger.info(f"Detected JS redirect from {self.url} to {redirect_url}")
                        
                        # Handle relative URLs
                        if not redirect_url.startswith(('http://', 'https://')):
                            redirect_url = urljoin(self.url, redirect_url)
                            
                        # Follow redirect with same headers and cookies
                        self.url = redirect_url  # Update URL for future references
                        logger.info(f"Following JS redirect to: {redirect_url}")
                        return await self._get_content()  # Recursively follow
                
                return decoded_content

        except aiohttp.ClientError as e:
            logger.error(f"Network error fetching {self.url}: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Error fetching {self.url}: {str(e)}")
            return None

    async def _detect_encoding(self, content: bytes) -> str:
        """Detect content encoding using chardet"""
        # First check if content starts with BOM
        if content.startswith(b"\xef\xbb\xbf"):
            return "utf-8-sig"
        elif content.startswith(b"\xff\xfe") or content.startswith(b"\xfe\xff"):
            return "utf-16"

        # Use chardet for detection
        result = chardet.detect(content)
        encoding = result["encoding"] if result and result["encoding"] else "utf-8"
        confidence = result["confidence"] if result else 0

        logger.debug(f"Detected encoding: {encoding} (confidence: {confidence})")
        return encoding
        
    def _extract_js_redirect(self, content: str) -> Optional[str]:
        """Extract JavaScript redirects from content"""
        if not content:
            return None
            
        # Common redirect patterns
        patterns = [
            # window.location patterns
            r'window\.location(?:\.href)? *= *["\'](https?://[^"\']+)["\']\'',
            r'window\.location\.replace\\?\(["\'](https?://[^"\']+)["\'](\\?\))',
            # document.location patterns
            r'document\.location(?:\.href)? *= *["\'](https?://[^"\']+)["\']\'',
            # Relative URL paths
            r'window\.location(?:\.href)? *= *["\'](/[^"\']+)["\']\'',
            r'document\.location(?:\.href)? *= *["\'](/[^"\']+)["\']\'',
            # Meta refresh
            r'<meta[^>]*?http-equiv=["\']?refresh["\']?[^>]*?content=["\']?\d+;\s*url=([^\s"\'>]+)["\']?',
        ]
        
        for pattern in patterns:
            try:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    if isinstance(matches[0], tuple):
                        # Some regex patterns might return tuples
                        return matches[0][0]  
                    else:
                        return matches[0]
            except Exception as e:
                logger.debug(f"Error in regex pattern: {str(e)}")
                continue
        
        return None

    def _is_cdn_url(self, url: str, media_type: str) -> bool:
        """Check if URL matches known CDN patterns"""
        patterns = self.CDN_PATTERNS.get(media_type, [])
        return any(re.search(pattern, url, re.IGNORECASE) for pattern in patterns)

    def _get_video_platform(self, url: str) -> Optional[str]:
        """Enhanced detection of video platform from URL"""
        parsed_url = urlparse(url.lower())
        domain = parsed_url.netloc
        path = parsed_url.path
        
        # Check for direct video file extensions
        video_extensions = [".mp4", ".webm", ".avi", ".mov", ".flv", ".mkv", ".wmv", ".ts"]
        if any(ext in path for ext in video_extensions):
            logger.debug(f"Direct video file detected in URL: {url}")
            return "direct-video"

        # Check for known video platforms
        for platform, patterns in self.VIDEO_PLATFORMS.items():
            if any(re.search(pattern, domain) for pattern in patterns):
                return platform
                
        return None

    def _get_best_image_url(self, element: Any) -> Tuple[Optional[str], Dict[str, Any]]:
        """
        Extract best quality image URL and attributes from element
        Returns tuple of (url, attributes)
        Enhanced with better quality detection based on Java implementation
        If pattern_manager is available, applies transformation patterns
        """
        attributes = {}
        candidates = []

        # Collect all possible sources with improved attribute detection
        sources = {
            "src": element.get("src", ""),
            "data-src": element.get("data-src", ""),
            "data-original": element.get("data-original", ""),
            "data-lazy": element.get("data-lazy", ""),
            "data-lazy-src": element.get("data-lazy-src", ""),
            "data-original-src": element.get("data-original-src", ""),
            "data-hi-res-src": element.get("data-hi-res-src", ""),
            "data-high-res": element.get("data-high-res", ""),
            "data-hires": element.get("data-hires", ""),
            "data-retina": element.get("data-retina", ""),
            "data-full": element.get("data-full", ""),
            "data-fullsize": element.get("data-fullsize", ""),
            "data-fullsizeurl": element.get("data-fullsizeurl", ""),
            "data-max-res": element.get("data-max-res", ""),
            "data-maxres": element.get("data-maxres", ""),
        }

        # Get srcset attributes
        srcset = element.get("srcset", "")
        data_srcset = element.get("data-srcset", "")
        data_lazy_srcset = element.get("data-lazy-srcset", "")
        data_srcset_sizes = [element.get("sizes", ""), element.get("data-sizes", "")]

        logger.debug(f"Found element with attributes: {element.attrs}")

        # Process each source
        for attr_name, url in sources.items():
            if url:
                # Try to determine if this is a high-quality source based on attribute name
                priority = 0
                if any(hint in attr_name.lower() for hint in ["hi-res", "high", "retina", "full", "original", "max"]):
                    priority = 100  # Give high priority to attributes suggesting high quality
                candidates.append({"url": url, "width": priority, "source": attr_name})

        # Process srcset attributes with improved srcset parsing
        for srcset_attr in [srcset, data_srcset, data_lazy_srcset]:
            if srcset_attr:
                candidates.extend(self._parse_srcset(srcset_attr))

        # Look for image URLs in other data attributes
        for attr_name, value in element.attrs.items():
            if isinstance(value, str):
                # Check for image file extensions in any attribute
                if re.search(r"\.(jpg|jpeg|png|webp|gif|avif|tiff|bmp)", value.lower()):
                    # Check if attribute name suggests quality
                    priority = 0
                    if any(hint in attr_name.lower() for hint in ["hi-res", "high", "retina", "full", "original", "max"]):
                        priority = 100
                    candidates.append({"url": value, "width": priority, "source": attr_name})

        # Get image dimensions if available
        width = element.get("width", "")
        height = element.get("height", "")
        if width and height:
            try:
                width_val = int(width) if str(width).isdigit() else 0
                height_val = int(height) if str(height).isdigit() else 0
                attributes["dimensions"] = {"width": width_val, "height": height_val}
                
                # Add size prioritization: larger images get higher priority
                for candidate in candidates:
                    if candidate["width"] == 0:  # Only update if not already prioritized
                        # Prioritize based on actual dimensions, using high-quality threshold (800px or user setting)
                        high_quality_threshold = max(800, self.settings.get("min_image_width", 100) * 2)
                        if width_val > high_quality_threshold or height_val > high_quality_threshold:
                            candidate["width"] = max(width_val, height_val)  # Use largest dimension for sorting
            except (ValueError, TypeError):
                pass

        # Get alt text and title
        attributes["alt"] = element.get("alt", "")
        attributes["title"] = element.get("title", "")

        # Enhanced candidate filtering and sorting based on user settings
        # Get minimum width/height from settings or use defaults
        min_width = self.settings.get("min_image_width", 100)
        min_height = self.settings.get("min_image_height", 100)
        
        # Filter out tiny images (likely icons or placeholders) based on user settings
        substantial_candidates = [
            c for c in candidates if 
            # If width is already set, check against min_width
            (c["width"] >= min_width and c["width"] > 0) or 
            # Also filter by height if available in attributes
            ("dimensions" in attributes and attributes["dimensions"].get("height", 0) >= min_height) or
            # Keep candidates with unknown dimensions (width==0)
            c["width"] == 0
        ]
        
        # If we have substantial candidates, use only those
        filtered_candidates = substantial_candidates if substantial_candidates else candidates
        
        # Sort candidates by image width/quality (higher is better)
        filtered_candidates.sort(key=lambda x: x["width"], reverse=True)

        # Return best candidate if found
        if filtered_candidates:
            best_candidate = filtered_candidates[0]
            url = best_candidate["url"]
            attributes["source"] = best_candidate["source"]
            attributes["original_width"] = best_candidate["width"]
            attributes["all_candidates"] = [c["url"] for c in filtered_candidates[:3]]  # Keep top 3 alternatives
            
            # Apply pattern transformation if pattern manager is available
            if self.pattern_manager and url:
                transformed_url = self.pattern_manager.transform_image_url(url, self.url)
                if transformed_url != url:
                    # Store original URL in attributes
                    attributes["original_url"] = url
                    attributes["transformed"] = True
                    url = transformed_url
                    logger.debug(f"Transformed image URL: {attributes['original_url']} -> {url}")
            
            return url, attributes

        return None, attributes

    def _parse_srcset(self, srcset: str) -> List[Dict[str, Any]]:
        """
        Parse srcset attribute into list of URLs with widths
        Returns list of dicts with 'url' and 'width' keys
        """
        candidates = []

        # Split srcset into individual source descriptors
        for src_item in srcset.split(","):
            src_item = src_item.strip()
            if not src_item:
                continue

            # Split into URL and descriptor
            parts = src_item.split()
            if not parts:
                continue

            url = parts[0]
            width = 0

            # Parse descriptor if present
            if len(parts) > 1:
                descriptor = parts[1]
                # Width descriptor (e.g., "800w")
                if descriptor.endswith("w"):
                    try:
                        width = int(descriptor[:-1])
                    except ValueError:
                        pass
                # Density descriptor (e.g., "2x")
                elif descriptor.endswith("x"):
                    try:
                        density = float(descriptor[:-1])
                        width = int(density * 1000)  # Use density as relative width
                    except ValueError:
                        pass

            candidates.append({"url": url, "width": width, "source": "srcset"})

        return candidates

    def _extract_inline_css_images(self, element: Any) -> List[str]:
        """Extract image URLs from inline CSS styles"""
        images = []
        style = element.get("style", "")

        if style:
            # Find all url() expressions
            urls = re.findall(r'url\(["\']?([^)"\']+)["\']?\)', style)
            for url in urls:
                if re.search(r"\.(jpg|jpeg|png|webp|gif|avif)", url.lower()):
                    images.append(url)

            # Find background-image properties
            bg_images = re.findall(
                r'background-image:\s*url\(["\']?([^)"\']+)["\']?\)', style
            )
            images.extend(bg_images)

        return images

    def _extract_picture_sources(self, picture_elem: Any) -> List[Dict[str, Any]]:
        """Extract image sources from picture element"""
        sources = []

        # Process source elements
        for source in picture_elem.find_all("source"):
            srcset = source.get("srcset", "")
            if srcset:
                candidates = self._parse_srcset(srcset)
                media = source.get("media", "")
                type_ = source.get("type", "")

                for candidate in candidates:
                    candidate["media"] = media
                    candidate["type"] = type_
                    sources.append(candidate)

        # Process img element if present
        img = picture_elem.find("img")
        if img:
            url, attrs = self._get_best_image_url(img)
            if url:
                sources.append(
                    {
                        "url": url,
                        "width": attrs.get("original_width", 0),
                        "source": "img",
                        "media": "",
                        "type": "",
                    }
                )

        return sources

    async def _extract_images(self, soup: BeautifulSoup) -> None:
        """Extract images with enhanced detection capabilities"""
        found = 0

        # Process picture elements first
        for picture in soup.find_all("picture"):
            sources = self._extract_picture_sources(picture)
            for source in sources:
                url = source["url"]
                if not url:
                    continue

                abs_url = urljoin(self.url, url)
                if not abs_url.startswith(("http://", "https://")):
                    continue

                attrs = {
                    "width": source["width"],
                    "media": source["media"],
                    "type": source["type"],
                    "source": source["source"],
                }

                if self._is_cdn_url(abs_url, "img"):
                    attrs["is_cdn"] = True

                self.media_files.append(("image", abs_url, attrs))
                found += 1
                logger.debug(f"Added image from picture element: {abs_url}")

        # Process regular img tags and find images wrapped in links
        for img in soup.find_all("img"):
            url, attrs = self._get_best_image_url(img)
            if not url:
                continue

            abs_url = urljoin(self.url, url)
            if not abs_url.startswith(("http://", "https://")):
                continue

            if self._is_cdn_url(abs_url, "img"):
                attrs["is_cdn"] = True

            self.media_files.append(("image", abs_url, attrs))
            found += 1
            logger.debug(f"Added image from img tag: {abs_url}")
            
            # Check if image is wrapped in a link to a potentially full-sized version
            parent_a = img.find_parent('a', href=True)
            if parent_a and parent_a.get('href'):
                link_url = parent_a.get('href')
                link_abs_url = urljoin(self.url, link_url)
                
                # Skip if already an absolute URL or not starting with http/https
                if not link_abs_url.startswith(("http://", "https://")):
                    continue
                    
                # Check if the link URL path contains parts from the image URL path (common for fullsize versions)
                img_path = urlparse(abs_url).path
                link_path = urlparse(link_abs_url).path
                
                # Check if it's a direct image file link
                if is_image_url(link_abs_url):
                    # It's directly an image file
                    link_attrs = attrs.copy()
                    link_attrs['source'] = 'parent-link'
                    self.media_files.append(("image", link_abs_url, link_attrs))
                    found += 1
                    logger.debug(f"Added linked image from parent link: {link_abs_url}")
                # Check if it looks like a fullsize version (common patterns)
                # But ONLY if it's not an HTML page or other webpage format
                # If it looks like a fullsize image URL
                elif (is_media_url(link_abs_url) or \
                     'full' in link_abs_url or 'large' in link_abs_url or 'original' in link_abs_url or \
                     any(ext in link_abs_url for ext in ('fullpic', 'fullsize', 'bigpic', 'bigsize', 'hi-res')) or \
                     (img_path and link_path and img_path.split('/')[-1].split('-')[-1] in link_path.split('/')[-1]) or \
                     # Special pattern for joyreactor: /pics/post/ to /pics/post/full/
                     ('/pics/post/' in img_path and '/pics/post/full/' in link_path)) \
                     and not link_abs_url.lower().endswith(('.html', '.htm', '.php', '.asp', '.aspx', '.jsp')):
                    # Looks like a link to a fullsize version
                    link_attrs = attrs.copy()
                    link_attrs['source'] = 'fullsize-link'
                    self.media_files.append(("image", link_abs_url, link_attrs))
                    found += 1
                    logger.debug(f"Added potential fullsize image: {link_abs_url}")
                else:
                    # Check for common patterns in image links
                    has_image_parts = any(part in link_abs_url.lower() for part in ['image', 'img', 'photo', 'pic', 'gallery', 'album'])
                    has_image_path = any(part in link_path.lower() for part in ['image', 'img', 'photo', 'pic', 'gallery', 'album'])
                    similar_base = False
                    
                    # Check if the image URL and link share a similar base
                    if img_path and link_path:
                        img_parts = img_path.split('/')
                        link_parts = link_path.split('/')
                        # Check if they share at least 2 path components
                        if len(img_parts) >= 2 and len(link_parts) >= 2:
                            similar_base = img_parts[:-1] == link_parts[:-1]
                    
                    # Mark this as a link found in an image context (for prioritization)
                    link_context = {
                        'from_image': True, 
                        'thumbnail_url': abs_url,
                        'is_webpage': True,  # Flag as a webpage that should be parsed, not downloaded
                        'potential_media_container': True,  # This URL likely contains more media
                        'media_context': {'source_image': abs_url}  # Store image reference
                    }
                    
                    # Check if it's likely a HTML page with a gallery or image viewer
                    is_likely_gallery = link_abs_url.lower().endswith(('.html', '.htm', '.php')) or \
                                       any(part in link_abs_url.lower() for part in ['gallery', 'viewer', 'album', 'slideshow'])
                    
                    if has_image_parts or has_image_path or similar_base or is_likely_gallery:
                        # This link is likely image-related, add with high priority
                        link_context['priority'] = 15.0  # Even higher priority for likely galleries
                        link_context['likely_gallery'] = True
                        self.links[link_abs_url] = link_context
                        logger.debug(f"Added potential image gallery link to discovered URLs: {link_abs_url}")
                    else:
                        # Regular link from image, still add with image context
                        self.links[link_abs_url] = link_context
                        logger.debug(f"Added image link to discovered URLs: {link_abs_url}")

        # Process CSS background images
        for elem in soup.find_all(attrs={"style": True}):
            for url in self._extract_inline_css_images(elem):
                abs_url = urljoin(self.url, url)
                if not abs_url.startswith(("http://", "https://")):
                    continue

                attrs = {"source": "css", "element": elem.name}

                if self._is_cdn_url(abs_url, "img"):
                    attrs["is_cdn"] = True

                self.media_files.append(("image", abs_url, attrs))
                found += 1
                logger.debug(f"Added image from CSS: {abs_url}")

        # Process link tags (e.g., favicons, apple-touch-icons)
        for link in soup.find_all("link", rel=re.compile(r"icon|apple-touch-icon")):
            href = link.get("href")
            if not href:
                continue

            abs_url = urljoin(self.url, href)
            if not abs_url.startswith(("http://", "https://")):
                continue

            attrs = {
                "rel": link.get("rel", []),
                "sizes": link.get("sizes", ""),
                "type": link.get("type", ""),
            }

            self.media_files.append(("image", abs_url, attrs))
            found += 1
            logger.debug(f"Added icon image: {abs_url}")

        # Process meta tags (e.g., og:image)
        for meta in soup.find_all(
            "meta", property=re.compile(r"og:image|twitter:image")
        ):
            content = meta.get("content")
            if not content:
                continue

            abs_url = urljoin(self.url, content)
            if not abs_url.startswith(("http://", "https://")):
                continue

            attrs = {"property": meta.get("property", ""), "source": "meta"}

            if self._is_cdn_url(abs_url, "img"):
                attrs["is_cdn"] = True

            self.media_files.append(("image", abs_url, attrs))
            found += 1
            logger.debug(f"Added meta image: {abs_url}")

        logger.info(f"Found {found} images on {self.url}")

    async def _extract_videos(self, soup: BeautifulSoup) -> None:
        """Extract videos with enhanced detection capabilities"""
        found = 0

        # Process video elements
        for video in soup.find_all("video"):
            # Get video sources
            sources = []

            # Direct source from video tag
            src = video.get("src")
            if src:
                sources.append({"url": src, "type": video.get("type", "")})

            # Sources from source tags
            for source in video.find_all("source"):
                src = source.get("src")
                if src:
                    sources.append({"url": src, "type": source.get("type", "")})
                            
            # Check for video download links near video elements
            parent = video.parent
            for a_tag in parent.find_all('a', href=True):
                href = a_tag.get('href', '')
                # Check if link likely points to a video file
                if any(ext in href.lower() for ext in [".mp4", ".webm", ".avi", ".mov", ".flv"]):
                    sources.append({"url": href, "type": "video/mp4"})

            # Process each source
            for source in sources:
                url = source["url"]
                abs_url = urljoin(self.url, url)
                if not abs_url.startswith(("http://", "https://")):
                    continue

                attrs = {
                    "width": video.get("width", ""),
                    "height": video.get("height", ""),
                    "poster": video.get("poster", ""),
                    "type": source["type"],
                    "controls": video.get("controls") is not None,
                    "autoplay": video.get("autoplay") is not None,
                }

                if self._is_cdn_url(abs_url, "video"):
                    attrs["is_cdn"] = True

                self.media_files.append(("video", abs_url, attrs))
                found += 1
                logger.debug(f"Added video: {abs_url}")

        # Process iframes for embedded videos
        for iframe in soup.find_all("iframe"):
            src = iframe.get("src", "")
            if not src:
                continue

            abs_url = urljoin(self.url, src)
            if not abs_url.startswith(("http://", "https://")):
                continue
                    
            # Also check for iframe data-src which might contain actual video URL
            data_src = iframe.get("data-src", "")
            if data_src and data_src.startswith(("http://", "https://")):
                # Add the data-src URL as a potential video source
                data_abs_url = urljoin(self.url, data_src)
                # Check if it's a video platform
                data_platform = self._get_video_platform(data_abs_url)
                if data_platform:
                    attrs = {
                        "width": iframe.get("width", ""),
                        "height": iframe.get("height", ""),
                        "platform": data_platform,
                        "source": "iframe-data-src"
                    }
                    self.media_files.append(("video", data_abs_url, attrs))
                    found += 1
                    logger.debug(f"Added video from iframe data-src: {data_abs_url}")

            # Check if it's a known video platform
            platform = self._get_video_platform(abs_url)
            if platform:
                attrs = {
                    "width": iframe.get("width", ""),
                    "height": iframe.get("height", ""),
                    "platform": platform,
                    "title": iframe.get("title", ""),
                    "allow": iframe.get("allow", ""),
                    "type": "embed",
                }

                self.media_files.append(("video", abs_url, attrs))
                found += 1
                logger.debug(f"Added embedded {platform} video: {abs_url}")

        # Process meta tags (og:video)
        for meta in soup.find_all(
            "meta", property=re.compile(r"og:video|twitter:player")
        ):
            content = meta.get("content")
            if not content:
                continue

            abs_url = urljoin(self.url, content)
            if not abs_url.startswith(("http://", "https://")):
                continue

            attrs = {"property": meta.get("property", ""), "source": "meta"}

            # Check if it's a known video platform
            platform = self._get_video_platform(abs_url)
            if platform:
                attrs["platform"] = platform

            self.media_files.append(("video", abs_url, attrs))
            found += 1
            logger.debug(f"Added meta video: {abs_url}")

        logger.info(f"Found {found} videos on {self.url}")

    async def _extract_links(self, soup: BeautifulSoup) -> None:
        """Extract links from BeautifulSoup object"""
        found = 0
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("javascript:", "#")):
                continue

            abs_url = urljoin(self.url, href)
            if not abs_url.startswith(("http://", "https://")):
                continue

            # Add regular link with context about its type
            link_context = {'from_image': False, 'element': 'a'}
            self.links[abs_url] = link_context
            found += 1
            logger.debug(f"Extracted link: {abs_url}")

        # Also check for canonical URLs
        canonical = soup.find("link", rel="canonical", href=True)
        if canonical:
            href = canonical["href"].strip()
            abs_url = urljoin(self.url, href)
            if abs_url.startswith(("http://", "https://")):
                # Add canonical link with higher priority context
                link_context = {'from_image': False, 'element': 'canonical', 'priority': 2.0}
                self.links[abs_url] = link_context
                found += 1
                logger.debug(f"Found canonical URL: {abs_url}")

        logger.info(f"Found {found} valid links on {self.url}")

    async def parse(self) -> Tuple[Set[str], List[Tuple[str, str, Dict[str, Any]]]]:
        """Parse webpage and extract media files and links"""
        try:
            async with self as parser:  # Use context manager to handle session lifecycle
                # Get webpage content with proper encoding detection
                content = await self._get_content()
                if not content:
                    return set(), []

                # Create soup object
                soup = BeautifulSoup(content, "html.parser")

                # Process standard HTML elements
                await self._extract_images(soup)
                await self._extract_videos(soup)
                await self._extract_links(soup)

                # Process dynamic content if enabled
                if self.process_dynamic:
                    await self._handle_dynamic_content(soup)

                return self.links, self.media_files
        except Exception as e:
            logger.error(f"Error parsing {self.url}: {str(e)}")
            raise

    async def _handle_dynamic_content(self, soup: BeautifulSoup) -> None:
        """Handle dynamic content using static analysis instead of browser rendering"""
        try:
            if not self.process_js:
                return

            # Process all script tags
            for script in soup.find_all("script"):
                if not script.string:
                    continue

                # Extract media URLs from JavaScript content
                self._extract_media_from_js(script.string)

            # Process data attributes on elements
            for elem in soup.find_all(True):  # Find all elements
                # Check for framework-specific patterns
                for framework, pattern in self.JS_PATTERNS[
                    "framework_patterns"
                ].items():
                    if re.search(pattern, str(elem)):
                        self._process_framework_element(elem, framework)

                # Process data attributes
                for attr in elem.attrs:
                    if attr.startswith("data-"):
                        self._process_data_attribute(elem, attr)

            # Look for common lazy-loading patterns
            for pattern in self.LAZY_LOAD_PATTERNS["data-attributes"]:
                for elem in soup.find_all(attrs={pattern: True}):
                    url = elem.get(pattern)
                    if url and is_media_url(url):
                        abs_url = urljoin(self.url, url)
                        attrs = {"source": f"lazy-load-{pattern}"}
                        self.media_files.append(("image", abs_url, attrs))

        except Exception as e:
            logger.error(
                f"Error in static JavaScript analysis for {self.url}: {str(e)}"
            )

    def _extract_media_from_js(self, js_content: str) -> None:
        """Extract media URLs from JavaScript content using regex patterns"""
        # Process image patterns
        for pattern in self.JS_PATTERNS["image_sources"]:
            for match in re.finditer(pattern, js_content):
                url = match.group(1)
                if url and url.startswith(("http://", "https://")):
                    attrs = {"source": "js-static-analysis"}
                    self.media_files.append(("image", url, attrs))

        # Process video patterns
        for pattern in self.JS_PATTERNS["video_sources"]:
            for match in re.finditer(pattern, js_content):
                url = match.group(1)
                if url and url.startswith(("http://", "https://")):
                    attrs = {"source": "js-static-analysis"}
                    self.media_files.append(("video", url, attrs))

    def _process_framework_element(self, elem: Any, framework: str) -> None:
        """Process elements with framework-specific patterns"""
        attrs = {"source": f"framework-{framework}"}

        if framework == "react":
            # Handle React lazy loading
            src = elem.get("data-src") or elem.get("data-lazy")
            if src:
                abs_url = urljoin(self.url, src)
                self.media_files.append(("image", abs_url, attrs))

        elif framework == "vue":
            # Handle Vue.js lazy loading
            src = elem.get("v-lazy")
            if src:
                abs_url = urljoin(self.url, src)
                self.media_files.append(("image", abs_url, attrs))

        elif framework == "angular":
            # Handle Angular lazy loading
            src = elem.get("lazyLoad") or elem.get("ng-src")
            if src:
                abs_url = urljoin(self.url, src)
                self.media_files.append(("image", abs_url, attrs))

    def _process_data_attribute(self, elem: Any, attr: str) -> None:
        """Process data attributes that might contain media URLs"""
        value = elem.get(attr, "").strip()
        if not value:
            return

        # Handle JSON-encoded values
        if value.startswith("{") and value.endswith("}"):
            try:
                data = json.loads(value)
                if isinstance(data, dict):
                    for key, val in data.items():
                        if isinstance(val, str) and is_media_url(val):
                            abs_url = urljoin(self.url, val)
                            attrs = {"source": f"data-{attr}-{key}"}
                            self.media_files.append(("image", abs_url, attrs))
                return
            except json.JSONDecodeError:
                pass

        # Handle direct URLs
        if is_media_url(value):
            abs_url = urljoin(self.url, value)
            attrs = {"source": f"data-{attr}"}
            self.media_files.append(("image", abs_url, attrs))

    def _process_lazy_load_patterns(self, elem: Any) -> None:
        """Process various lazy loading patterns"""
        # Check class patterns
        elem_class = elem.get("class", [])
        if isinstance(elem_class, str):
            elem_class = elem_class.split()

        for pattern in self.LAZY_LOAD_PATTERNS["class-patterns"]:
            for class_name in elem_class:
                if re.search(pattern, class_name, re.IGNORECASE):
                    # Found lazy loading class, check for data attributes
                    for data_attr in self.LAZY_LOAD_PATTERNS["data-attributes"]:
                        if elem.has_attr(data_attr):
                            value = elem[data_attr]
                            if value and is_media_url(value):
                                abs_url = urljoin(self.url, value)
                                attrs = {"source": f"lazy-{class_name}"}
                                self.media_files.append(("image", abs_url, attrs))
                    break

    def get_media_files(self) -> List[Tuple[str, str, Dict[str, Any]]]:
        """
        Get the list of media files found during parsing.

        Returns:
            List of tuples containing:
            - Media type (str): 'image' or 'video'
            - URL (str): Absolute URL of the media file
            - Attributes (Dict[str, Any]): Additional metadata about the media file
        """
        return self.media_files
