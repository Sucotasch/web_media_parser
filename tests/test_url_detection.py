#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Test URL detection functions
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.parser.utils import is_media_url, is_image_url, is_video_url, is_audio_url


def test_media_url_detection():
    """Test media URL detection"""
    # Valid media URLs
    assert is_media_url("https://example.com/image.jpg")
    assert is_media_url("https://example.com/video.mp4")
    assert is_media_url("https://example.com/document.pdf")
    assert is_media_url("https://example.com/images/photo.png")
    assert is_media_url("https://cdn.example.com/image.jpg")
    
    # URLs that should NOT be detected as media
    assert not is_media_url("https://example.com/page.html")
    assert not is_media_url("https://example.com/index.php")
    assert not is_media_url("https://example.com/article.asp")
    assert not is_media_url("https://example.com/profile.jsp")
    assert not is_media_url("https://example.com/gallery.aspx")
    
    # Special cases
    assert is_media_url("https://example.com/images/gallery/")
    assert is_media_url("https://example.com/download/file?name=image.jpg")


def test_image_url_detection():
    """Test image URL detection"""
    assert is_image_url("https://example.com/image.jpg")
    assert is_image_url("https://example.com/photo.png")
    assert not is_image_url("https://example.com/video.mp4")
    assert not is_image_url("https://example.com/page.html")


def test_video_url_detection():
    """Test video URL detection"""
    assert is_video_url("https://example.com/video.mp4")
    assert is_video_url("https://example.com/movie.webm")
    assert not is_video_url("https://example.com/image.jpg")
    assert not is_video_url("https://example.com/page.html")


def test_audio_url_detection():
    """Test audio URL detection"""
    # Valid audio URLs (direct extension)
    assert is_audio_url("https://example.com/audio.mp3")
    assert is_audio_url("https://example.com/track.wav")
    assert is_audio_url("https://example.com/sound.ogg")
    assert is_audio_url("https://example.com/music.aac")
    assert is_audio_url("https://example.com/song.flac")
    assert is_audio_url("https://example.com/podcast.m4a")
    assert is_audio_url("https://example.com/speech.opus")
    assert is_audio_url("https://example.com/AUDIO.MP3")  # case insensitivity
    assert is_audio_url("https://example.com/audio.mp3?query=param")  # with query parameters

    # Invalid URLs (should not be detected as audio)
    assert not is_audio_url("https://example.com/image.jpg")
    assert not is_audio_url("https://example.com/video.mp4")
    assert not is_audio_url("https://example.com/document.pdf")
    assert not is_audio_url("https://example.com/page.html")
    assert not is_audio_url("https://example.com/script.js")
    assert not is_audio_url("https://example.com/archive.zip")
    assert not is_audio_url("https://example.com/noextension")
    assert not is_audio_url("https://example.com/mp3") # no dot before extension

    # URLs that might seem like audio but are not (e.g., part of a path)
    assert not is_audio_url("https://example.com/path/mp3/file.html")
    assert not is_audio_url("https://example.com/path.mp3archive/file")