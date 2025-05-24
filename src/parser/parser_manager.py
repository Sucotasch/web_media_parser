#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Enhanced parser manager that coordinates parsing and downloading process
"""

import os
import re
import time
# import queue # Not used directly, asyncio.Queue is used
import pickle
import asyncio
import logging
import hashlib

import aiofiles
import threading
import traceback
from typing import Dict, Any, Set, List, Optional, Tuple
from urllib.parse import urlparse, urljoin
# from concurrent.futures import ThreadPoolExecutor # Not actively used in current provided code

from PySide6.QtCore import QObject, Signal
# from bs4 import BeautifulSoup # Not used directly in ParserManager

from src.parser.webpage_parser import WebpageParser
from src.parser.json_parser import JSONWebpageParser
from src.parser.priority_url_queue import PriorityURLQueue
from src.parser.site_pattern_manager import SitePatternManager
from src.downloader.media_downloader import MediaDownloader
from src.parser.utils import (
    # is_valid_url, # Not used
    get_domain,
    is_media_url,
    is_webpage_url,
    is_same_domain,
    normalize_url,
    # is_image_url, # Not used directly
    # is_video_url # Not used directly
)
from src.parser.shared_session import AsyncClientManager
from src import constants as K # Import constants

logger = logging.getLogger(__name__)


class ParserManager(QObject):
    """Enhanced parser manager with async support"""

    total_progress_updated = Signal(int)
    current_progress_updated = Signal(int)
    parsing_finished = Signal()
    status_updated = Signal(str)

    def __init__(
        self, url: str, download_path: str, settings: Dict[str, Any], log_handler
    ):
        super().__init__()
        self.start_url = url
        self.download_path = download_path 
        self.settings = settings 
        self.log = log_handler

        self.is_running = False
        self.is_paused = False
        self._pause_event = asyncio.Event()
        self._stop_event = asyncio.Event()
        self._pause_event.set()

        self.max_depth = self.settings.get(K.SETTING_SEARCH_DEPTH, K.DEFAULT_SEARCH_DEPTH)
        
        self.domain_health = {}
        self.quarantined_domains = set()
        self.quarantine_queue = asyncio.Queue() # Max size can be a constant if needed
        
        self.pattern_manager = None
        if self.settings.get(K.SETTING_USE_PATTERNS, K.DEFAULT_USE_PATTERNS):
            custom_pattern_path = self.settings.get(K.SETTING_CUSTOM_PATTERN_PATH, K.DEFAULT_SETTINGS_VALUES[K.SETTING_CUSTOM_PATTERN_PATH])
            self.pattern_manager = SitePatternManager(
                enable_built_in=True, custom_pattern_path=custom_pattern_path
            )
            logger.info("Using SitePatternManager for pattern transformations")

        self.url_queue = PriorityURLQueue()
        self.download_queue = asyncio.Queue()
        self.processed_urls = set()
        self.downloaded_files = set() # Stores URLs of media marked for download to avoid re-processing

        self.stats = {
            "pages_processed": 0, "images_found": 0, "videos_found": 0,
            "audio_files_found": 0, # Add this line
            "files_downloaded": 0, "files_skipped": 0,
        }
        
        self.loop = asyncio.new_event_loop()
        self.async_client_manager: AsyncClientManager = AsyncClientManager(self.settings)
        
        self.parser_tasks = []
        self.downloader_tasks = []
        self.blocked_domains: Set[str] = self._load_domain_blocklist()

    def _load_domain_blocklist(self, blocklist_file_name: str = K.DOMAIN_BLOCKLIST_FILENAME) -> Set[str]:
        blocked_domains: Set[str] = set()
        current_script_dir = os.path.dirname(os.path.abspath(__file__))
        path_in_parser_dir = os.path.join(current_script_dir, blocklist_file_name)
        
        paths_to_try = [blocklist_file_name, path_in_parser_dir]
        path_found = None

        if blocklist_file_name == K.DOMAIN_BLOCKLIST_FILENAME and os.path.exists(path_in_parser_dir):
            path_found = path_in_parser_dir
        else:
            for path_try in paths_to_try:
                if os.path.exists(path_try):
                    path_found = path_try
                    break
        
        if path_found:
            try:
                with open(path_found, "r", encoding="utf-8") as f:
                    for line in f:
                        domain = line.strip()
                        if domain and not domain.startswith("#"): blocked_domains.add(domain)
                logger.info(f"Loaded {len(blocked_domains)} domains into the blocklist from {path_found}.")
            except Exception as e:
                logger.error(f"Error loading domain blocklist from {path_found}: {e}", exc_info=True)
        else:
            logger.warning(f"Domain blocklist file not found (tried paths like '{path_in_parser_dir}'). Proceeding with an empty blocklist.")
        return blocked_domains

    async def _update_queue_priorities(
        self, url: str, media_files: List[Tuple[str, str, Dict[str, Any]]]
    ):
        media_count = len(media_files)
        if media_count > 0:
            self.url_queue.update_domain_score(url, media_count)
            self.url_queue.update_url_pattern(url, True)
        else:
            self.url_queue.update_url_pattern(url, False)

    def start_parsing(self):
        self.is_running = True
        self._stop_event.clear()
        if not self.loop or self.loop.is_closed(): self.loop = asyncio.new_event_loop()
        
        asyncio.run_coroutine_threadsafe(
            self.url_queue.put(self.start_url, 0, self.start_url, {"is_start_url": True, "start_url": self.start_url}),
            self.loop
        )
        self.loop_thread = threading.Thread(target=self._run_event_loop, name="AsyncEventLoopThread")
        self.loop_thread.daemon = True
        self.loop_thread.start()
        self.monitor_thread = threading.Thread(target=self._monitor_progress, name="ProgressMonitorThread")
        self.monitor_thread.daemon = True
        self.monitor_thread.start()

    def _run_event_loop(self):
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main_task())
        except Exception as e:
            logger.error(f"Error in event loop: {str(e)}", exc_info=True)
        finally:
            if self.async_client_manager:
                try:
                    if self.loop.is_running(): # Should ideally be closed via _main_task's async with
                        self.loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self.async_client_manager.close()))
                    elif not self.loop.is_closed():
                         self.loop.run_until_complete(self.async_client_manager.close())
                    logger.info("AsyncClientManager session closed during event loop shutdown.")
                except Exception as e:
                    logger.error(f"Error closing AsyncClientManager session in _run_event_loop: {e}", exc_info=True)
            if self.loop and not self.loop.is_closed():
                self.loop.close()
                logger.info("Asyncio event loop closed.")

    async def _main_task(self):
        try:
            async with self.async_client_manager as session:
                parser_count = self.settings.get(K.SETTING_PARSER_THREADS, K.DEFAULT_PARSER_THREADS)
                downloader_count = self.settings.get(K.SETTING_DOWNLOADER_THREADS, K.DEFAULT_DOWNLOADER_THREADS)
                logger.info(f"Main task started. Parser threads: {parser_count}, Downloader threads: {downloader_count}")

                self.parser_tasks = [asyncio.create_task(self._parser_worker(session), name=f"parser_{i}") for i in range(parser_count)]
                self.downloader_tasks = [asyncio.create_task(self._downloader_worker(), name=f"downloader_{i}") for i in range(downloader_count)]
                
                all_tasks = self.parser_tasks + self.downloader_tasks
                stop_waiter = asyncio.create_task(self._stop_event.wait(), name="StopEventWaiter")
                
                done, pending = await asyncio.wait(all_tasks + [stop_waiter], return_when=asyncio.FIRST_COMPLETED)

                if stop_waiter in done: logger.info("Stop event received, cancelling tasks.")
                else: logger.info("A worker task completed (or failed), initiating shutdown of other tasks.")
                
                for task in pending:
                    if task is not stop_waiter: task.cancel()
                if pending: await asyncio.gather(*pending, return_exceptions=True)
        except Exception as e:
            logger.error(f"Critical error in main task: {str(e)}", exc_info=True)
        finally:
            logger.info("_main_task finished.")

    async def _handle_empty_queues_and_quarantine(self) -> bool:
        if not (self.download_queue.empty() and self.url_queue.empty() and self.stats["pages_processed"] > 0):
            return True
        quarantine_size = self.quarantine_queue.qsize()
        if quarantine_size > 0:
            logger.info(f"Main queues empty. Processing {quarantine_size} URLs from quarantine.")
            self.status_updated.emit(f"Processing {quarantine_size} URLs from quarantined domains...")
            items_to_process = min(quarantine_size, K.QUARANTINE_BATCH_PROCESS_SIZE)
            for _ in range(items_to_process):
                try:
                    item = await self.quarantine_queue.get()
                    domain = urlparse(item["url"]).netloc
                    if domain in self.quarantined_domains:
                        self.quarantined_domains.remove(domain)
                        if domain in self.domain_health: self.domain_health[domain]["failures"] = 0
                    await self.download_queue.put(item)
                    self.quarantine_queue.task_done()
                except asyncio.QueueEmpty: break
            return True
        logger.info("All queues empty and processing appears complete.")
        if not self._stop_event.is_set(): 
            self.parsing_finished.emit()
        return False

    async def _get_next_url_to_parse(self) -> Optional[Tuple[str, int, str, Dict[str, Any]]]:
        try:
            return await self.url_queue.get(timeout=0.5) 
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            return None

    def _determine_parser_type(self, url: str) -> bool:
        parsed_url = urlparse(url)
        path = parsed_url.path.lower()
        query = parsed_url.query.lower()
        return ("/api/" in path or "/json/" in path or path.endswith(".json") or 
                "format=json" in query or "output=json" in query or "callback=" in query)

    async def _invoke_parser(self, url: str, session, is_json_api: bool, context: Dict[str, Any]):
        links_found: Any = set() 
        media_files_found: List[Tuple[str, str, Dict[str, Any]]] = []
        if is_json_api:
            logger.debug(f"Using JSONWebpageParser for {url}")
            async with JSONWebpageParser(url=url, settings=self.settings, external_session=session) as p:
                links_found, media_files_found = await p.parse()
        else:
            logger.debug(f"Using WebpageParser for {url}")
            async with WebpageParser(
                url=url, settings=self.settings,
                process_js=self.settings.get(K.SETTING_PROCESS_JS, K.DEFAULT_PROCESS_JS),
                process_dynamic=self.settings.get(K.SETTING_PROCESS_DYNAMIC, K.DEFAULT_PROCESS_DYNAMIC),
                external_session=session, pattern_manager=self.pattern_manager
            ) as p: 
                links_dict, media_files_found = await p.parse()
                links_found = links_dict 
        return links_found, media_files_found

    async def _process_parser_results(self, url: str, depth: int, 
                                      links_data: Any, media_files: List[Tuple[str, str, Dict[str, Any]]],
                                      original_url_context: Dict[str, Any]):
        await self._process_media_files(media_files, url)
        await self._update_queue_priorities(url, media_files)

        if depth < self.max_depth:
            urls_to_queue = []
            if isinstance(links_data, dict): # From WebpageParser
                for disc_url, link_ctx in links_data.items(): urls_to_queue.append((disc_url, link_ctx))
            elif isinstance(links_data, set): # From JSONWebpageParser
                for disc_url in links_data: urls_to_queue.append((disc_url, {})) 

            for disc_url_str, link_spec_ctx in urls_to_queue:
                abs_disc_url = disc_url_str
                if not abs_disc_url.startswith(("http://", "https://")):
                    abs_disc_url = urljoin(url, abs_disc_url)
                
                disc_domain = get_domain(abs_disc_url)
                if disc_domain in self.blocked_domains:
                    logger.debug(f"Skipping blocked domain for URL {abs_disc_url} (Domain: {disc_domain})")
                    continue
                
                if self.settings.get(K.SETTING_STAY_IN_DOMAIN, K.DEFAULT_STAY_IN_DOMAIN) and \
                   not is_same_domain(abs_disc_url, self.start_url): 
                    logger.debug(f"Skipping out-of-domain link: {abs_disc_url} (Original start: {self.start_url})")
                    continue
                
                stop_words_list = self.settings.get(K.SETTING_STOP_WORDS, K.DEFAULT_STOP_WORDS)
                if any(stop_word.lower() in abs_disc_url.lower() for stop_word in stop_words_list):
                    logger.debug(f"Skipping link with stop word: {abs_disc_url}")
                    continue

                new_ctx = {"source_url": url, "start_url": self.start_url, **link_spec_ctx}
                await self.url_queue.put(abs_disc_url, depth + 1, url, new_ctx)
        
        self.stats["pages_processed"] += 1
        self.status_updated.emit(f"Processed: {url}")

    async def _parser_worker(self, session):
        while not self._stop_event.is_set():
            if self.is_paused:
                await self._pause_event.wait()
                if self._stop_event.is_set(): break
                continue
            if not await self._handle_empty_queues_and_quarantine():
                await asyncio.sleep(0.5) 
                continue
            url_data = await self._get_next_url_to_parse()
            if not url_data:
                await asyncio.sleep(0.1) 
                continue
            current_url, depth, source_page_url, context = url_data
            try:
                if not current_url.startswith(("http://", "https://")):
                    current_url = urljoin(source_page_url or self.start_url, current_url)
                current_url = normalize_url(current_url)
                if current_url in self.processed_urls:
                    self.url_queue.task_done(); continue
                self.processed_urls.add(current_url)
                is_json = self._determine_parser_type(current_url)
                links_found, media_files_found = await self._invoke_parser(current_url, session, is_json, context)
                await self._process_parser_results(current_url, depth, links_found, media_files_found, context)
            except Exception as e:
                logger.error(f"Error processing URL {current_url}: {str(e)}", exc_info=True)
                if current_url not in self.processed_urls: self.processed_urls.add(current_url)
            finally:
                self.url_queue.task_done()
        logger.info(f"Parser worker {threading.get_ident()} finished.")

    async def _process_media_files(self, media_files: List[Tuple[str, str, Dict[str, Any]]], source_url: str) -> None:
        if not media_files: return
        await self._process_media_batch(media_files, source_url)

    async def _process_media_batch(self, media_files: List[Tuple[str, str, Dict[str, Any]]], source_url: str) -> None:
        sorted_media = sorted(media_files, key=lambda x: self._get_media_priority(x, source_url), reverse=True)
        for media_type, url, attrs in sorted_media:
            try:
                abs_url = urljoin(source_url, url) if not (url.startswith("http://") or url.startswith("https://")) else url
                abs_url = normalize_url(abs_url)
                if abs_url in self.downloaded_files: continue 
                    
                if is_webpage_url(abs_url) and not is_media_url(abs_url):
                    assumed_depth_for_media_webpage = 1 
                    if assumed_depth_for_media_webpage < self.max_depth:
                        ctx = {"source_url": source_url, "start_url": self.start_url, "from_media_item": True, "media_context": attrs, "priority": 5.0}
                        await self.url_queue.put(abs_url, assumed_depth_for_media_webpage, self.start_url, ctx)
                    continue
                
                base_filename = self._get_filename_from_url(abs_url, media_type)
                target_dir_path_final = self.download_path 
                page_domain_for_subdir = get_domain(source_url)
                
                if page_domain_for_subdir:
                    sane_domain = re.sub(r'[<>:"\/\\|?*]', '_', page_domain_for_subdir)
                    parsed_source_page = urlparse(source_url)
                    source_page_path_components = parsed_source_page.path.strip('/').split('/')
                    sane_path_parts = [re.sub(r'[<>:"\/\\|?*]', '_', part)[:K.MAX_SUBDIR_COMPONENT_LENGTH] for part in source_page_path_components if part][:K.MAX_PATH_COMPONENTS_FOR_SUBDIR]
                    path_subdir = os.path.join(*sane_path_parts) if sane_path_parts else ""
                    subdirname = os.path.join(sane_domain, path_subdir) if path_subdir else sane_domain
                    target_dir_path_final = os.path.join(self.download_path, subdirname)

                try:
                    os.makedirs(target_dir_path_final, exist_ok=True)
                except OSError as e:
                    logger.error(f"Could not create target directory {target_dir_path_final}: {e}. Defaulting to {self.download_path}", exc_info=True)
                    target_dir_path_final = self.download_path

                full_filepath_for_downloader = os.path.join(target_dir_path_final, base_filename)
                self.downloaded_files.add(abs_url) 

                media_item = {
                    "url": abs_url, "source_url": source_url, "media_type": media_type,
                    "attrs": attrs, "filepath": full_filepath_for_downloader,
                }
                await self.download_queue.put(media_item)
                if media_type == "image":
                    self.stats["images_found"] += 1
                elif media_type == "video":
                    self.stats["videos_found"] += 1
                elif media_type == "audio": # Add this condition
                    self.stats["audio_files_found"] += 1
            except Exception as err:
                logger.error(f"Error processing media file {url}: {str(err)}", exc_info=True)

    async def _downloader_worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.is_paused:
                    await self._pause_event.wait()
                    if self._stop_event.is_set(): break
                    continue
                media_item = await self.download_queue.get(timeout=0.5)
            except asyncio.TimeoutError: continue
            if not media_item:
                self.download_queue.task_done(); continue
            try:
                url = media_item["url"]
                filepath_from_queue = media_item["filepath"] 
                domain = urlparse(url).netloc
                if domain in self.quarantined_domains:
                    await self.quarantine_queue.put(media_item)
                    self.download_queue.task_done(); continue
                if domain not in self.domain_health: self.domain_health[domain] = {"failures": 0, "total": 0}
                domain_state = self.domain_health[domain]
                is_probation = domain_state["failures"] > 0
                
                timeout_val = K.DEFAULT_DOMAIN_PROBATION_TIMEOUT if is_probation else self.settings.get(K.SETTING_TIMEOUT, K.DEFAULT_TIMEOUT)
                retries_val = K.DEFAULT_DOMAIN_PROBATION_RETRIES if is_probation else self.settings.get(K.SETTING_RETRY_COUNT, K.DEFAULT_RETRY_COUNT)
                
                downloader = MediaDownloader(
                    url=url, filepath=filepath_from_queue, settings=self.settings,
                    media_type=media_item["media_type"], source_url=media_item["source_url"],
                )
                downloader.set_progress_callback(self._update_current_progress)
                base_filename_for_status = os.path.basename(filepath_from_queue)
                self.status_updated.emit(f"Downloading: {base_filename_for_status}")
                
                result = downloader.download(timeout=timeout_val, retries=retries_val)
                
                domain_state["total"] += 1
                if result["success"]:
                    self.stats["files_downloaded"] += 1
                    if domain_state["failures"] > 0: domain_state["failures"] = max(0, domain_state["failures"] - 1)
                else:
                    self.stats["files_skipped"] += 1
                    domain_state["failures"] += 1
                    logger.warning(f"Failed to download file: {url} - {result['error']}")
                    if domain_state["failures"] >= K.DEFAULT_QUARANTINE_FAILURE_THRESHOLD:
                        self.quarantined_domains.add(domain)
                        logger.warning(f"Domain {domain} quarantined after {domain_state['failures']} failures.")
            except Exception as err:
                logger.error(f"Error in downloader_worker for {media_item.get('url', 'Unknown URL')}: {str(err)}", exc_info=True)
            finally:
                self.download_queue.task_done()
        logger.info(f"Downloader worker {threading.get_ident()} finished.")

    def pause_parsing(self) -> None:
        self.is_paused = True; self._pause_event.clear(); logger.info("Parsing paused")

    def resume_parsing(self) -> None:
        self.is_paused = False; self._pause_event.set(); logger.info("Parsing resumed")

    def stop_parsing(self) -> None:
        logger.info("Attempting to stop parsing...")
        self.is_running = False; self._stop_event.set(); self._pause_event.set()
        try:
            while not self.download_queue.empty(): self.download_queue.get_nowait(); self.download_queue.task_done()
            while not self.quarantine_queue.empty(): self.quarantine_queue.get_nowait(); self.quarantine_queue.task_done()
            self.url_queue = PriorityURLQueue() 
            logger.info("Queues cleared.")
        except Exception as e: logger.error(f"Error clearing queues: {str(e)}", exc_info=True)
        logger.info("Parsing stop procedure initiated. Tasks will shut down.")

    def _monitor_progress(self) -> None:
        while self.is_running and not self._stop_event.is_set():
            try:
                if self.is_paused and not self._pause_event.is_set(): time.sleep(0.5); continue
                total_found = self.stats["images_found"] + self.stats["videos_found"] + self.stats["audio_files_found"] # Added audio_files_found
                total_proc = self.stats["files_downloaded"] + self.stats["files_skipped"]
                if total_found > 0: self.total_progress_updated.emit(int((total_proc / total_found) * 100))
                else: self.total_progress_updated.emit(0) 
                time.sleep(0.5) 
            except Exception as e: logger.error(f"Error in progress monitor: {str(e)}", exc_info=True); time.sleep(1)
        logger.info("Progress monitor finished.")

    def _update_current_progress(self, progress: int) -> None: self.current_progress_updated.emit(progress)

    def _get_filename_from_url(self, url: str, media_type: str) -> str:
        parsed_url = urlparse(url)
        path = parsed_url.path.strip("/")
        basename = os.path.basename(path)
        if not basename or "." not in basename:
            name_part = basename if basename and "." not in basename else hashlib.md5(url.encode()).hexdigest()[:K.DEFAULT_FILENAME_HASH_LENGTH]
            extension = ""
            if "." in path:
                potential_ext = os.path.splitext(path)[1]
                if potential_ext and 1 < len(potential_ext) <= 5: extension = potential_ext.lower() # Max 5 char extension like .jpeg
            if not extension: extension = K.DEFAULT_IMAGE_EXTENSION if media_type == "image" else K.DEFAULT_VIDEO_EXTENSION
            basename = f"{name_part}{extension}"
        return self._sanitize_filename(basename)

    def _sanitize_filename(self, filename: str) -> str:
        invalid_os_chars = r'<>:"/\\|?*' 
        control_chars = ''.join(map(chr, range(32)))
        sanitized_name = re.sub(f'[{re.escape(invalid_os_chars + control_chars)}]+', '_', filename)
        sanitized_name = re.sub(r'\s+', '_', sanitized_name) 
        sanitized_name = re.sub(r'__+', '_', sanitized_name).strip('_')
        name_part, ext_part = os.path.splitext(sanitized_name)
        if len(ext_part) > 7: ext_part = ext_part[:7] 
        
        # K.MAX_FILENAME_LENGTH from constants.py is for the name_part only
        if len(name_part) > K.MAX_FILENAME_LENGTH: name_part = name_part[:K.MAX_FILENAME_LENGTH]
        
        final_filename = f"{name_part}{ext_part}"
        if not name_part: 
             final_filename = f"{hashlib.md5(filename.encode()).hexdigest()[:K.DEFAULT_FILENAME_HASH_LENGTH]}{ext_part or K.DEFAULT_IMAGE_EXTENSION}"
        return final_filename
    
    def _get_media_priority(self, media_item: Tuple[str, str, Dict[str, Any]], source_url: str) -> float:
        media_type, url, attrs = media_item; priority = 1.0
        if media_type == "image": priority *= 2.0
        elif media_type == "video": priority *= 3.0
        source_type = attrs.get("source", "")
        if "fullsize" in source_type or "original" in source_type: priority *= 3.0
        elif "parent-link" in source_type: priority *= 2.5
        if any(p in url.lower() for p in ["/full/", "/large/", "/original/", "fullsize", "highres"]): priority *= 2.0
        if source_url == self.start_url: priority *= 3.0 
        if "dimensions" in attrs:
            w, h = attrs["dimensions"].get("width",0), attrs["dimensions"].get("height",0)
            if w > 0 and h > 0: priority *= min(1.0 + ((w * h) / 1000000), 3.0) 
        return priority
    
    def get_stats(self) -> Dict[str, int]: return self.stats.copy()

    async def save_state(self, task_download_path: str) -> None:
        url_queue_items = []
        if hasattr(self.url_queue, '_queue'):
             url_queue_items = [item_tuple_with_priority[1] for item_tuple_with_priority in self.url_queue._queue]
        
        download_queue_items = []
        temp_dq_holder = [] 
        while not self.download_queue.empty():
            try: item = self.download_queue.get_nowait(); temp_dq_holder.append(item)
            except asyncio.QueueEmpty: break
        download_queue_items.extend(temp_dq_holder) 
        for item in temp_dq_holder: await self.download_queue.put(item) 

        state = {
            "url_queue_items": url_queue_items, "download_queue_items": download_queue_items,
            "processed_urls": list(self.processed_urls), "downloaded_files": list(self.downloaded_files),
            "stats": self.stats, "settings": self.settings, "start_url": self.start_url,
            "download_path": self.download_path, 
            "domain_health": self.domain_health, "quarantined_domains": list(self.quarantined_domains)
        }
        
        session_dir = os.path.join(task_download_path, K.SESSION_STATE_SUBDIR) 
        os.makedirs(session_dir, exist_ok=True)
        full_state_path = os.path.join(session_dir, K.SESSION_STATE_FILENAME)

        async with aiofiles.open(full_state_path, "wb") as f: await f.write(pickle.dumps(state))
        logger.info(f"Session state saved to {full_state_path}")

    async def load_state(self, task_download_path: str) -> None: 
        session_file_path = os.path.join(task_download_path, K.SESSION_STATE_SUBDIR, K.SESSION_STATE_FILENAME)
        if not os.path.exists(session_file_path):
            logger.info(f"No session state file found at {session_file_path}. Starting fresh.")
            return

        try:
            async with aiofiles.open(session_file_path, "rb") as f: data = await f.read(); state = pickle.loads(data)
            
            for item_tuple in state.get("url_queue_items", []):
                if len(item_tuple) == 4: # url, depth, source_url, context
                     await self.url_queue.put(item_tuple[0], item_tuple[1], item_tuple[2], item_tuple[3])
            for item in state.get("download_queue_items", []): await self.download_queue.put(item)

            self.processed_urls = set(state.get("processed_urls", []))
            self.downloaded_files = set(state.get("downloaded_files", [])) 
            self.stats = state.get("stats", self.stats)
            self.start_url = state.get("start_url", self.start_url)
            self.domain_health = state.get("domain_health", {})
            self.quarantined_domains = set(state.get("quarantined_domains", []))
            logger.info(f"Successfully loaded state from {session_file_path}")
        except Exception as err:
            logger.error(f"Error loading state from {session_file_path}: {str(err)}", exc_info=True)
