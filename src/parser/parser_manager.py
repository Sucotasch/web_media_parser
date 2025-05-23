#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Enhanced parser manager that coordinates parsing and downloading process
"""

import os
import re
import time
import queue
import pickle
import asyncio
import logging
import hashlib

# Import brotli detection
from src.parser.webpage_parser import HAS_BROTLI
import aiofiles
import threading
import traceback
from typing import Dict, Any, Set, List, Optional, Tuple
from urllib.parse import urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, Signal
from bs4 import BeautifulSoup

from src.parser.webpage_parser import WebpageParser
from src.parser.json_parser import JSONWebpageParser
from src.parser.priority_url_queue import PriorityURLQueue
from src.parser.site_pattern_manager import SitePatternManager
from src.downloader.media_downloader import MediaDownloader
from src.parser.utils import (
    is_valid_url,
    get_domain,
    is_media_url,
    is_webpage_url,
    is_same_domain,
    normalize_url,
    is_image_url,
    is_video_url
)

logger = logging.getLogger(__name__)


class ParserManager(QObject):
    """Enhanced parser manager with async support"""

    # Qt signals
    total_progress_updated = Signal(int)
    current_progress_updated = Signal(int)
    parsing_finished = Signal()
    status_updated = Signal(str)

    def __init__(
        self, url: str, download_path: str, settings: Dict[str, Any], log_handler
    ):
        """Initialize parser manager"""
        super().__init__()
        self.start_url = url
        self.download_path = download_path
        self.settings = settings
        self.log = log_handler

        # Initialize state variables
        self.is_running = False
        self.is_paused = False
        self._pause_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._pause_event.set()  # Not paused initially

        # Initialize max depth
        self.max_depth = self.settings.get("search_depth", 3)
        
        # Domain health tracking
        self.domain_health = {}  # Tracks domain health {domain: {failures: count, total: count}}
        self.quarantined_domains = set()  # Set of domains currently in quarantine
        self.quarantine_queue = asyncio.Queue()  # Queue for URLs from quarantined domains
        
        # Initialize site pattern manager if patterns are enabled
        self.pattern_manager = None
        if self.settings.get("use_patterns", True):
            custom_pattern_path = self.settings.get("custom_pattern_path", None)
            
            # Initialize the SitePatternManager
            self.pattern_manager = SitePatternManager(
                enable_built_in=True,
                custom_pattern_path=custom_pattern_path
            )
            logger.info("Using SitePatternManager for pattern transformations")

        # Initialize queues and sets
        self.url_queue = PriorityURLQueue()
        self.download_queue = asyncio.Queue()
        self.processed_urls = set()
        self.downloaded_files = set()

        # Initialize statistics
        self.stats = {
            "pages_processed": 0,
            "images_found": 0,
            "videos_found": 0,
            "files_downloaded": 0,
            "files_skipped": 0,
        }
        
        # Create event loop
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        # Create shared session for all parsers
        self.session = None
        self.parser_tasks = []
        self.downloader_tasks = []

    async def _update_queue_priorities(
        self, url: str, media_files: List[Tuple[str, str, Dict[str, Any]]]
    ):
        """Update URL queue priorities based on media discovery"""
        media_count = len(media_files)
        if media_count > 0:
            self.url_queue.update_domain_score(url, media_count)
            self.url_queue.update_url_pattern(url, True)
        else:
            self.url_queue.update_url_pattern(url, False)

    def start_parsing(self):
        """Start the parsing process"""
        self.is_running = True

        # Start first URL with depth 0
        asyncio.run_coroutine_threadsafe(
            self.url_queue.put(self.start_url, 0), self.loop
        )

        # Start async event loop in a separate thread
        self.loop_thread = threading.Thread(target=self._run_event_loop)
        self.loop_thread.daemon = True
        self.loop_thread.start()

        # Start progress monitoring
        self.monitor_thread = threading.Thread(target=self._monitor_progress)
        self.monitor_thread.daemon = True
        self.monitor_thread.start()

    def _run_event_loop(self):
        """Run the asyncio event loop"""
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main_task())
        except Exception as e:
            logger.error(f"Error in event loop: {str(e)}")
            logger.debug(traceback.format_exc())
        finally:
            # Ensure session is closed before loop
            if self.session is not None and not self.session.closed:
                try:
                    self.loop.run_until_complete(self.session.close())
                    logger.info("Session closed in event loop shutdown")
                except Exception as e:
                    logger.error(f"Error closing session: {str(e)}")
            
            # Close event loop
            self.loop.close()

    async def _main_task(self):
        """Main async task that coordinates parsing and downloading"""
        try:
            # Create shared aiohttp session for all parsers
            import aiohttp
            if HAS_BROTLI:
                from aiohttp import ClientSession, TCPConnector
                self.session = ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60, connect=20, sock_read=60),
                    connector=TCPConnector(ssl=False)
                )
            else:
                self.session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=60, connect=20, sock_read=60)
                )
            
            # Create parser and downloader tasks
            parser_count = self.settings.get("parser_threads", 2)
            downloader_count = self.settings.get("downloader_threads", 4)

            # Start with initial URL
            # The start_url is used as both the URL and the source for the initial seed
            # This ensures that all subsequent URLs are evaluated against this starting point
            logger.info(f"Adding initial URL to queue: {self.start_url}")
            await self.url_queue.put(self.start_url, 0, self.start_url, {
                "is_start_url": True,
                "start_url": self.start_url
            })

            # Create parser tasks
            self.parser_tasks = []
            for _ in range(parser_count):
                task = asyncio.create_task(self._parser_worker())
                task.set_name(f"parser_{_}")
                self.parser_tasks.append(task)

            # Create downloader tasks
            self.downloader_tasks = []
            for _ in range(downloader_count):
                task = asyncio.create_task(self._downloader_worker())
                task.set_name(f"downloader_{_}")
                self.downloader_tasks.append(task)

            # All tasks combined for monitoring
            all_tasks = self.parser_tasks + self.downloader_tasks

            # Wait for all tasks with proper cancellation handling
            try:
                await asyncio.gather(*all_tasks)
            except asyncio.CancelledError:
                # Already handled in stop_parsing()
                pass

        except Exception as e:
            logger.error(f"Error in main task: {str(e)}")
            logger.debug(traceback.format_exc())
            self.stop_parsing()
        finally:
            # Ensure everything is cleaned up properly
            if not self._stop_event.is_set():
                self.stop_parsing()

    async def _parser_worker(self):
        """Worker function for parsing URLs"""
        while self.is_running:
            if self.is_paused:
                await asyncio.sleep(0.5)
                continue

            try:
                # Check if processing is complete
                if (
                    self.download_queue.empty()
                    and self.url_queue.empty()
                    and self.stats["pages_processed"] > 0
                    and not self.is_paused
                ):
                    # Check if there are quarantined items to process
                    quarantine_size = self.quarantine_queue.qsize()
                    if quarantine_size > 0:
                        logger.info(f"Main queues empty. Processing {quarantine_size} URLs from quarantine")
                        self.status_updated.emit(f"Processing {quarantine_size} URLs from quarantined domains...")
                        
                        # Process URLs from quarantine - move them to download queue with standard settings
                        items_to_process = min(quarantine_size, 50)  # Process in batches of 50 to avoid blocking
                        for _ in range(items_to_process):
                            try:
                                item = self.quarantine_queue.get_nowait()
                                # Reset quarantine status for the domain to give it another chance
                                domain = urlparse(item["url"]).netloc
                                if domain in self.quarantined_domains:
                                    self.quarantined_domains.remove(domain)
                                    if domain in self.domain_health:
                                        # Reset failure counter but keep history
                                        self.domain_health[domain]["failures"] = 1  # Start with 1 failure
                                
                                await self.download_queue.put(item)
                                self.quarantine_queue.task_done()
                            except asyncio.QueueEmpty:
                                break
                        
                        # Continue processing
                        continue
                    
                    # If no quarantined items or quarantine processed, we're done
                    logger.info("All queues empty and processing complete")
                    self.parsing_finished.emit()
                    self.stop_parsing()
                    return

                # Try to get URL with exponential backoff
                backoff = 0.1
                max_backoff = 2.0
                url = None
                depth = 0

                while True:
                    try:
                        url, depth = await self.url_queue.get(timeout=backoff)
                        break
                    except asyncio.TimeoutError:
                        if backoff < max_backoff:
                            backoff = min(backoff * 2, max_backoff)
                        await asyncio.sleep(0.1)
                        if not self.is_running:
                            return
                    except asyncio.QueueEmpty:
                        await asyncio.sleep(0.1)
                        if not self.is_running:
                            return
                        continue
                    except Exception as e:
                        logger.error(f"Error getting URL from queue: {str(e)}")
                        await asyncio.sleep(1)
                        continue

                if url is None:
                    continue

                try:
                    # Make URL absolute if needed
                    if not url.startswith(("http://", "https://")):
                        url = urljoin(self.start_url, url)

                    # Normalize URL
                    url = normalize_url(url)

                    # Skip if already processed
                    if url in self.processed_urls:
                        self.url_queue.task_done()
                        continue

                    # Add to processed URLs
                    self.processed_urls.add(url)

                    # Determine if this is likely a JSON API or a webpage
                    is_json_api = False
                    parsed_url = urlparse(url)
                    path = parsed_url.path.lower()
                    query = parsed_url.query.lower()
                    
                    # Check URL patterns that suggest JSON API
                    if (
                        "/api/" in path or 
                        "/json/" in path or 
                        path.endswith(".json") or 
                        "format=json" in query or 
                        "output=json" in query or
                        "callback=" in query  # JSONP
                    ):
                        is_json_api = True
                        logger.info(f"URL {url} appears to be a JSON API, using JSON parser")
                    
                    # Process the resource with appropriate parser
                    if is_json_api:
                        # Use JSON parser
                        async with JSONWebpageParser(
                            url=url,
                            settings=self.settings,
                            external_session=self.session
                        ) as json_parser:
                            links, media_files = await json_parser.parse()
                            
                            # Process discovered media files
                            await self._process_media_files(media_files, url)
    
                            # Update queue priorities based on media discovery
                            await self._update_queue_priorities(url, media_files)
    
                            # Process discovered URLs if within depth limit
                            if depth < self.max_depth:
                                for discovered_url in links:
                                    if not discovered_url.startswith(
                                        ("http://", "https://")
                                    ):
                                        discovered_url = urljoin(url, discovered_url)
                                    # Add context that this came from JSON API
                                    await self.url_queue.put(discovered_url, depth + 1, url, {'from_json': True})
                    else:
                        # Use standard HTML parser
                        async with WebpageParser(
                            url=url,
                            settings=self.settings,
                            process_js=self.settings.get("process_js", False),
                            process_dynamic=self.settings.get("process_dynamic", False),
                            external_session=self.session,
                            pattern_manager=self.pattern_manager
                        ) as webpage_parser:
                            await webpage_parser.parse()
    
                            # Process discovered media files
                            media_files = webpage_parser.get_media_files()
                            await self._process_media_files(media_files, url)
    
                            # Update queue priorities based on media discovery
                            await self._update_queue_priorities(url, media_files)
    
                            # Process discovered URLs if within depth limit
                            if depth < self.max_depth:
                                discovered_urls = webpage_parser.get_discovered_urls()  # Now returns a dictionary
                                
                                # Separate navigation and media URLs for prioritized processing
                                media_container_urls = {}
                                navigation_urls = {}
                                
                                # First pass: categorize URLs
                                for discovered_url, context in discovered_urls.items():
                                    # Make absolute URL if needed
                                    if not discovered_url.startswith(("http://", "https://")):
                                        discovered_url = urljoin(url, discovered_url)
                                    
                                    # Setup common context fields
                                    if "source_url" not in context:
                                        context["source_url"] = url  # Track immediate source
                                    context["start_url"] = self.start_url
                                    
                                    # Categorize the URL
                                    if is_media_url(discovered_url):
                                        # Direct media URLs get highest priority and immediate processing
                                        if 'priority' not in context:
                                            context['priority'] = 30.0
                                        await self.url_queue.put(discovered_url, depth + 1, self.start_url, context)
                                        logger.debug(f"Added direct media URL to queue with max priority: {discovered_url}")
                                    elif context.get('is_webpage', False) or context.get('potential_media_container', False):
                                        # URLs likely to contain media get high priority but processed after direct media
                                        if 'priority' not in context:
                                            context['priority'] = 20.0
                                        media_container_urls[discovered_url] = context
                                        logger.debug(f"Queued media container URL for processing: {discovered_url}")
                                    else:
                                        # Navigation URLs get lowest priority and are processed last
                                        navigation_urls[discovered_url] = context
                                        logger.debug(f"Queued navigation URL for processing: {discovered_url}")
                                
                                # Second pass: add media container URLs after direct media is processed
                                for discovered_url, context in media_container_urls.items():
                                    logger.info(f"Adding high-priority media container URL to parsing queue: {discovered_url}")
                                    await self.url_queue.put(discovered_url, depth + 1, self.start_url, context)
                                
                                # Final pass: add navigation URLs last
                                for discovered_url, context in navigation_urls.items():
                                    # For navigation URLs, apply standard priority
                                    if 'priority' not in context:
                                        context['priority'] = 1.0
                                    await self.url_queue.put(discovered_url, depth + 1, self.start_url, context)

                    # Update stats and emit signal
                    self.stats["pages_processed"] += 1
                    # Count how many images and videos were found
                    for media_type, _, _ in media_files:
                        if media_type == "image":
                            self.stats["images_found"] += 1
                        elif media_type == "video":
                            self.stats["videos_found"] += 1
                    self.status_updated.emit(f"Processed: {url}")

                except Exception as e:
                    logger.error(f"Error processing URL {url}: {str(e)}")
                    logger.debug(traceback.format_exc())

                finally:
                    self.url_queue.task_done()

            except Exception as e:
                logger.error(f"Unexpected error in parser worker: {str(e)}")
                logger.debug(traceback.format_exc())
                await asyncio.sleep(1)

    async def _process_media_files(
        self, media_files: List[Tuple[str, str, Dict[str, Any]]], source_url: str
    ) -> None:
        """Process media files with enhanced filtering and prioritization"""
        logger.debug(f"Processing {len(media_files)} media files from {source_url}")
        
        if not media_files:
            return

        # Apply the same prioritization logic to all pages, not just the initial page
        # This ensures we process all media from the current page before moving on
        
        # First process direct media files (actual images/videos)
        direct_media = [
            (mtype, url, attrs)
            for mtype, url, attrs in media_files
            if is_media_url(url) or (attrs.get("source", "") in ["img", "video", "picture", "source"])
        ]
        
        # Then process linked full-size images (highest quality versions)
        fullsize_media = [
            (mtype, url, attrs)
            for mtype, url, attrs in media_files
            if attrs.get("source", "") in ["fullsize-link", "parent-link", "original"]
            and (mtype, url, attrs) not in direct_media
        ]
        
        # Finally process any remaining media (usually lower priority)
        remaining_media = [
            (mtype, url, attrs)
            for mtype, url, attrs in media_files
            if (mtype, url, attrs) not in direct_media and (mtype, url, attrs) not in fullsize_media
        ]
        
        # Process in priority order
        if direct_media:
            logger.debug(f"Processing {len(direct_media)} direct media files from {source_url}")
            await self._process_media_batch(direct_media, source_url)
            
        if fullsize_media:
            logger.debug(f"Processing {len(fullsize_media)} full-size media files from {source_url}")
            await self._process_media_batch(fullsize_media, source_url)
            
        if remaining_media:
            logger.debug(f"Processing {len(remaining_media)} other media files from {source_url}")
            await self._process_media_batch(remaining_media, source_url)

    async def _process_media_batch(
        self, media_files: List[Tuple[str, str, Dict[str, Any]]], source_url: str
    ) -> None:
        """Process a batch of media files with depth-first processing"""
        # Sort media files by priority - direct media files first, then linked media
        sorted_media = sorted(media_files, key=lambda x: self._get_media_priority(x, source_url), reverse=True)
        
        logger.debug(f"Processing {len(sorted_media)} media files from {source_url} in priority order")
        
        for media_type, url, attrs in sorted_media:
            try:
                if not (url.startswith("http://") or url.startswith("https://")):
                    url = urljoin(source_url, url)

                url = normalize_url(url)

                # Skip if already downloaded
                if url in self.downloaded_files:
                    continue
                    
                # Double-check: if it's clearly a media URL by extension, prioritize download
                if media_type in ["image", "video"] and is_media_url(url):
                    # Direct media URLs should be downloaded, not parsed
                    logger.info(f"Processing direct media URL for download: {url}")
                    # Continue to download processing
                elif is_webpage_url(url):
                    # For URLs that are definitely webpages, add to parsing queue
                    logger.info(f"Adding webpage URL to parsing queue instead of downloading: {url}")
                    # Instead of downloading, add to parsing queue if within depth limit
                    # Get the current depth of the source URL
                    current_depth = 0
                    for i in range(len(self.parser_tasks)):
                        try:
                            item = self.url_queue._queue[i]
                            if item.url == source_url:
                                current_depth = item.depth
                                break
                        except (IndexError, AttributeError):
                            pass
                    
                    # Only add if within depth limit
                    if current_depth < self.max_depth:
                        context = {
                            "source_url": source_url,
                            "start_url": self.start_url,
                            "from_media_item": True,
                            "media_context": attrs,
                            "priority": 5.0  # High priority for potential gallery pages
                        }
                        # Use the start_url for downward enforcement, but keep track of the immediate source
                        await self.url_queue.put(url, current_depth + 1, self.start_url, context)
                    continue
                    
                # Apply dimension filtering if image and has dimensions
                if media_type == "image" and "dimensions" in attrs:
                    min_width = self.settings.get("min_image_width", 100)
                    min_height = self.settings.get("min_image_height", 100)
                    width = attrs["dimensions"].get("width", 0)
                    height = attrs["dimensions"].get("height", 0)
                    
                    # Skip if image is too small and both dimensions are known
                    if width > 0 and height > 0:
                        if width < min_width and height < min_height:
                            logger.debug(f"Skipping image too small: {url} ({width}x{height} < {min_width}x{min_height})")
                            continue

                # Add to downloaded files set
                self.downloaded_files.add(url)

                # Generate filename
                filename = self._get_filename_from_url(url, media_type)

                # Create media item
                media_item = {
                    "url": url,
                    "source_url": source_url,
                    "media_type": media_type,
                    "attrs": attrs,
                    "filename": filename,
                }

                await self.download_queue.put(media_item)

                # Update stats
                if media_type == "image":
                    self.stats["images_found"] += 1
                elif media_type == "video":
                    self.stats["videos_found"] += 1

            except Exception as err:
                logger.error(f"Error processing media file {url}: {str(err)}")
                logger.debug(traceback.format_exc())
                continue

        logger.info(f"Processed {len(media_files)} media files from {source_url}")

    async def _process_links(
        self, links: Set[str], depth: int, source_url: str
    ) -> None:
        """Process links with enhanced filtering"""
        source_domain = get_domain(source_url)
        stay_in_domain = self.settings.get("stay_in_domain", True)
        stop_words = self.settings.get("stop_words", [])

        logger.debug(
            f"Processing {len(links)} links from {source_url} at depth {depth}"
        )

        for link in links:
            try:
                # Normalize and make absolute
                if not (link.startswith("http://") or link.startswith("https://")):
                    link = urljoin(source_url, link)
                link = normalize_url(link)

                logger.debug(f"Processing link: {link}")

                # Apply filters
                if link in self.processed_urls:
                    logger.debug(f"Skipping already processed link: {link}")
                    continue

                if stay_in_domain and not is_same_domain(link, source_url):
                    logger.debug(f"Skipping out-of-domain link: {link}")
                    continue

                if any(stop_word.lower() in link.lower() for stop_word in stop_words):
                    logger.debug(f"Skipping link with stop word: {link}")
                    continue

                await self.url_queue.put(link, depth, source_url)
                logger.debug(f"Queued link for parsing: {link} (depth={depth})")

            except Exception as err:
                logger.error(f"Error processing link {link}: {str(err)}")
                logger.debug(traceback.format_exc())
                continue

    async def _downloader_worker(self) -> None:
        """Async worker for downloading media files"""
        while self.is_running and not self._stop_event.is_set():
            try:
                if self.is_paused:
                    await self._pause_event.wait()
                    if not self.is_running or self._stop_event.is_set():
                        break
                    continue

                try:
                    # Get item with timeout
                    media_item = await asyncio.wait_for(
                        self.download_queue.get(), timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue

                if not media_item:
                    continue

                try:
                    url = media_item["url"]
                    media_type = media_item["media_type"]
                    filename = media_item["filename"]
                    filepath = os.path.join(self.download_path, filename)

                    # Check if file already exists
                    if await self._check_file_exists(filepath, url):
                        self.stats["files_skipped"] += 1
                        logger.info(f"File already exists and is valid: {filename}")
                        continue

                    # Log the media type before creating downloader
                    logger.info(f"Creating downloader for media type: {media_type} URL: {url}")
                    
                    # Use our centralized functions to determine media type
                    from src.parser.utils import is_video_url, is_image_url, is_media_url, is_webpage_url
                    
                    # Check if this URL appears to be a video
                    if is_video_url(url) and media_type != "video":
                        logger.info(f"Overriding media type from '{media_type}' to 'video' based on URL pattern check: {url}")
                        media_type = "video"
                    # Re-verify this is actually a media URL
                    elif not is_media_url(url):
                        logger.warning(f"URL was detected as {media_type} but doesn't appear to be a media file: {url}")
                        
                        # Check for URL patterns that typically indicate full-size images across many sites
                        # This is a universal approach that works for any site with common naming patterns
                        fullsize_indicators = ['full', 'large', 'original', 'highres', 'hi-res', 'big', 'max']
                        if any(indicator in url.lower() for indicator in fullsize_indicators) and any(ext in url.lower() for ext in [".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"]):
                            logger.info(f"Override: Processing as media URL with fullsize indicator: {url}")
                            # Continue processing as media URL
                        elif is_webpage_url(url):
                            # Add to parsing queue instead
                            logger.info(f"Redirecting non-media URL to parsing queue: {url}")
                            current_depth = 0
                            for i in range(len(self.parser_tasks)):
                                try:
                                    item = self.url_queue._queue[i]
                                    if item.url == media_item['source_url']:
                                        current_depth = item.depth
                                        break
                                except (IndexError, AttributeError):
                                    pass
                            
                            if current_depth < self.max_depth:
                                context = {
                                    "source_url": media_item['source_url'],
                                    "start_url": self.start_url,
                                    "from_media_item": True,
                                    "potential_media_container": True,
                                    "priority": 10.0  # Very high priority for redirected content
                                }
                                # Use the start_url for downward enforcement, but keep track of the immediate source
                                await self.url_queue.put(url, current_depth + 1, self.start_url, context)
                            continue
                    
                    # Check if domain is quarantined
                    from urllib.parse import urlparse
                    domain = urlparse(url).netloc
                    
                    if domain in self.quarantined_domains:
                        # Put this media item in the quarantine queue for later processing
                        logger.info(f"URL from quarantined domain: {url} (domain: {domain}) - postponing download")
                        await self.quarantine_queue.put(media_item)
                        continue
                    
                    # Check domain health and adjust settings
                    if domain not in self.domain_health:
                        self.domain_health[domain] = {"failures": 0, "total": 0}
                    
                    domain_state = self.domain_health[domain]
                    is_probation = domain_state["failures"] > 0  # Domain has had failures
                    
                    # Adjust timeout and retries based on domain health
                    timeout = 2 if is_probation else self.settings.get("timeout", 30)  # 2 seconds for problematic domains
                    retries = 0 if is_probation else self.settings.get("retry_count", 3)  # No retries for problematic domains
                    
                    if is_probation:
                        logger.debug(f"Domain {domain} on probation - using reduced timeout={timeout}s, retries={retries}")
                    
                    downloader = MediaDownloader(
                        url=url,
                        filepath=filepath,
                        settings=self.settings,
                        media_type=media_type,
                        source_url=media_item["source_url"],
                    )

                    downloader.set_progress_callback(self._update_current_progress)

                    self.status_updated.emit(f"Downloading: {filename}")
                    logger.info(f"Downloading file: {url} -> {filename}")

                    # Use custom timeout and retries
                    result = downloader.download(timeout=timeout, retries=retries)
                    
                    # Update domain health tracking
                    domain_state["total"] += 1
                    if not result["success"]:
                        domain_state["failures"] += 1
                        logger.debug(f"Domain {domain} failure count: {domain_state['failures']}")
                        
                        # Quarantine domain if too many failures
                        if domain_state["failures"] >= 3:
                            logger.warning(f"Domain {domain} quarantined after {domain_state['failures']} failures")
                            self.quarantined_domains.add(domain)
                    else:
                        # Success - gradually reduce failure count
                        if domain_state["failures"] > 0:
                            domain_state["failures"] = max(0, domain_state["failures"] - 1)
                            logger.debug(f"Domain {domain} failure count reduced to {domain_state['failures']}")

                    if result["success"]:
                        self.stats["files_downloaded"] += 1
                        logger.info(f"Successfully downloaded: {filename}")
                    else:
                        self.stats["files_skipped"] += 1
                        logger.warning(
                            f"Failed to download file: {url} - {result['error']}"
                        )

                except Exception as err:
                    logger.error(f"Error processing download item: {str(err)}")
                    logger.debug(traceback.format_exc())
                finally:
                    self.download_queue.task_done()

            except Exception as err:
                logger.error(f"Error in downloader worker: {str(err)}")
                logger.debug(traceback.format_exc())
                await asyncio.sleep(1)  # Prevent tight loop on error

    async def _check_file_exists(self, filepath: str, url: str) -> bool:
        """
        Simplified check - only see if the file exists
        Always return False to generate a new unique filename
        """
        return False

    async def _calculate_partial_hash(self, filepath: str, size: int = 1024) -> str:
        """Stub method kept for compatibility - no longer used"""
        return ""

    def pause_parsing(self) -> None:
        """Pause the parsing process"""
        self.is_paused = True
        self._pause_event.clear()
        logger.info("Parsing paused")

    def resume_parsing(self) -> None:
        """Resume the parsing process"""
        self.is_paused = False
        self._pause_event.set()
        logger.info("Parsing resumed")

    def stop_parsing(self) -> None:
        """Stop the parsing process"""
        logger.info("Stopping parsing...")
        self.is_running = False
        self._stop_event.set()
        self._pause_event.set()  # Ensure no thread is waiting
        
        # Clear the download queue
        try:
            while not self.download_queue.empty():
                self.download_queue.get_nowait()
                self.download_queue.task_done()
            logger.info("Download queue cleared")
        except Exception as e:
            logger.error(f"Error clearing download queue: {str(e)}")
            
        # Clear the quarantine queue
        try:
            while not self.quarantine_queue.empty():
                self.quarantine_queue.get_nowait()
                self.quarantine_queue.task_done()
            logger.info("Quarantine queue cleared")
        except Exception as e:
            logger.error(f"Error clearing quarantine queue: {str(e)}")
        
        # Clear the URL queue
        try:
            self.url_queue = PriorityURLQueue()  # Create a new empty queue
            logger.info("URL queue cleared")
        except Exception as e:
            logger.error(f"Error clearing URL queue: {str(e)}")
            
        # Cancel all running tasks
        for task in self.parser_tasks + self.downloader_tasks:
            if not task.done():
                task.cancel()
        
        # Close shared aiohttp session
        if self.session is not None:
            asyncio.run_coroutine_threadsafe(self.session.close(), self.loop)
            logger.info("Shared session closed")
            
        logger.info("Parsing stopped")

    def _monitor_progress(self) -> None:
        """Monitor progress and update UI"""
        while self.is_running and not self._stop_event.is_set():
            try:
                if self.is_paused:
                    if not self._pause_event.is_set():
                        time.sleep(0.5)
                        continue

                # Update total progress
                total_found = self.stats["images_found"] + self.stats["videos_found"]
                total_processed = (
                    self.stats["files_downloaded"] + self.stats["files_skipped"]
                )

                if total_found > 0:
                    progress = int((total_processed / total_found) * 100)
                    self.total_progress_updated.emit(progress)

                # Sleep to reduce CPU usage
                time.sleep(0.5)

            except Exception as e:
                logger.error(f"Error in progress monitor: {str(e)}")
                logger.debug(traceback.format_exc())
                time.sleep(1)

    def _update_current_progress(self, progress: int) -> None:
        """Update current file progress"""
        self.current_progress_updated.emit(progress)

    def _get_filename_from_url(self, url: str, media_type: str) -> str:
        """Generate unique filename from URL"""
        parsed_url = urlparse(url)
        path = parsed_url.path.strip("/")

        # Get basename from path
        basename = os.path.basename(path)

        # If no basename or no extension, generate a name
        if not basename or "." not in basename:
            url_hash = hashlib.md5(url.encode()).hexdigest()[:10]

            # Set default extension based on media type
            extension = ".jpg" if media_type == "image" else ".mp4"
            basename = f"{url_hash}{extension}"

        # Ensure unique filename
        basename = self._ensure_unique_filename(basename)

        # Sanitize filename
        return self._sanitize_filename(basename)

    def _ensure_unique_filename(self, basename: str) -> str:
        """Ensure filename is unique in the download directory"""
        name, ext = os.path.splitext(basename)
        counter = 1
        result = basename

        while os.path.exists(os.path.join(self.download_path, result)):
            result = f"{name}_{counter}{ext}"
            counter += 1

        return result

    def _sanitize_filename(self, filename: str) -> str:
        """Remove invalid characters from filename"""
        # Replace invalid characters
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            filename = filename.replace(char, "_")

        # Limit length
        max_length = 255
        if len(filename) > max_length:
            name, ext = os.path.splitext(filename)
            filename = name[: max_length - len(ext)] + ext

        return filename

    async def _calculate_file_hash(self, filepath: str) -> Optional[str]:
        """Stub method kept for compatibility - no longer used"""
        return None

    def _get_media_priority(self, media_item: Tuple[str, str, Dict[str, Any]], source_url: str) -> float:
        """
        Calculate priority for media items to ensure proper processing order:
        - Direct media files (images, videos) get highest priority
        - Full-size images (from thumbnails) get high priority
        - Items from the initial page get priority over other pages
        """
        media_type, url, attrs = media_item
        priority = 1.0
        
        # Base priority by media type
        if media_type == "image":
            priority *= 2.0
        elif media_type == "video":
            priority *= 3.0  # Videos are often higher value content
            
        # Priority based on source
        source_type = attrs.get("source", "")
        if "fullsize" in source_type or "original" in source_type:
            priority *= 3.0  # Full-size images are highest priority
        elif "parent-link" in source_type:
            priority *= 2.5  # Links from parent elements are likely higher quality
            
        # Priority based on URL patterns suggesting higher quality
        if any(pattern in url.lower() for pattern in ["/full/", "/large/", "/original/", "fullsize", "highres"]):
            priority *= 2.0
            
        # Highest priority for media from initial page
        if source_url == self.start_url:
            priority *= 3.0
            
        # Use dimensions if available
        if "dimensions" in attrs:
            width = attrs["dimensions"].get("width", 0)
            height = attrs["dimensions"].get("height", 0)
            if width > 0 and height > 0:
                # Boost priority for larger images (but cap to avoid overflow)
                size_factor = min(1.0 + ((width * height) / 1000000), 3.0)
                priority *= size_factor
                
        return priority
    
    def get_stats(self) -> Dict[str, int]:
        """Get parsing statistics"""
        return self.stats.copy()

    async def save_state(self, state_path: str) -> None:
        """Save current state for resume capability"""
        state = {
            "url_queue": list(self.url_queue._queue),
            "download_queue": list(self.download_queue._queue),
            "processed_urls": list(self.processed_urls),
            "downloaded_files": list(self.downloaded_files),
            "stats": self.stats,
            "settings": self.settings,
            "start_url": self.start_url,
            "download_path": self.download_path,
        }

        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        async with aiofiles.open(state_path, "wb") as f:
            await f.write(pickle.dumps(state))

    async def load_state(self, state_path: str) -> None:
        """Load previously saved state"""
        try:
            async with aiofiles.open(state_path, "rb") as f:
                data = await f.read()
                state = pickle.loads(data)

            # Restore state
            for item in state["url_queue"]:
                await self.url_queue.put(item)
            for item in state["download_queue"]:
                await self.download_queue.put(item)

            self.processed_urls = set(state["processed_urls"])
            self.downloaded_files = set(state["downloaded_files"])
            self.stats = state["stats"]
            self.settings = state["settings"]
            self.start_url = state["start_url"]
            self.download_path = state["download_path"]

        except Exception as err:
            logger.error(f"Error loading state from {state_path}: {str(err)}")
            logger.debug(traceback.format_exc())

    # Unused Manager class removed - functionality now handled by the main ParserManager class
