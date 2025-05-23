#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Media downloader class for downloading media files
"""

import os
import re
import time
import threading
from urllib.parse import urlparse
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)


class MediaDownloader:
    """
    Media downloader class for downloading media files
    """

    def __init__(self, url, filepath, settings, media_type="image", source_url=None):
        self.url = url
        self.filepath = filepath
        self.settings = settings
        self.media_type = media_type
        self.source_url = source_url
        self.progress_callback = None

        # Create requests session with custom settings
        self.session = self._create_session()

        # Rate limiting
        self.rate_limit = self.settings.get(
            "max_download_speed", 0
        )  # KB/s, 0 = unlimited

        # Thread settings
        self.threads_per_file = min(
            self.settings.get("threads_per_file", 1), 8
        )  # Max 8 threads per file

    def _create_session(self):
        """
        Create requests session with retry and custom headers
        """
        session = requests.Session()

        # Configure retry strategy
        retry_strategy = Retry(
            total=self.settings.get("retry_count", 3),
            backoff_factor=0.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Set headers
        headers = {
            "User-Agent": self.settings.get(
                "user_agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            ),
            "Accept-Language": self.settings.get("accept_language", "en-US,en;q=0.9"),
        }

        # Set appropriate Accept header based on media type
        if self.media_type == "image":
            headers["Accept"] = (
                "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"
            )
        elif self.media_type == "video":
            headers["Accept"] = "video/mp4,video/webm,video/*,*/*;q=0.8"
        else:
            headers["Accept"] = "*/*"

        # Set referrer if source URL is available
        if self.source_url:
            referrer_policy = self.settings.get("referrer", "auto")
            if referrer_policy == "origin":
                parsed = urlparse(self.source_url)
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}"
            elif referrer_policy == "auto":
                headers["Referer"] = self.source_url

        session.headers.update(headers)
        return session

    def set_progress_callback(self, callback):
        """
        Set callback function for progress updates
        """
        self.progress_callback = callback

    def download(self, timeout=None, retries=None):
        """
        Download media file with customizable timeout and retry count
        
        Args:
            timeout (int, optional): Custom timeout in seconds. Defaults to settings timeout.
            retries (int, optional): Custom retry count. Defaults to settings retry count.
        """
        try:
            # Save original settings
            original_timeout = self.settings.get("timeout", 30)
            original_retries = self.settings.get("retry_count", 3)
            
            # Apply custom settings if provided
            if timeout is not None:
                self.settings["timeout"] = timeout
            if retries is not None:
                self.settings["retry_count"] = retries
                
            # Recreate session with new settings if retries changed
            if retries is not None:
                self.session = self._create_session()
                
            # Download with custom settings
            result = self._do_download()
            
            # Restore original settings
            if timeout is not None:
                self.settings["timeout"] = original_timeout
            if retries is not None:
                self.settings["retry_count"] = original_retries
                
            return result
        except Exception as e:
            logger.error(f"Download failed for {self.filepath}: {str(e)}")
            return {"success": False, "error": str(e)}

    def _get_unique_filepath(self, base_path: str, remote_size: int) -> str:
        """
        Generate unique filepath using timestamp for uniqueness
        Creates a subdirectory based on the source URL domain if available
        Returns: unique filepath
        """
        # Create a subdirectory based on the source URL if available
        dir_path = os.path.dirname(base_path)
        base_name, ext = os.path.splitext(os.path.basename(base_path))
        
        # If we have a source URL, create a subdirectory based on its domain and path
        if self.source_url:
            from urllib.parse import urlparse
            parsed = urlparse(self.source_url)
            domain = parsed.netloc
            
            # Remove www. prefix if present
            if domain.startswith('www.'):
                domain = domain[4:]
                
            # Create a sanitized path component
            path_component = ''
            if parsed.path and parsed.path != '/':
                path = parsed.path.strip('/')
                # Take the first 2 path components for organization
                path_parts = path.split('/')
                if len(path_parts) > 0:
                    path_component = '_'.join(path_parts[:min(2, len(path_parts))])
                    # Sanitize the path component
                    path_component = re.sub(r'[<>:"\/\\|?*]', '_', path_component)
                    
            # Create the subdirectory name
            if path_component:
                subdir_name = f"{domain}_{path_component}"
            else:
                subdir_name = domain
                
            # Create subdirectory
            subdir_path = os.path.join(dir_path, subdir_name)
            os.makedirs(subdir_path, exist_ok=True)
            
            # Update the file path
            dir_path = subdir_path
        
        # Check if the file exists in the new path
        new_path = os.path.join(dir_path, f"{base_name}{ext}")
        if not os.path.exists(new_path):
            return new_path

        # If file exists, add timestamp for uniqueness
        timestamp = int(time.time() * 1000)  # Millisecond timestamp for more uniqueness
        return os.path.join(dir_path, f"{base_name}_{timestamp}{ext}")

    def _do_download(self):
        """
        Internal download implementation
        """
        try:
            # Quick check for common webpage file extensions that should NOT be downloaded
            non_media_extensions = [
                # Webpage extensions
                ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".jspx",
                ".cfm", ".cfml", ".py", ".rb", ".pl", ".cgi", ".shtml", ".xhtml",
                # Script and data extensions
                ".js", ".jsx", ".ts", ".tsx", ".coffee", ".es6", ".mjs",
                ".css", ".scss", ".sass", ".less", 
                ".json", ".xml", ".rss", ".atom", ".yaml", ".yml", 
                ".wasm", ".map"
            ]
            
            url_lower = self.url.lower()
            if any(url_lower.endswith(ext) for ext in non_media_extensions):
                logger.warning(f"Skipping non-media file: {self.url}")
                return {"success": False, "error": "Non-media files should not be downloaded"}
            
            # Create directory if not exists first
            os.makedirs(os.path.dirname(self.filepath), exist_ok=True)

            # Check file size and get initial response
            try:
                response = self.session.head(
                    self.url, timeout=self.settings.get("timeout", 30)
                )
                content_length = int(response.headers.get("Content-Length", 0))
                remote_modified = response.headers.get("Last-Modified")
                
                # Check Content-Type to filter out HTML and script files
                content_type = response.headers.get("Content-Type", "").lower()
                if any(t in content_type for t in ["text/html", "application/javascript", "text/javascript", "text/css", "application/json"]):
                    logger.warning(f"Skipping webpage/script content: {self.url} (Content-Type: {content_type})")
                    return {"success": False, "error": f"Webpage/script content should not be downloaded (Content-Type: {content_type})"}

                # Apply size filters only if we got a valid content length
                if content_length > 0:
                    size_kb = content_length / 1024
                    
                    # Get minimum size setting based on media type
                    # Log only the relevant size settings
                    logger.info(f"Min image size setting: {self.settings.get('min_image_size', 0)} KB")
                    logger.info(f"Min video size setting: {self.settings.get('min_video_size', 0)} KB")
                    
                    min_size_key = "min_image_size" if self.media_type == "image" else "min_video_size"
                    logger.info(f"Using {min_size_key} for media type: {self.media_type}")
                    min_size = self.settings.get(min_size_key, 0)
                    
                    # Log filter details for debugging
                    logger.info(
                        f"Size check for {self.url} - Media type: {self.media_type}, "
                        f"Size: {size_kb:.2f}KB, Min size: {min_size}KB, Setting key: {min_size_key}"
                    )
                    
                    if min_size > 0 and size_kb < min_size:
                        logger.info(
                            f"Skipping {self.url} - too small ({size_kb:.2f}KB < {min_size}KB)"
                        )
                        return {"success": False, "error": "File too small"}
            except Exception as e:
                logger.warning(f"Could not check size for {self.url}: {str(e)}")
                content_length = 0

            # Get unique filepath considering sizes
            if content_length > 0:
                self.filepath = self._get_unique_filepath(self.filepath, content_length)

            # Always use write mode, no resuming downloads
            mode = "wb"
            headers = {}

            # Attempt multi-threaded download if conditions allow
            if self.threads_per_file > 1 and mode == "wb" and content_length > 0:
                try:
                    result = self._download_with_threads()
                    if result["success"]:
                        return result
                    # If multi-threaded fails, fall back to single-threaded
                    logger.warning(
                        "Multi-threaded download failed, falling back to single-threaded"
                    )
                except Exception as e:
                    logger.warning(
                        f"Multi-threaded download failed: {str(e)}, falling back to single-threaded"
                    )

            # Single-threaded download
            logger.info(
                f"Starting single-threaded download: {os.path.basename(self.filepath)}"
            )
            response = self.session.get(
                self.url,
                headers=headers,
                stream=True,
                timeout=self.settings.get("timeout", 30),
            )

            if response.status_code not in [200, 206]:
                return {
                    "success": False,
                    "error": f"HTTP error: {response.status_code}",
                }

            # Download file with progress tracking and rate limiting
            with open(self.filepath, mode) as f:
                start_time = time.time()
                chunk_size = 8192  # 8KB chunks
                downloaded = 0

                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:  # Filter out keep-alive chunks
                        f.write(chunk)
                        downloaded += len(chunk)

                        # Update progress if we know the total size
                        if content_length > 0 and self.progress_callback:
                            progress = min(
                                100, int((downloaded / content_length) * 100)
                            )
                            self.progress_callback(progress)
                        elif self.progress_callback:
                            # If we don't know the size, just show activity
                            self.progress_callback(-1)

                        # Apply rate limiting if enabled
                        if self.rate_limit > 0:
                            elapsed = time.time() - start_time
                            expected_time = downloaded / (
                                self.rate_limit * 1024
                            )  # Convert KB/s to bytes/s
                            if elapsed < expected_time:
                                time.sleep(expected_time - elapsed)

            # Verify download completed successfully
            if content_length > 0:
                final_size = os.path.getsize(self.filepath)
                if final_size != content_length:
                    return {
                        "success": False,
                        "error": f"Downloaded file size mismatch: expected {content_length}, got {final_size}",
                    }

            # Final progress update
            if self.progress_callback:
                self.progress_callback(100)

            logger.info(
                f"Download completed successfully: {os.path.basename(self.filepath)}"
            )
            return {"success": True, "message": "File downloaded successfully"}

        except Exception as e:
            logger.error(f"Download failed: {str(e)}")
            return {"success": False, "error": str(e)}

    def _download_with_threads(self):
        """
        Download file using multiple threads
        """
        temp_files = []
        try:
            # Get file info
            response = self.session.head(
                self.url, timeout=self.settings.get("timeout", 30)
            )

            # Check if request was successful
            if response.status_code != 200:
                return {
                    "success": False,
                    "error": f"HTTP error: {response.status_code}",
                }

            # Check if server supports range requests
            if (
                "Accept-Ranges" not in response.headers
                or response.headers["Accept-Ranges"] != "bytes"
            ):
                logger.info(
                    "Server does not support range requests, falling back to single-threaded download"
                )
                return {"success": False, "error": "Range requests not supported"}

            # Get file size
            total_size = int(response.headers.get("Content-Length", 0))
            if total_size == 0:
                logger.info(
                    "Unknown file size, falling back to single-threaded download"
                )
                return {"success": False, "error": "Unknown file size"}

            # Calculate optimal thread count based on file size
            # Use fewer threads for smaller files
            min_chunk_size = 1024 * 1024  # 1MB minimum chunk size
            optimal_threads = min(
                self.threads_per_file,
                max(1, total_size // min_chunk_size),
                8,  # Hard maximum of 8 threads
            )

            # Calculate chunk size
            chunk_size = total_size // optimal_threads

            # Create and start download threads
            threads = []
            progress_lock = threading.Lock()
            progress_dict = {"total": 0, "success": True}

            logger.info(
                f"Starting multi-threaded download with {optimal_threads} threads"
            )

            for i in range(optimal_threads):
                # Calculate range for this thread
                start = i * chunk_size
                end = (
                    (i + 1) * chunk_size - 1
                    if i < optimal_threads - 1
                    else total_size - 1
                )

                # Create temporary file
                temp_file = f"{self.filepath}.part{i}"
                temp_files.append(temp_file)

                # Create and start thread
                thread = threading.Thread(
                    target=self._download_chunk,
                    args=(
                        start,
                        end,
                        temp_file,
                        total_size,
                        progress_dict,
                        progress_lock,
                    ),
                )
                thread.daemon = True
                thread.start()
                threads.append(thread)

            # Wait for all threads to complete
            for thread in threads:
                thread.join()

            # Check if all chunks were downloaded successfully
            if not progress_dict["success"]:
                raise Exception("One or more chunks failed to download")

            # Combine all chunks
            logger.info("Combining downloaded chunks")
            with open(self.filepath, "wb") as outfile:
                for temp_file in temp_files:
                    if os.path.exists(temp_file):
                        with open(temp_file, "rb") as infile:
                            outfile.write(infile.read())

            # Verify final file size
            final_size = os.path.getsize(self.filepath)
            if final_size != total_size:
                raise Exception(
                    f"File size mismatch after combining chunks: expected {total_size}, got {final_size}"
                )

            # Clean up temporary files
            for temp_file in temp_files:
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass

            # Final progress update
            if self.progress_callback:
                self.progress_callback(100)

            logger.info("Multi-threaded download completed successfully")
            return {"success": True, "message": "File downloaded successfully"}

        except Exception as e:
            # Clean up temporary files
            for temp_file in temp_files:
                if os.path.exists(temp_file):
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass

            logger.error(f"Multi-threaded download failed: {str(e)}")
            return {"success": False, "error": str(e)}

    def _download_chunk(
        self, start, end, filename, total_size, progress_dict, progress_lock
    ):
        """
        Download a chunk of the file
        """
        headers = {"Range": f"bytes={start}-{end}"}
        chunk_size = 8192  # 8KB chunks
        retries = self.settings.get("retry_count", 3)
        retry_delay = 0.5  # Initial retry delay in seconds

        for attempt in range(retries + 1):
            try:
                response = self.session.get(
                    self.url,
                    headers=headers,
                    stream=True,
                    timeout=self.settings.get("timeout", 30),
                )

                if response.status_code not in [200, 206]:
                    if attempt < retries:
                        time.sleep(retry_delay * (2**attempt))  # Exponential backoff
                        continue
                    with progress_lock:
                        progress_dict["success"] = False
                    return

                downloaded = 0
                start_time = time.time()

                with open(filename, "wb") as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                            # Update progress
                            with progress_lock:
                                progress_dict["total"] += len(chunk)
                                if self.progress_callback:
                                    progress = min(
                                        99,  # Leave room for final verification
                                        int(
                                            (progress_dict["total"] / total_size) * 100
                                        ),
                                    )
                                    self.progress_callback(progress)

                            # Apply rate limiting if enabled
                            if self.rate_limit > 0:
                                elapsed = time.time() - start_time
                                expected_time = downloaded / (
                                    self.rate_limit * 1024 / self.threads_per_file
                                )  # Adjust limit per thread
                                if elapsed < expected_time:
                                    time.sleep(expected_time - elapsed)

                # Verify chunk size after download
                if os.path.getsize(filename) != (end - start + 1):
                    if attempt < retries:
                        time.sleep(retry_delay * (2**attempt))
                        continue
                    with progress_lock:
                        progress_dict["success"] = False
                    return

                # Successfully downloaded chunk
                return

            except Exception as e:
                logger.warning(f"Chunk download attempt {attempt + 1} failed: {str(e)}")
                if attempt < retries:
                    time.sleep(retry_delay * (2**attempt))
                    continue
                with progress_lock:
                    progress_dict["success"] = False
                return
